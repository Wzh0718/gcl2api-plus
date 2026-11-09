"""Advertised feature models must round-trip through request model parsing."""

import json
from unittest.mock import AsyncMock

import pytest

from src.router.antigravity import model_list
from src.converter.gemini_fix import map_antigravity_gemini_model
from src.utils import (
    get_base_model_from_feature_model,
    is_anti_truncation_model,
    is_fake_streaming_model,
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model_id",
    ["gemini-2.5-flash", "gemini-2.5-pro", "gemini-3.1-pro-low", "claude-sonnet-4-6"],
)
@pytest.mark.parametrize("format_name", ["openai", "gemini"])
async def test_advertised_feature_models_round_trip(monkeypatch, model_id, format_name):
    monkeypatch.setattr(
        model_list, "fetch_available_models", AsyncMock(return_value=[{"id": model_id}])
    )

    if format_name == "openai":
        response = await model_list.list_openai_models(principal=None)
        names = [model["id"] for model in json.loads(response.body)["data"]]
    else:
        response = await model_list.list_gemini_models(principal=None)
        names = [
            model["name"].removeprefix("models/")
            for model in json.loads(response.body)["models"]
        ]

    assert names == [model_id, f"假流式/{model_id}", f"流式抗截断/{model_id}"]
    assert all(get_base_model_from_feature_model(name) == model_id for name in names)
    assert [is_fake_streaming_model(name) for name in names] == [False, True, False]
    assert [is_anti_truncation_model(name) for name in names] == [False, False, True]


@pytest.mark.parametrize(
    "model_id",
    ["gemini-2.5-flash", "gemini-2.5-pro", "gemini-3.1-pro-low", "claude-sonnet-4-6"],
)
@pytest.mark.parametrize("prefix", ["", "假流式/", "流式抗截断/", "抗截断/"])
def test_feature_prefixes_preserve_base_model(model_id, prefix):
    name = prefix + model_id

    assert get_base_model_from_feature_model(name) == model_id
    assert is_anti_truncation_model(name) == (prefix in ("流式抗截断/", "抗截断/"))
    assert is_fake_streaming_model(name) == (prefix == "假流式/")


@pytest.mark.parametrize("model_id", ["gemini-2.5-flash", "gemini-3.1-pro", "gemini-3.1-pro-low"])
@pytest.mark.parametrize("prefix", ["流式抗截断/", "抗截断/"])
def test_antigravity_mapping_accepts_both_anti_truncation_prefixes(model_id, prefix):
    assert map_antigravity_gemini_model(prefix + model_id, None, None) == (
        map_antigravity_gemini_model(model_id, None, None)
    )
