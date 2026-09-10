"""Non-blocking Telegram live alerts for the Board of Directors."""

from __future__ import annotations

import html
import threading
from typing import Any

import requests

from core.config import Settings, mask_secret
from core.retry import call_with_backoff

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
STARTUP_TEXT = "Chronos-Nexus is ONLINE and monitoring the tape."
_INFLIGHT: list[threading.Thread] = []
_INFLIGHT_LOCK = threading.Lock()


class TelegramNotifier:
    def __init__(self, token: str = "", chat_id: str = "") -> None:
        self.token = (token or "").strip()
        self.chat_id = str(chat_id or "").strip()

    @classmethod
    def from_settings(cls, settings: Settings) -> "TelegramNotifier":
        return cls(settings.telegram_bot_token, settings.telegram_chat_id)

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    def status(self) -> dict[str, str]:
        return {
            "enabled": str(self.enabled),
            "token": mask_secret(self.token),
            "chat_id": self.chat_id or "(empty)",
        }

    def send_startup_message(self) -> bool:
        """Synchronous boot ping so credentials are proven before the cycle."""
        if not self.enabled:
            print(
                "[TELEGRAM ERROR] disarmed — TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing",
                flush=True,
            )
            return False
        text = f"<b>CHRONOS-NEXUS</b>\n{html.escape(STARTUP_TEXT)}"
        try:
            self._post(text, parse_mode="HTML")
            print("[TELEGRAM] startup ping sent", flush=True)
            return True
        except Exception as exc:
            print(f"[TELEGRAM ERROR] {str(exc)}", flush=True)
            return False

    def alert_veto(
        self,
        *,
        reason: str,
        symbol: str,
        flags: list[str] | None = None,
        model: str = "",
    ) -> None:
        flag_bit = ", ".join(flags or []) or "none"
        text = "\n".join(
            [
                "<b>CHRONOS-NEXUS</b>",
                "SENTINEL <b>VETO</b>",
                "",
                f"<b>Reason:</b> <code>{_html(reason)}</code>",
                f"<b>Symbol:</b> <code>{_html(symbol)}</code>",
                f"<b>Flags:</b> <code>{_html(flag_bit)}</code>",
                f"<b>Model:</b> <code>{_html(model or '—')}</code>",
            ]
        )
        self.send_async(text)

    def alert_position_open(self, *, symbol: str, side: str = "", detail: str = "") -> None:
        extra = f" ({_html(side.upper())})" if side else ""
        text = "\n".join(
            [
                "<b>CHRONOS-NEXUS</b>",
                f"⚠️ Trade Skipped: Position already open for {_html(symbol)}{extra}",
            ]
        )
        if detail:
            text += f"\n<code>{_html(detail)}</code>"
        self.send_async(text)

    def alert_execute(
        self,
        *,
        symbol: str,
        side: str,
        amount: float | str,
        sl_price: Any = None,
        tp_price: Any = None,
        order_id: Any = None,
        explorer_url: str | None = None,
        model: str = "",
    ) -> None:
        lines = [
            "<b>CHRONOS-NEXUS</b>",
            "CHAIRMAN <b>EXECUTE</b>",
            "",
            f"<b>Symbol:</b> <code>{_html(symbol)}</code>",
            f"<b>Side:</b> <code>{_html(str(side).upper())}</code>",
            f"<b>Amount:</b> <code>{_html(amount)}</code>",
        ]
        if sl_price not in (None, "", 0, "0"):
            lines.append(f"<b>Stop-Loss:</b> <code>{_html(sl_price)}</code> (-2%)")
        if tp_price not in (None, "", 0, "0"):
            lines.append(f"<b>Take-Profit:</b> <code>{_html(tp_price)}</code> (+5%)")
        if order_id:
            lines.append(f"<b>Order:</b> <code>{_html(order_id)}</code>")
        if explorer_url:
            href = html.escape(str(explorer_url), quote=True)
            lines.append(f'<b>Proof:</b> <a href="{href}">Arbitrum Sepolia</a>')
        if model:
            lines.append(f"<b>Model:</b> <code>{_html(model)}</code>")
        self.send_async("\n".join(lines))

    def send_async(self, text: str) -> None:
        if not self.enabled:
            print(
                "[TELEGRAM ERROR] disarmed — TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing",
                flush=True,
            )
            return
        thread = threading.Thread(
            target=self._send_safe,
            args=(text,),
            daemon=True,
            name="chronos-telegram",
        )
        with _INFLIGHT_LOCK:
            _INFLIGHT.append(thread)
        thread.start()

    def drain(self, timeout: float = 3.0) -> None:
        """Best-effort flush so the cycle can exit without dropping the alert."""
        with _INFLIGHT_LOCK:
            threads = list(_INFLIGHT)
            _INFLIGHT.clear()
        for thread in threads:
            thread.join(timeout=timeout)

    def _send_safe(self, text: str) -> None:
        try:
            self._post(text, parse_mode="HTML")
        except Exception as exc:
            print(f"[TELEGRAM ERROR] HTML send failed: {str(exc)}", flush=True)
            try:
                self._post(_strip_tags(text), parse_mode=None)
            except Exception as exc2:
                print(f"[TELEGRAM ERROR] {str(exc2)}", flush=True)

    def _post(self, text: str, parse_mode: str | None) -> None:
        url = TELEGRAM_API.format(token=self.token)
        payload: dict[str, Any] = {
            "chat_id": self.chat_id,
            "text": text,
            "disable_web_page_preview": False,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode

        def _once() -> None:
            resp = requests.post(url, json=payload, timeout=12)
            body = ""
            try:
                body = resp.text[:300]
            except Exception:
                body = ""
            if resp.status_code == 429:
                raise TimeoutError(f"429 telegram rate limit {body}")
            if resp.status_code >= 400:
                raise RuntimeError(f"telegram HTTP {resp.status_code}: {body}")
            data = resp.json() if resp.content else {}
            if isinstance(data, dict) and data.get("ok") is False:
                raise RuntimeError(str(data.get("description") or "telegram send failed"))

        call_with_backoff(_once, attempts=3, label="telegram.sendMessage")


def send_startup_message(notifier: TelegramNotifier) -> bool:
    """Boot ping used by main.py to verify Telegram credentials immediately."""
    return notifier.send_startup_message()


def escape_html(text: str) -> str:
    return html.escape(str(text), quote=False)


def _html(value: Any) -> str:
    return escape_html(value)


def _strip_tags(text: str) -> str:
    out = text.replace("<b>", "").replace("</b>", "")
    out = out.replace("<code>", "").replace("</code>", "")
    return out


# Back-compat alias for older tests / imports.
def escape_markdown_v2(text: str) -> str:
    return escape_html(text)
