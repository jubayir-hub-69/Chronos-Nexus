"""Gemini cortex used by the Board of Directors."""

from __future__ import annotations

import json
import re
import threading
from typing import Any, Callable, TypeVar

from core.config import Settings, _FALLBACK_MODELS, _normalize_model_name
from core.retry import call_with_backoff

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE | re.DOTALL)

# Hard cap per HTTP round-trip. Never let generate_content block the CIC.
GEMINI_TIMEOUT_S = 45
GEMINI_TIMEOUT_MS = GEMINI_TIMEOUT_S * 1000
API_TIMEOUT_VETO = "VETO: API Timeout"
API_QUOTA_VETO = (
    "Gemini Free Tier API quota reached. System safely standing down until limits reset."
)

T = TypeVar("T")


def _api_error(exc: BaseException) -> None:
    print(f"[API ERROR] {_clean_api_error(exc)}", flush=True)


def _clean_api_error(exc: BaseException) -> str:
    if _looks_quota(exc):
        return API_QUOTA_VETO
    if _looks_parse_fault(exc):
        return "parser fault: unusable Gemini JSON"
    if _looks_unavailable(exc):
        return "Gemini 503/unavailable — standing down"
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


def is_quota_fault(value: Any) -> bool:
    """True for Gemini Free Tier 429 / quota / resource-exhausted — not a crash."""
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
            "resource exhausted",
            "resource_exhausted",
            "exceeded your current quota",
            "quota exceeded",
            "free_tier",
            "free tier",
        )
    )


def _exc_blob(exc: BaseException) -> str:
    parts = [type(exc).__name__, str(exc)]
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
        except Exception as exc:  # noqa: BLE001 — surface the real Gemini fault
            box["err"] = exc

    thread = threading.Thread(target=_worker, daemon=True, name="gemini-timeout")
    thread.start()
    thread.join(timeout=timeout)
    if thread.is_alive():
        raise TimeoutError(f"{label} timed out after {timeout}s")
    if "err" in box:
        raise box["err"]
    if "ok" not in box:
        raise TimeoutError(f"{label} timed out after {timeout}s")
    return box["ok"]


class GeminiCortex:
    """Thin adapter over google-generativeai (new google-genai as fallback)."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        # Exact auto-selector id (e.g. gemini-3.8-flash). Never a nickname like "1.5".
        self.selected_model = _normalize_model_name(
            settings.resolved_gemini_model or _FALLBACK_MODELS[0]
        )
        self.model_name = self.selected_model
        self.backend: str = "none"
        self.last_error: str | None = None
        self._legacy: Any = None
        self._client: Any = None
        self._bind()

    def _bind(self) -> None:
        key = self.settings.gemini_api_key
        if not key:
            self.backend = "offline"
            return
        try:
            from google import genai
            from google.genai import types

            try:
                http: Any = types.HttpOptions(
                    timeout=GEMINI_TIMEOUT_MS,
                    retry_options=types.HttpRetryOptions(attempts=1),
                )
            except Exception:
                http = {"timeout": GEMINI_TIMEOUT_MS}
            self._client = genai.Client(api_key=key, http_options=http)
            self.backend = "google-genai"
            return
        except ImportError:
            pass
        try:
            import google.generativeai as genai

            genai.configure(api_key=key)
            self._legacy = genai
            self.backend = "google-generativeai"
        except ImportError as exc:
            raise RuntimeError(
                "Install google-genai (pip install google-genai). "
                "google-generativeai is end-of-life."
            ) from exc

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
            self.last_error = "parser fault: unusable Gemini JSON"
            print(
                "[API ERROR] parser fault — Gemini JSON unusable (truncated/503). Standing down.",
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
            self.last_error = str(exc)
            _api_error(exc)
            if _looks_quota(exc):
                return API_QUOTA_VETO
            if _looks_timeout(exc):
                return API_TIMEOUT_VETO
            raise

    def _complete(self, system: str, user: str, temperature: float, json_mode: bool) -> str:
        if self.backend == "offline":
            raise RuntimeError("GEMINI_API_KEY is empty")

        names = _candidate_models(self.model_name)[:3]
        last_error: Exception | None = None
        for index, name in enumerate(names):
            label = f"gemini:{name}"
            try:
                text = call_with_backoff(
                    lambda n=name, lab=label: _run_with_timeout(
                        lambda: self._complete_once(n, system, user, temperature, json_mode),
                        timeout=GEMINI_TIMEOUT_S,
                        label=lab,
                    ),
                    attempts=3 if index == 0 else 1,
                    base_delay=0.4,
                    max_delay=1.5,
                    label=label,
                )
                self.model_name = _normalize_model_name(name)
                return text
            except Exception as exc:
                last_error = exc
                _api_error(exc)
                # Timeout and quota will burn every fallback model the same way — exit.
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
        if self.backend == "google-generativeai":
            return self._complete_legacy(name, system, user, temperature, json_mode)
        return self._complete_new(name, system, user, temperature, json_mode)

    def _complete_legacy(
        self,
        name: str,
        system: str,
        user: str,
        temperature: float,
        json_mode: bool,
    ) -> str:
        gen_cfg: dict[str, Any] = {"temperature": temperature, "max_output_tokens": 2048}
        if json_mode:
            gen_cfg["response_mime_type"] = "application/json"

        def _call(cfg: dict[str, Any]) -> str:
            model = self._legacy.GenerativeModel(
                model_name=name,
                system_instruction=system,
                generation_config=cfg,
            )
            try:
                response = model.generate_content(
                    user, request_options={"timeout": GEMINI_TIMEOUT_S}
                )
            except TypeError:
                # Older stubs omit request_options; the thread cap still enforces GEMINI_TIMEOUT_S.
                response = model.generate_content(user)
            return _response_text(response)

        try:
            return _call(gen_cfg)
        except Exception:
            if not json_mode:
                raise
            gen_cfg.pop("response_mime_type", None)
            return _call(gen_cfg)

    def _complete_new(
        self,
        name: str,
        system: str,
        user: str,
        temperature: float,
        json_mode: bool,
    ) -> str:
        from google.genai import types

        cfg_kwargs: dict[str, Any] = {
            "temperature": temperature,
            "max_output_tokens": 2048,
            "system_instruction": system,
            "http_options": types.HttpOptions(
                timeout=GEMINI_TIMEOUT_MS,
                retry_options=types.HttpRetryOptions(attempts=1),
            ),
        }
        try:
            cfg_kwargs["automatic_function_calling"] = types.AutomaticFunctionCallingConfig(disable=True)
        except Exception:
            pass
        if json_mode:
            cfg_kwargs["response_mime_type"] = "application/json"
        response = self._client.models.generate_content(
            model=name,
            contents=user,
            config=types.GenerateContentConfig(**cfg_kwargs),
        )
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
    ordered = [_normalize_model_name(primary)]
    for name in _FALLBACK_MODELS:
        if name not in ordered:
            ordered.append(name)
    return ordered


def _response_text(response: Any) -> str:
    text = getattr(response, "text", None)
    if text:
        return str(text)
    chunks: list[str] = []
    for candidate in getattr(response, "candidates", None) or []:
        content = getattr(candidate, "content", None)
        for part in getattr(content, "parts", None) or []:
            piece = getattr(part, "text", None)
            if piece:
                chunks.append(str(piece))
    if chunks:
        return "\n".join(chunks)
    raise RuntimeError("empty Gemini response")


def _parse_json(text: str) -> dict[str, Any]:
    cleaned = _FENCE.sub("", (text or "").strip()).strip()
    if not cleaned:
        raise ValueError("parser fault: empty Gemini JSON")
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start < 0 or end <= start:
            raise json.JSONDecodeError("parser fault: unterminated Gemini JSON", cleaned, 0)
        try:
            payload = json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError:
            raise json.JSONDecodeError(
                "parser fault: unterminated Gemini JSON",
                cleaned,
                0,
            ) from None
    if not isinstance(payload, dict):
        raise ValueError("Gemini JSON was not an object")
    return payload
