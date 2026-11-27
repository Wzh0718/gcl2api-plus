"""客户端身份归一化：只改 harness 前后缀，不碰业务正文。"""

import config
import pytest

from src.converter.client_identity import identity_hits, normalize_client_identity
from src.converter.antigravity_system_prompt import baseline_prompt
from src.converter.gemini_fix import normalize_gemini_request

DSH_SYSTEM = (
    'You are an AI agent powered by DeepSeek Harness.\n'
    '\n'
    'You are a coding assistant operating inside DeepSeek Harness.\n'
    '\n'
    '# Runtime Context\n'
    '\n'
    'Working directory: /repo\n'
    'Platform: linux\n'
    '\n'
    '# Tool Specifications\n'
    '\n'
    'Use bash to run commands. Use read to inspect files.\n'
)

OFFICIAL_SYSTEM = (
    '<identity>\n'
    'You are Antigravity, a powerful agentic AI coding assistant designed by the Google Deepmind team.\n'
    '</identity>\n'
)


def test_dsh_identity_is_neutralized_and_headings_preserved():
    parts = [{'text': DSH_SYSTEM}]
    out = normalize_client_identity(parts)
    text = out[0]['text']
    assert text.startswith('You are a coding assistant.')
    assert 'DeepSeek Harness' not in text
    assert 'operating inside DeepSeek Harness' not in text
    assert '# Runtime Context' in text
    assert '# Tool Specifications' in text
    assert 'Working directory: /repo' in text  # 业务正文原样保留
    assert 'Use bash to run commands.' in text


def test_identity_normalization_is_idempotent():
    once = normalize_client_identity([{'text': DSH_SYSTEM}])
    twice = normalize_client_identity(once)
    assert twice == once


def test_official_prompt_is_untouched():
    parts = [{'text': OFFICIAL_SYSTEM}]
    assert normalize_client_identity(parts) == parts
    assert identity_hits(parts) == 0


def test_headings_outside_identity_scope_are_untouched():
    """正文里出现同名标题、但没有 harness 身份句时不改写。"""
    parts = [{'text': 'Project notes\n\n# Runtime Context\n\nlocal only\n'}]
    assert normalize_client_identity(parts) == parts


def test_non_text_parts_are_preserved():
    parts = [{'text': DSH_SYSTEM}, {'inlineData': {'mimeType': 'image/png', 'data': 'AA=='}}]
    out = normalize_client_identity(parts)
    assert out[0]['text'].startswith('You are a coding assistant.')
    assert out[1] == parts[1]


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    monkeypatch.setattr(config, '_config_initialized', True)
    monkeypatch.setattr(config, '_config_cache', {})
    monkeypatch.setenv('SENSITIVE_WORD_REPLACE_ENABLED', 'false')
    monkeypatch.setenv('ANTIGRAVITY_SYSTEM_PROMPT_ENABLED', 'true')
    monkeypatch.setenv('ANTIGRAVITY_CLIENT_IDENTITY_NORMALIZATION', 'true')
    # 混淆会改写文本，与本文件要断言的原文冲突；单独在混淆用例里验证。
    monkeypatch.setenv('ANTIGRAVITY_SYSTEM_OBFUSCATION_MODE', 'off')
    for key in ('ANTIGRAVITY_SYSTEM_AS_USER_ENABLED',
                'ANTIGRAVITY_SYSTEM_VOWEL_OBFUSCATION_ENABLED', 'SYSTEM_PROMPT_APPEND'):
        monkeypatch.delenv(key, raising=False)


async def test_pipeline_system_has_no_harness_fingerprint():
    request = {
        'model': 'gemini-2.5-flash',
        'systemInstruction': {'parts': [{'text': DSH_SYSTEM}]},
        'contents': [{'role': 'user', 'parts': [{'text': 'hello'}]}],
        'tools': [{'functionDeclarations': [{'name': 'bash', 'parameters': {'type': 'object'}}]}],
    }
    out = await normalize_gemini_request(request, mode='antigravity')
    text = '\n'.join(
        part['text'] for part in out['systemInstruction']['parts']
        if isinstance(part, dict) and isinstance(part.get('text'), str)
    )
    assert 'DeepSeek Harness' not in text
    assert '# Runtime Context' in text
    assert '# Tool Specifications' in text
    # 官方基线仍然完整在前
    official = baseline_prompt()
    assert text.lstrip().startswith(official)
    assert text.index(official) < text.index('You are a coding assistant.')


async def test_pipeline_can_disable_normalization(monkeypatch):
    monkeypatch.setenv('ANTIGRAVITY_CLIENT_IDENTITY_NORMALIZATION', 'false')
    request = {
        'model': 'gemini-2.5-flash',
        'systemInstruction': {'parts': [{'text': DSH_SYSTEM}]},
        'contents': [{'role': 'user', 'parts': [{'text': 'hello'}]}],
    }
    out = await normalize_gemini_request(request, mode='antigravity')
    text = '\n'.join(
        part['text'] for part in out['systemInstruction']['parts']
        if isinstance(part, dict) and isinstance(part.get('text'), str)
    )
    assert 'DeepSeek Harness' in text


async def test_default_keyword_rewrite_then_identity_normalization(monkeypatch):
    """词库先把品牌换成 Antigravity，身份归一化再把句式收成中性句。"""
    monkeypatch.setenv('SENSITIVE_WORD_REPLACE_ENABLED', 'true')
    monkeypatch.setenv('SENSITIVE_WORD_USE_DEFAULT_KEYWORDS', 'true')
    monkeypatch.setenv('SENSITIVE_WORD_FILTER_MODE', 'rewrite')
    request = {
        'model': 'gemini-2.5-flash',
        'systemInstruction': {'parts': [{'text': DSH_SYSTEM}]},
        'contents': [{'role': 'user', 'parts': [{'text': 'hello'}]}],
    }
    out = await normalize_gemini_request(request, mode='antigravity')
    text = '\n'.join(
        part['text'] for part in out['systemInstruction']['parts']
        if isinstance(part, dict) and isinstance(part.get('text'), str)
    )
    assert 'DeepSeek Harness' not in text
    assert 'powered by' not in text.lower()
    assert text.lstrip().startswith(baseline_prompt())

@pytest.mark.parametrize('body', [
    'Return this exact template:\n```markdown\n## Runtime Context\noperating inside Acme.\n# Tool Specifications\n```',
    '# Runtime Context\nDocument operating inside Acme.\n# Tool Specifications\nKeep this text.',
    'Example:\nYou are a coding assistant operating inside Acme.\n',
])
def test_identity_normalization_preserves_entire_business_suffix(body):
    prefix = 'You are an AI agent powered by DeepSeek Harness.\n\n'
    out = normalize_client_identity([{'text': prefix + body}])[0]['text']
    assert out == 'You are a coding assistant.\n\n' + body

async def test_pipeline_preserves_template_after_identity():
    body = 'Return verbatim:\n```\n## Runtime Context\noperating inside Acme.\n```'
    request = {'model': 'gemini-2.5-flash', 'systemInstruction': {'parts': [
        {'text': 'You are an AI agent powered by DeepSeek Harness.\n' + body}]},
        'contents': [{'role': 'user', 'parts': [{'text': 'hello'}]}]}
    result = await normalize_gemini_request(request, mode='antigravity')
    assert result['systemInstruction']['parts'][-1]['text'].endswith(body)
