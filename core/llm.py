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
GEMINI_TIMEOUT_S = 15
GEMINI_TIMEOUT_MS = GEMINI_TIMEOUT_S * 1000
API_TIMEOUT_VETO = "VETO: API Timeout"

T = TypeVar("T")


def _api_error(exc: BaseException) -> None:
    print(f"[API ERROR] {str(exc)}", flush=True)


def _looks_timeout(exc: BaseException) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    blob = f"{type(exc).__name__} {exc}".lower()
    return any(tok in blob for tok in ("timeout", "timed out", "deadline", "read timed out"))


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
            return _parse_json(text), False
        except Exception as exc:
            self.last_error = str(exc)
            _api_error(exc)
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
                # A hung/timed-out primary will hang every fallback model too — exit.
                if _looks_timeout(exc):
                    break
                continue
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
                # Older stubs omit request_options; the thread cap still enforces 15s.
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
    if not _looks_timeout(exc):
        return out
    out["rationale"] = API_TIMEOUT_VETO
    if "thesis" in out:
        out["thesis"] = API_TIMEOUT_VETO
    if "verdict" in out:
        out["verdict"] = "VETO"
    if "conviction" in out:
        out["conviction"] = 0
    if "action" in out:
        out["action"] = "STAND_DOWN"
    if "consensus" in out:
        out["consensus"] = "DEGRADED"
    if "fake_news_risk" in out:
        out["fake_news_risk"] = "HIGH"
    flags = out.get("black_swan_flags")
    if isinstance(flags, list) and API_TIMEOUT_VETO not in flags:
        out["black_swan_flags"] = [API_TIMEOUT_VETO, *flags][:8]
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
    cleaned = _FENCE.sub("", text.strip()).strip()
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start < 0 or end <= start:
            raise
        payload = json.loads(cleaned[start : end + 1])
    if not isinstance(payload, dict):
        raise ValueError("Gemini JSON was not an object")
    return payload
