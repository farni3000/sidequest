import httpx
import respx

from app.ai import deepseek
from app.ai.deepseek import (
    ModelInfo,
    choose_effort,
    parse_models,
    rank_models,
)
from app.config import Settings


def _catalog() -> list[ModelInfo]:
    return parse_models(
        {
            "data": [
                {
                    "id": "deepseek-v4-pro",
                    "name": "DeepSeek-V4-Pro",
                    "context_window": 1_048_576,
                    "input_modalities": ["text"],
                    "effort": {"supported_levels": ["low", "high", "max"], "default_level": "high"},
                },
                {
                    "id": "deepseek-flash",
                    "name": "DeepSeek-V4.1-Flash",
                    "context_window": 1_048_576,
                    "input_modalities": ["text", "image"],
                    "effort": {"supported_levels": ["low", "high", "max"], "default_level": "high"},
                },
                {
                    "id": "deepseek-lite",
                    "name": "DeepSeek-V4-Lite",
                    "context_window": 128_000,
                    "input_modalities": ["text"],
                    "effort": {"supported_levels": ["low", "high"], "default_level": "high"},
                },
            ]
        }
    )


def test_parse_models_keeps_capabilities() -> None:
    models = _catalog()
    flash = next(model for model in models if model.id == "deepseek-flash")
    assert flash.supports_images
    assert flash.version == (4, 1)
    assert flash.tier == 1
    assert flash.supports_max_effort


def test_best_text_model_is_newest_version_at_max_effort() -> None:
    ranked = rank_models(_catalog(), require_image=False)
    assert ranked[0].id == "deepseek-flash"
    assert choose_effort(ranked[0]) == "max"


def test_vision_ranking_only_returns_image_models() -> None:
    ranked = rank_models(_catalog(), require_image=True)
    assert [model.id for model in ranked] == ["deepseek-flash"]


def test_choose_effort_honours_available_levels() -> None:
    lite = next(model for model in _catalog() if model.id == "deepseek-lite")
    assert choose_effort(lite) == "high"
    assert choose_effort(lite, "max") == "high"


def test_settings_fallbacks_are_not_pins() -> None:
    settings = Settings(_env_file=None, deepseek_model="deepseek-chat")
    assert settings.deepseek_model == "deepseek-chat"
    assert settings.route_review_enabled is True
    assert settings.route_review_max_rounds == 3


async def test_fetch_models_is_cached() -> None:
    deepseek.reset_model_cache()
    settings = Settings(
        _env_file=None,
        deepseek_api_key="test-key",
        deepseek_base_url="https://ds.test",
        deepseek_model_cache_ttl_s=3600,
    )
    payload = {
        "object": "list",
        "data": [
            {
                "id": "deepseek-flash",
                "name": "DeepSeek-V4.1-Flash",
                "context_window": 1_048_576,
                "input_modalities": ["text", "image"],
                "effort": {"supported_levels": ["low", "high", "max"], "default_level": "high"},
            }
        ],
    }
    try:
        with respx.mock:
            route = respx.get("https://ds.test/models").mock(
                return_value=httpx.Response(200, json=payload)
            )
            first = await deepseek.fetch_models(settings)
            second = await deepseek.fetch_models(settings)
            assert [model.id for model in first] == ["deepseek-flash"]
            assert second == first
            assert route.call_count == 1

        selection = await deepseek.resolve_model(settings, require_image=True)
        assert selection.model.id == "deepseek-flash"
        assert selection.effort == "max"
    finally:
        deepseek.reset_model_cache()


async def test_fetch_models_falls_back_when_unreachable() -> None:
    deepseek.reset_model_cache()
    settings = Settings(
        _env_file=None,
        deepseek_api_key="test-key",
        deepseek_base_url="https://ds.test",
        deepseek_model="deepseek-chat",
    )
    try:
        with respx.mock:
            respx.get("https://ds.test/models").mock(side_effect=httpx.ConnectError("down"))
            models = await deepseek.fetch_models(settings)
        assert models[0].id == "deepseek-chat"
        assert models[0].input_modalities == ("text",)
    finally:
        deepseek.reset_model_cache()
