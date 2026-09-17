"""Non-blocking Telegram live alerts for the Board of Directors."""

from __future__ import annotations

import html
import re
import threading
from typing import Any

import requests

from core.config import Settings, mask_secret
from core.llm import API_QUOTA_VETO, is_quota_fault
from core.retry import call_with_backoff

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
STARTUP_TEXT = (
    "Chronos-Nexus is ONLINE — glasshouse desk, unlimited Demo universe, "
    "hourly tape scan."
)
_TELEGRAM_MAX = 3900
_INFLIGHT: list[threading.Thread] = []
_INFLIGHT_LOCK = threading.Lock()
_TAG_RE = re.compile(r"</?(?:b|i|u|s|code|pre|a)(?:\s[^>]*)?>", re.IGNORECASE)


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

    def alert_news_analysis(
        self,
        *,
        headlines: list[str] | None = None,
        news_good: str = "",
        news_bad: str = "",
        session: str = "",
        sources: list[str] | None = None,
        universe_n: int = 0,
    ) -> None:
        lines = [
            "<b>CHRONOS-NEXUS</b>",
            "📡 <b>MARKET WIRE</b>",
        ]
        if session:
            lines.append(f"<i>{_html(session)}</i>")
        if universe_n:
            lines.append(f"<b>Demo universe:</b> <code>{universe_n}</code> listed names")
        lines.append("")
        lines.append("<b>What's working</b>")
        lines.append(_html(news_good or "No constructive tape isolated on this scan."))
        lines.append("")
        lines.append("<b>What's hurting</b>")
        lines.append(_html(news_bad or "No discrete shock isolated on this scan."))
        if headlines:
            lines.append("")
            lines.append("<b>Headlines</b>")
            for idx, headline in enumerate(headlines[:8], start=1):
                lines.append(f"{idx}. {_html(headline)}")
        if sources:
            uniq = []
            for src in sources:
                if src and src not in uniq:
                    uniq.append(src)
            if uniq:
                lines.append("")
                lines.append(f"<b>Sources:</b> {_html(', '.join(uniq))}")
        self.send_async("\n".join(lines))

    def alert_stay_away(
        self,
        *,
        items: list[str],
        rationale: str = "",
        session: str = "",
    ) -> None:
        if not items:
            return
        lines = [
            "<b>CHRONOS-NEXUS</b>",
            "⚠️ <b>STAY AWAY</b>",
        ]
        if session:
            lines.append(f"<i>{_html(session)}</i>")
        lines.append("")
        lines.append("Toxic tape — do not buy these names on this cycle.")
        for item in items[:8]:
            lines.append(f"• <code>{_html(item)}</code>")
        if rationale:
            lines.append("")
            lines.append(f"<b>Rationale:</b> {_html(rationale)}")
        self.send_async("\n".join(lines))

    def alert_stand_down(
        self,
        *,
        reason: str,
        symbol: str = "NONE",
        side: str = "none",
        conviction: int = 0,
        model: str = "",
        session: str = "",
    ) -> None:
        lines = [
            "<b>CHRONOS-NEXUS</b>",
            "CHAIRMAN <b>STAND_DOWN</b>",
        ]
        if session:
            lines.append(f"<i>{_html(session)}</i>")
        lines.extend(
            [
                "",
                "Desk is safely standing down. No blind fallback. No assumed BUY.",
                f"<b>Symbol:</b> <code>{_html(symbol or 'NONE')}</code>",
                f"<b>Side:</b> <code>{_html(str(side or 'none').upper())}</code>",
                f"<b>Conviction:</b> <code>{_html(conviction)}</code>",
                f"<b>Reason:</b> {_html(reason)}",
            ]
        )
        if model:
            lines.append(f"<b>Model:</b> <code>{_html(model)}</code>")
        self.send_async("\n".join(lines))

    def alert_api_error(
        self,
        *,
        error: str,
        where: str = "OpenRouter - Qwen",
        action: str = "STAND_DOWN",
        session: str = "",
    ) -> None:
        quota = is_quota_fault(error)
        title = (
            "⚠️ <b>OPENROUTER QUOTA</b>"
            if quota
            else "⚠️ <b>API ERROR / TIMEOUT</b>"
        )
        fault = API_QUOTA_VETO if quota else error
        lines = [
            "<b>CHRONOS-NEXUS</b>",
            title,
        ]
        if session:
            lines.append(f"<i>{_html(session)}</i>")
        lines.extend(
            [
                "",
                f"<b>Where:</b> <code>{_html(where)}</code>",
                f"<b>Reason:</b> {_html(fault)}",
                f"<b>Action:</b> <code>{_html(action)}</code>",
                "primary_symbol=<code>NONE</code>  side=<code>none</code>  conviction=<code>0</code>",
            ]
        )
        if quota:
            lines.append(
                "Not a crash. OpenRouter limit hit — the hourly daemon will retry after reset."
            )
        else:
            lines.append(
                "No NVDA default. No assumed BUY. Desk stands down until the next live cycle."
            )
        self.send_async("\n".join(lines))

    def alert_veto(
        self,
        *,
        reason: str,
        symbol: str,
        flags: list[str] | None = None,
        model: str = "",
        status: str = "",
        spread: str = "",
    ) -> None:
        flag_bit = ", ".join(flags or []) or "none"
        title = "SENTINEL <b>VETO</b>  ·  trade skipped"
        if status:
            title = f"SENTINEL <b>VETO</b>  ·  {_html(status)}"
        lines = [
            "<b>CHRONOS-NEXUS</b>",
            title,
            "",
            f"<b>Exact reason:</b> {_html(reason)}",
            f"<b>Symbol:</b> <code>{_html(symbol)}</code>",
        ]
        if spread:
            lines.append(f"<b>Spread:</b> <code>{_html(spread)}</code>")
        lines.append(f"<b>Flags:</b> <code>{_html(flag_bit)}</code>")
        if model:
            lines.append(f"<b>Model:</b> <code>{_html(model or '—')}</code>")
        self.send_async("\n".join(lines))

    def alert_position_open(self, *, symbol: str, side: str = "", detail: str = "") -> None:
        extra = f" ({_html(side.upper())})" if side else ""
        text = "\n".join(
            [
                "<b>CHRONOS-NEXUS</b>",
                "⚠️ <b>TRADE SKIPPED</b> — position already open",
                "",
                f"<b>Symbol:</b> <code>{_html(symbol)}</code>{extra}",
                "<b>Exact reason:</b> Position already open — refusing to stack a redundant book.",
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
        why: str = "",
        thesis: str = "",
        news_good: str = "",
        passed_over: str = "",
        session: str = "",
    ) -> None:
        lines = [
            "<b>CHRONOS-NEXUS</b>",
            "CHAIRMAN <b>EXECUTE</b>  ·  best opportunity",
        ]
        if session:
            lines.append(f"<i>{_html(session)}</i>")
        lines.extend(
            [
                "",
                f"<b>Symbol:</b> <code>{_html(symbol)}</code>",
                f"<b>Side:</b> <code>{_html(str(side).upper())}</code>",
                f"<b>Amount:</b> <code>{_html(amount)}</code>",
            ]
        )
        if sl_price not in (None, "", 0, "0"):
            lines.append(f"<b>Stop-Loss:</b> <code>{_html(sl_price)}</code> (-2%)")
        if tp_price not in (None, "", 0, "0"):
            lines.append(f"<b>Take-Profit:</b> <code>{_html(tp_price)}</code> (+5%)")
        if order_id:
            lines.append(f"<b>Order:</b> <code>{_html(order_id)}</code>")
        if explorer_url:
            href = html.escape(str(explorer_url), quote=True)
            lines.append(f'<b>Proof:</b> <a href="{href}">Arbitrum Sepolia</a>')
        if why:
            lines.append("")
            lines.append("<b>Why this name over the rest of the tape</b>")
            lines.append(_html(why))
        if passed_over:
            lines.append(f"<b>Passed over:</b> {_html(passed_over)}")
        if thesis:
            lines.append("")
            lines.append(f"<b>Thesis:</b> {_html(thesis)}")
        if news_good:
            lines.append(f"<b>Tape:</b> {_html(news_good)}")
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
            args=(_clip_telegram(text),),
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


def _clip_telegram(text: str, limit: int = _TELEGRAM_MAX) -> str:
    raw = str(text or "")
    if len(raw) <= limit:
        return raw
    return raw[: limit - 1] + "…"


def _strip_tags(text: str) -> str:
    out = _TAG_RE.sub("", text)
    out = out.replace("<b>", "").replace("</b>", "")
    out = out.replace("<code>", "").replace("</code>", "")
    out = out.replace("<i>", "").replace("</i>", "")
    return out


# Back-compat alias for older tests / imports.
def escape_markdown_v2(text: str) -> str:
    return escape_html(text)
