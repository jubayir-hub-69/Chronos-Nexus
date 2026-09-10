"""Gemini cortex used by the Board of Directors."""

from __future__ import annotations

import json
import re
from typing import Any

from core.config import Settings, _FALLBACK_MODELS, _normalize_model_name

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE | re.DOTALL)


class GeminiCortex:
    """Thin adapter over google-generativeai (new google-genai as fallback)."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.model_name = settings.resolved_gemini_model or _FALLBACK_MODELS[0]
        self.backend: str = "none"
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

            self._client = genai.Client(api_key=key)
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
        try:
            text = self._complete(system, user, temperature=temperature, json_mode=True)
            return _parse_json(text), False
        except Exception:
            if fallback is not None:
                return fallback, True
            raise

    def generate_text(self, system: str, user: str, temperature: float = 0.3) -> str:
        return self._complete(system, user, temperature=temperature, json_mode=False)

    def _complete(self, system: str, user: str, temperature: float, json_mode: bool) -> str:
        if self.backend == "offline":
            raise RuntimeError("GEMINI_API_KEY is empty")

        names = _candidate_models(self.model_name)[:3]
        last_error: Exception | None = None
        for name in names:
            try:
                text = self._complete_once(name, system, user, temperature, json_mode)
                self.model_name = name
                return text
            except Exception as exc:
                last_error = exc
                continue
        raise RuntimeError(f"Gemini generate failed: {last_error}")

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
                response = model.generate_content(user, request_options={"timeout": 60})
            except TypeError:
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
