"""hub 通道基线/混淆与账号构建版本的一致性。

约束：
- 基线只随仓库分发，缺失版本必须回退，不能在线上生成不可控 system；
- 同版本时替换必须是零开销的相等判断，不能复制请求；
- 前缀替换不得修改调用方传入的 request。
"""

from pathlib import Path

import pytest

import src.converter.antigravity_system_prompt as asp


@pytest.fixture(autouse=True)
def _clear_cache():
    asp._baseline_for_version.cache_clear()
    yield
    asp._baseline_for_version.cache_clear()


def test_missing_version_falls_back_to_bundled_baseline():
    default = asp.baseline_prompt(asp.DEFAULT_PROMPT_VERSION)
    assert asp.baseline_prompt('9.9.9') == default
    assert asp.baseline_prompt('2.31.0') == default


def test_version_specific_baseline_is_used_when_shipped(tmp_path, monkeypatch):
    (tmp_path / f'antigravity-{asp.DEFAULT_PROMPT_VERSION}.txt').write_text('DEFAULT-BASELINE', encoding='utf-8')
    (tmp_path / 'antigravity-9.9.9.txt').write_text('V999-BASELINE', encoding='utf-8')
    monkeypatch.setattr(asp, 'PROMPT_DIR', Path(tmp_path))
    asp._baseline_for_version.cache_clear()

    assert asp.baseline_prompt('9.9.9') == 'V999-BASELINE'
    assert asp.baseline_prompt() in ('DEFAULT-BASELINE', 'V999-BASELINE')


def test_same_version_is_a_no_op_and_returns_same_object():
    request = {'systemInstruction': {'parts': [{'text': asp.baseline_prompt() + '\n\n' + asp.BRIDGE}]}}
    resolved = asp.resolve_pending_baseline(request, asp.DEFAULT_PROMPT_VERSION)
    assert resolved is request


def test_prefix_is_swapped_for_other_version(tmp_path, monkeypatch):
    (tmp_path / f'antigravity-{asp.DEFAULT_PROMPT_VERSION}.txt').write_text('DEFAULT-BASELINE', encoding='utf-8')
    (tmp_path / 'antigravity-9.9.9.txt').write_text('V999-BASELINE', encoding='utf-8')
    monkeypatch.setattr(asp, 'PROMPT_DIR', Path(tmp_path))
    asp._baseline_for_version.cache_clear()

    original_text = asp.baseline_prompt() + '\n\n' + asp.BRIDGE + 'TAIL'
    request = {'systemInstruction': {'parts': [{'text': original_text}, {'text': 'second'}]}}

    resolved = asp.resolve_pending_baseline(request, '9.9.9')
    parts = resolved['systemInstruction']['parts']
    assert parts[0]['text'] == 'V999-BASELINE' + '\n\n' + asp.BRIDGE + 'TAIL'
    assert parts[1] == {'text': 'second'}
    # 调用方请求不得被就地修改
    assert request['systemInstruction']['parts'][0]['text'] == original_text


def test_request_without_injected_baseline_is_untouched():
    request = {'systemInstruction': {'parts': [{'text': 'caller only'}]}}
    assert asp.resolve_pending_baseline(request, '9.9.9') is request
    assert request['systemInstruction']['parts'][0]['text'] == 'caller only'


def test_non_dict_system_instruction_is_untouched():
    assert asp.resolve_pending_baseline({'systemInstruction': 'plain'}, '9.9.9') == {'systemInstruction': 'plain'}
    assert asp.resolve_pending_baseline({}, '9.9.9') == {}


@pytest.mark.asyncio
async def test_hub_injection_keeps_baseline_and_obfuscation_together(monkeypatch):
    """hub 通道：注入基线 + 保护式混淆同时生效，且基线仍是原文。"""
    import config

    async def fake_mode():
        return 'protected'

    async def fake_enabled():
        return True

    async def fake_as_user():
        return False

    monkeypatch.setattr(config, 'get_antigravity_system_obfuscation_mode', fake_mode)
    monkeypatch.setattr(config, 'get_antigravity_system_prompt_enabled', fake_enabled)
    monkeypatch.setattr(config, 'get_antigravity_system_as_user_enabled', fake_as_user)

    request = {
        'model': 'gemini-2.5-flash-lite',
        'systemInstruction': {'parts': [{'text': 'You are a keyword screening assistant.'}]},
        'contents': [{'role': 'user', 'parts': [{'text': 'screen these'}]}],
    }
    prepared = await asp.prepare_antigravity_system_prompt(request)
    parts = prepared['systemInstruction']['parts']
    assert parts[0]['text'].startswith(asp.baseline_prompt().partition('\n')[0])
    # 基线之后紧跟 BRIDGE，说明基线未参与混淆
    assert asp.BRIDGE in parts[0]['text']
    # 调用方文本被混淆（不再是原文）
    assert all(p.get('text') != 'You are a keyword screening assistant.' for p in parts[1:])
