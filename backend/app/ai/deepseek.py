"""Dynamic DeepSeek model discovery and JSON chat completions.

The public ``/models`` endpoint exposes capabilities (input modalities, reasoning
effort levels, context window). We use that metadata instead of hard-coding a
model id, so the app automatically moves to newer/better models as they appear.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..config import Settings

_VERSION_RE = re.compile(r"[Vv](\d+(?:\.\d+)*)")
_EFFORT_ORDER = ("max", "high", "medium", "low")


class DeepSeekError(RuntimeError):
    """Raised when a DeepSeek request cannot be completed."""


@dataclass(frozen=True)
class ModelInfo:
    id: str
    name: str = ""
    context_window: int = 0
    input_modalities: tuple[str, ...] = ("text",)
    effort_levels: tuple[str, ...] = ()
    default_effort: str | None = None

    @property
    def supports_images(self) -> bool:
        return "image" in self.input_modalities

    @property
    def supports_max_effort(self) -> bool:
        return "max" in self.effort_levels

    @property
    def version(self) -> tuple[int, ...]:
        match = _VERSION_RE.search(self.name) or _VERSION_RE.search(self.id)
        if not match:
            return ()
        return tuple(int(part) for part in match.group(1).split("."))

    @property
    def tier(self) -> int:
        haystack = f"{self.name} {self.id}".lower()
        if "pro" in haystack:
            return 3
        if "max" in haystack:
            return 2
        if "flash" in haystack:
            return 1
        return 0


@dataclass(frozen=True)
class ModelSelection:
    model: ModelInfo
    effort: str
    discovered: bool = True


@dataclass
class JsonCompletion:
    data: dict[str, Any]
    model: str
    effort: str
    raw: str
    selection: ModelSelection = field(repr=False, default=None)  # type: ignore[assignment]


_CACHE_LOCK = asyncio.Lock()
_MODEL_CACHE: dict[str, tuple[float, list[ModelInfo]]] = {}


def reset_model_cache() -> None:
    """Clear the process-wide model cache (mainly for tests)."""
    _MODEL_CACHE.clear()


def _as_str_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(item) for item in value if isinstance(item, (str, int)))


def parse_models(payload: Any) -> list[ModelInfo]:
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return []

    models: list[ModelInfo] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        model_id = entry.get("id")
        if not isinstance(model_id, str) or not model_id:
            continue
        effort = entry.get("effort")
        levels: tuple[str, ...] = ()
        default_effort: str | None = None
        if isinstance(effort, dict):
            levels = _as_str_tuple(effort.get("supported_levels"))
            raw_default = effort.get("default_level")
            default_effort = raw_default if isinstance(raw_default, str) else None
        models.append(
            ModelInfo(
                id=model_id,
                name=str(entry.get("name") or model_id),
                context_window=int(entry.get("context_window") or 0),
                input_modalities=_as_str_tuple(entry.get("input_modalities")) or ("text",),
                effort_levels=levels,
                default_effort=default_effort,
            )
        )
    return models


def rank_models(models: list[ModelInfo], *, require_image: bool = False) -> list[ModelInfo]:
    """Best first: max reasoning, newest version, highest tier, largest context."""
    candidates = [model for model in models if not require_image or model.supports_images]
    return sorted(
        candidates,
        key=lambda model: (
            model.supports_max_effort,
            model.version,
            model.tier,
            model.context_window,
            model.id,
        ),
        reverse=True,
    )


def choose_effort(model: ModelInfo, requested: str | None = None) -> str:
    levels = [level for level in _EFFORT_ORDER if level in model.effort_levels]
    if requested and requested in levels:
        return requested
    if levels:
        return levels[0]
    if requested:
        return requested
    return model.default_effort or "high"


def _fallback_models(settings: Settings, require_image: bool) -> list[ModelInfo]:
    modalities: tuple[str, ...]
    if require_image:
        model_id = settings.deepseek_vision_model or "deepseek-flash"
        name = "DeepSeek Vision (fallback)"
        modalities = ("text", "image")
    else:
        model_id = settings.deepseek_model or "deepseek-flash"
        name = "DeepSeek (fallback)"
        modalities = ("text",)
    return [
        ModelInfo(
            id=model_id,
            name=name,
            context_window=128_000,
            input_modalities=modalities,
            effort_levels=("max", "high", "low"),
        )
    ]


async def fetch_models(settings: Settings, *, force: bool = False) -> list[ModelInfo]:
    """Return the provider's model catalog, cached for a configurable TTL."""
    cache_key = settings.deepseek_base_url.rstrip("/")
    now = time.monotonic()
    cached = _MODEL_CACHE.get(cache_key)
    if cached and not force and now - cached[0] < settings.deepseek_model_cache_ttl_s:
        return cached[1]

    if not settings.has_deepseek:
        return _fallback_models(settings, require_image=False)

    async with _CACHE_LOCK:
        cached = _MODEL_CACHE.get(cache_key)
        if cached and not force and now - cached[0] < settings.deepseek_model_cache_ttl_s:
            return cached[1]

        url = f"{settings.deepseek_base_url.rstrip('/')}/models"
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=10.0)) as client:
                response = await client.get(
                    url,
                    headers={
                        "Authorization": f"Bearer {settings.deepseek_api_key}",
                        "Accept": "application/json",
                    },
                )
                response.raise_for_status()
                models = parse_models(response.json())
        except Exception:
            # Keep serving a stale catalog if we have one; discovery must never
            # stop route planning.
            return cached[1] if cached else _fallback_models(settings, require_image=False)

        if models:
            _MODEL_CACHE[cache_key] = (now, models)
            return models
        return cached[1] if cached else _fallback_models(settings, require_image=False)


async def resolve_model(
    settings: Settings, *, require_image: bool = False, force_refresh: bool = False
) -> ModelSelection:
    """Pick the best currently available model for the requested modality."""
    pinned = settings.deepseek_vision_model if require_image else None
    models = await fetch_models(settings, force=force_refresh)
    if pinned:
        for model in models:
            if model.id == pinned:
                return ModelSelection(model=model, effort=choose_effort(model, settings.deepseek_effort))

    ranked = rank_models(models, require_image=require_image)
    if ranked:
        model = ranked[0]
        return ModelSelection(
            model=model,
            effort=choose_effort(model, settings.deepseek_effort),
            discovered=True,
        )

    fallback = _fallback_models(settings, require_image)[0]
    return ModelSelection(
        model=fallback,
        effort=choose_effort(fallback, settings.deepseek_effort),
        discovered=False,
    )


def _strip_code_fence(content: str) -> str:
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


async def complete_json(
    settings: Settings,
    messages: list[dict[str, Any]],
    *,
    require_image: bool = False,
    temperature: float = 0.2,
    timeout_s: float | None = None,
) -> JsonCompletion:
    """Send a chat completion in JSON mode with the highest reasoning effort."""
    if not settings.has_deepseek:
        raise DeepSeekError("DEEPSEEK_API_KEY fehlt.")

    selection = await resolve_model(settings, require_image=require_image)
    payload = {
        "model": selection.model.id,
        "messages": messages,
        "response_format": {"type": "json_object"},
        "reasoning_effort": selection.effort,
        "temperature": temperature,
        "stream": False,
    }
    url = f"{settings.deepseek_base_url.rstrip('/')}/chat/completions"
    timeout = timeout_s or settings.deepseek_timeout_s

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=20.0)
        ) as client:
            response = await client.post(
                url,
                headers={
                    "Authorization": f"Bearer {settings.deepseek_api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            response.raise_for_status()
            body = response.json()
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text[:300] if exc.response is not None else str(exc)
        raise DeepSeekError(f"DeepSeek API {exc.response.status_code}: {detail}") from exc
    except Exception as exc:  # noqa: BLE001 - callers provide graceful fallbacks
        raise DeepSeekError(f"DeepSeek nicht erreichbar: {exc}") from exc

    try:
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise DeepSeekError("DeepSeek-Antwort ohne Inhalt.") from exc
    if not isinstance(content, str):
        raise DeepSeekError("DeepSeek-Antwort ist kein Text.")

    raw = _strip_code_fence(content)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DeepSeekError("DeepSeek hat kein gültiges JSON geliefert.") from exc
    if not isinstance(data, dict):
        raise DeepSeekError("DeepSeek-JSON ist kein Objekt.")

    return JsonCompletion(
        data=data,
        model=selection.model.id,
        effort=selection.effort,
        raw=raw,
        selection=selection,
    )
