"""Bitget Hackathon - Qwen 3.8 Max cortex used by the Board of Directors."""

from __future__ import annotations

import json
import re
import threading
from typing import Any, Callable, TypeVar

from core.config import (
    CORTEX_BACKEND_LABEL,
    QWEN_BASE_URL,
    DEFAULT_QWEN_MODEL,
    Settings,
    normalize_model_name,
)
from core.retry import call_with_backoff

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE | re.DOTALL)

# Hard cap per HTTP round-trip. Never let chat.completions block the CIC.
LLM_TIMEOUT_S = 45
API_TIMEOUT_VETO = "VETO: API Timeout"
API_QUOTA_VETO = (
    "Bitget Qwen API quota reached. System safely standing down until limits reset."
)

T = TypeVar("T")


def _api_error(exc: BaseException) -> None:
    print(f"[API ERROR] {_clean_api_error(exc)}", flush=True)


def _clean_api_error(exc: BaseException) -> str:
    if _looks_quota(exc):
        return API_QUOTA_VETO
    if _looks_parse_fault(exc):
        return "parser fault: unusable Qwen JSON"
    if _looks_unavailable(exc):
        return "Bitget Qwen 503/unavailable — standing down"
    msg = " ".join(str(exc).split())
    return msg[:400] if msg else type(exc).__name__


def _looks_timeout(exc: BaseException) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    blob = f"{type(exc).__name__} {exc}".lower()
    return any(tok in blob for tok in ("timeout", "timed out", "deadline", "read timed out"))


def _looks_parse_fault(exc: BaseException) -> bool:
    if isinstance(exc, json.JSONDecodeError):
        return True
    blob = f"{type(exc).__name__} {exc}".lower()
    return any(
        tok in blob
        for tok in (
            "unterminated",
            "jsondecode",
            "expecting value",
            "invalid json",
            "parser fault",
            "json was not an object",
        )
    )


def _looks_unavailable(exc: BaseException) -> bool:
    blob = _exc_blob(exc)
    if _looks_quota(exc):
        return False
    return any(
        tok in blob
        for tok in ("503", "unavailable", "overloaded", "high demand", "capacity")
    )


def _looks_quota(exc: BaseException) -> bool:
    return is_quota_fault(exc)


def _looks_json_mode_unsupported(exc: BaseException) -> bool:
    blob = _exc_blob(exc)
    return any(
        tok in blob
        for tok in (
            "response_format",
            "json_object",
            "json mode",
            "json_schema",
            "not supported",
            "unsupported",
        )
    )


def is_quota_fault(value: Any) -> bool:
    """True for Bitget Qwen 429 / quota / rate-limit — not a crash."""
    if isinstance(value, BaseException):
        blob = _exc_blob(value)
    else:
        blob = str(value or "").lower()
    return any(
        tok in blob
        for tok in (
            "429",
            "quota",
            "rate limit",
            "rate_limit",
            "ratelimit",
            "throttling",
            "ratequota",
            "flow control",
            "resource exhausted",
            "resource_exhausted",
            "exceeded your current quota",
            "quota exceeded",
            "free_tier",
            "free tier",
            "insufficient_quota",
            "credits",
            "arrearage",
            "allocationquota",
        )
    )


def _exc_blob(exc: BaseException) -> str:
    parts = [type(exc).__name__, str(exc)]
    status = getattr(exc, "status_code", None)
    if status is not None:
        parts.append(str(status))
    code = getattr(exc, "code", None)
    if code is not None:
        parts.append(str(code))
    cause = getattr(exc, "__cause__", None)
    if isinstance(cause, BaseException):
        parts.append(type(cause).__name__)
        parts.append(str(cause))
    return " ".join(parts).lower()


def _should_stand_down(exc: BaseException) -> bool:
    return (
        _looks_quota(exc)
        or _looks_timeout(exc)
        or _looks_parse_fault(exc)
        or _looks_unavailable(exc)
    )


def _run_with_timeout(fn: Callable[[], T], *, timeout: float, label: str) -> T:
    """SDK timeouts can be ignored. Daemon thread + join is the last line of defense."""
    box: dict[str, Any] = {}

    def _worker() -> None:
        try:
            box["ok"] = fn()
        except Exception as exc:  # noqa: BLE001 — surface the real Qwen proxy fault
            box["err"] = exc

    thread = threading.Thread(target=_worker, daemon=True, name="qwen-timeout")
    thread.start()
    thread.join(timeout=timeout)
    if thread.is_alive():
        raise TimeoutError(f"{label} timed out after {timeout}s")
    if "err" in box:
        raise box["err"]
    if "ok" not in box:
        raise TimeoutError(f"{label} timed out after {timeout}s")
    return box["ok"]


class QwenCortex:
    """Thin adapter over the OpenAI SDK pointed at the Bitget hackathon Qwen proxy."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.selected_model = normalize_model_name(
            settings.resolved_qwen_model or settings.qwen_model or DEFAULT_QWEN_MODEL
        ) or DEFAULT_QWEN_MODEL
        self.model_name = self.selected_model
        self.backend: str = "none"
        self.last_error: str | None = None
        self._client: Any = None
        self._bind()

    def _bind(self) -> None:
        key = (self.settings.qwen_api_key or "").strip()
        if not key:
            self.backend = "offline"
            print(
                f"[CORTEX] QWEN_API_KEY empty — {CORTEX_BACKEND_LABEL} offline",
                flush=True,
            )
            return
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise RuntimeError(
                f"Install openai (pip install openai) to call {CORTEX_BACKEND_LABEL}."
            ) from exc
        try:
            self._client = OpenAI(
                api_key=key,
                base_url=QWEN_BASE_URL,
                timeout=float(LLM_TIMEOUT_S),
                max_retries=0,
            )
        except Exception as exc:  # noqa: BLE001 — never crash the CIC on bind
            self._client = None
            self.backend = "offline"
            self.last_error = _clean_api_error(exc)
            print(
                f"[API ERROR] {CORTEX_BACKEND_LABEL} bind failed — standing down: {self.last_error}",
                flush=True,
            )
            return
        self.backend = CORTEX_BACKEND_LABEL
        print(
            f"[CORTEX] running on {CORTEX_BACKEND_LABEL} · {self.selected_model} "
            "· Responses API · thinking off",
            flush=True,
        )

    def generate_json(
        self,
        system: str,
        user: str,
        temperature: float = 0.3,
        fallback: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Return (payload, degraded). Degraded means local fallback was used."""
        self.last_error = None
        try:
            text = self._complete(system, user, temperature=temperature, json_mode=True)
        except Exception as exc:
            self.last_error = _clean_api_error(exc)
            if _looks_quota(exc):
                print(f"[API ERROR] {API_QUOTA_VETO}", flush=True)
            else:
                _api_error(exc)
            if fallback is None:
                raise
            return _timeout_fallback(fallback, exc), True
        try:
            return _parse_json(text), False
        except (json.JSONDecodeError, ValueError, TypeError) as exc:
            self.last_error = "parser fault: unusable Qwen JSON"
            print(
                "[API ERROR] parser fault — Qwen JSON unusable (truncated/503). Standing down.",
                flush=True,
            )
            if fallback is None:
                raise
            return _timeout_fallback(fallback, exc), True

    def generate_text(self, system: str, user: str, temperature: float = 0.3) -> str:
        self.last_error = None
        try:
            return self._complete(system, user, temperature=temperature, json_mode=False)
        except Exception as exc:
            self.last_error = _clean_api_error(exc)
            _api_error(exc)
            if _looks_quota(exc):
                return API_QUOTA_VETO
            return API_TIMEOUT_VETO

    def _complete(self, system: str, user: str, temperature: float, json_mode: bool) -> str:
        if self.backend == "offline" or self._client is None:
            raise RuntimeError("QWEN_API_KEY is empty")

        names = _candidate_models(self.model_name)
        last_error: Exception | None = None
        for index, name in enumerate(names):
            label = f"qwen:{name}"
            try:
                text = call_with_backoff(
                    lambda n=name, lab=label: _run_with_timeout(
                        lambda: self._complete_once(n, system, user, temperature, json_mode),
                        timeout=LLM_TIMEOUT_S,
                        label=lab,
                    ),
                    attempts=3 if index == 0 else 1,
                    base_delay=0.4,
                    max_delay=1.5,
                    label=label,
                )
                self.model_name = normalize_model_name(name) or name
                return text
            except Exception as exc:
                last_error = exc
                _api_error(exc)
                if _looks_timeout(exc) or _looks_quota(exc):
                    break
                continue
        if last_error is not None and _looks_quota(last_error):
            raise last_error
        raise TimeoutError(f"{API_TIMEOUT_VETO}: {last_error}") from last_error

    def _complete_once(
        self,
        name: str,
        system: str,
        user: str,
        temperature: float,
        json_mode: bool,
    ) -> str:
        # Official Bitget hackathon proxy is the OpenAI Responses wire
        # (Codex config: wire_api = "responses"). qwen3.8-max thinks by
        # default; thinking on a board JSON prompt blows past 45s and the
        # desk stands down. Disable thinking on every path.
        try:
            return self._complete_via_responses(name, system, user, temperature, json_mode)
        except Exception as exc:
            if _looks_timeout(exc) or _looks_quota(exc):
                raise
            return self._complete_via_chat(name, system, user, temperature, json_mode)

    def _complete_via_responses(
        self,
        name: str,
        system: str,
        user: str,
        temperature: float,
        json_mode: bool,
    ) -> str:
        kwargs: dict[str, Any] = {
            "model": name,
            "input": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "max_output_tokens": 2048,
            "reasoning": {"effort": "none"},
            "extra_body": {"enable_thinking": False},
        }
        if json_mode:
            kwargs["text"] = {"format": {"type": "json_object"}}
        try:
            response = self._client.responses.create(**kwargs)
        except Exception as exc:
            if not json_mode or not _looks_json_mode_unsupported(exc):
                raise
            kwargs.pop("text", None)
            response = self._client.responses.create(**kwargs)
        return _response_text(response)

    def _complete_via_chat(
        self,
        name: str,
        system: str,
        user: str,
        temperature: float,
        json_mode: bool,
    ) -> str:
        kwargs: dict[str, Any] = {
            "model": name,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "max_tokens": 2048,
            "extra_body": {"enable_thinking": False},
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        try:
            response = self._client.chat.completions.create(**kwargs)
        except Exception as exc:
            if not json_mode or not _looks_json_mode_unsupported(exc):
                raise
            kwargs.pop("response_format", None)
            response = self._client.chat.completions.create(**kwargs)
        return _response_text(response)


def _timeout_fallback(fallback: dict[str, Any], exc: BaseException) -> dict[str, Any]:
    out = dict(fallback)
    if not _should_stand_down(exc):
        return out
    reason = API_QUOTA_VETO if _looks_quota(exc) else API_TIMEOUT_VETO
    out["rationale"] = reason
    if "thesis" in out:
        out["thesis"] = reason
    if "verdict" in out:
        out["verdict"] = "VETO"
    if "conviction" in out:
        out["conviction"] = 0
    if "primary_symbol" in out:
        out["primary_symbol"] = "NONE"
    if "side" in out:
        out["side"] = "none"
    if "action" in out:
        out["action"] = "STAND_DOWN"
    if "consensus" in out:
        out["consensus"] = "DEGRADED"
    if "fake_news_risk" in out:
        out["fake_news_risk"] = "HIGH"
    flags = out.get("black_swan_flags")
    if isinstance(flags, list) and reason not in flags:
        out["black_swan_flags"] = [reason, *flags][:8]
    return out


def _candidate_models(primary: str) -> list[str]:
    ordered: list[str] = []
    for name in (primary, DEFAULT_QWEN_MODEL):
        cleaned = normalize_model_name(name)
        if cleaned and cleaned not in ordered:
            ordered.append(cleaned)
    return ordered or [DEFAULT_QWEN_MODEL]


def _message_text(message: Any) -> str:
    if message is None:
        return ""
    content = getattr(message, "content", None)
    if content is None and isinstance(message, dict):
        content = message.get("content")
    if isinstance(content, str) and content.strip():
        return content
    if isinstance(content, list):
        chunks: list[str] = []
        for part in content:
            if isinstance(part, str) and part.strip():
                chunks.append(part)
                continue
            if isinstance(part, dict):
                text = part.get("text") or part.get("content") or ""
                if text:
                    chunks.append(str(text))
                continue
            text = getattr(part, "text", None) or getattr(part, "content", None)
            if text:
                chunks.append(str(text))
        if chunks:
            return "\n".join(chunks)
    parsed = getattr(message, "parsed", None)
    if isinstance(parsed, dict):
        return json.dumps(parsed)
    if isinstance(message, dict) and isinstance(message.get("parsed"), dict):
        return json.dumps(message["parsed"])
    return str(content or "").strip()


def _response_text(response: Any) -> str:
    choices = getattr(response, "choices", None)
    if choices is None and isinstance(response, dict):
        choices = response.get("choices")
    for choice in choices or []:
        message = getattr(choice, "message", None)
        if message is None and isinstance(choice, dict):
            message = choice.get("message")
        text = _message_text(message)
        if text:
            return text
    text = getattr(response, "output_text", None)
    if callable(text):
        try:
            text = text()
        except Exception:
            text = None
    if isinstance(text, str) and text.strip():
        return text
    output = getattr(response, "output", None)
    if output is None and isinstance(response, dict):
        output = response.get("output")
    for item in output or []:
        itype = ""
        if isinstance(item, dict):
            itype = str(item.get("type") or "")
        else:
            itype = str(getattr(item, "type", "") or type(item).__name__)
        if "reasoning" in itype.lower():
            continue
        text = _message_text(item)
        if text:
            return text
    raise RuntimeError("empty Qwen response")


def _parse_json(text: str) -> dict[str, Any]:
    cleaned = _FENCE.sub("", (text or "").strip()).strip()
    if not cleaned:
        raise ValueError("parser fault: empty Qwen JSON")
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start < 0 or end <= start:
            raise json.JSONDecodeError("parser fault: unterminated Qwen JSON", cleaned, 0)
        try:
            payload = json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError:
            raise json.JSONDecodeError(
                "parser fault: unterminated Qwen JSON",
                cleaned,
                0,
            ) from None
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ValueError("Qwen JSON was not an object") from exc
    if not isinstance(payload, dict):
        raise ValueError("Qwen JSON was not an object")
    return payload
