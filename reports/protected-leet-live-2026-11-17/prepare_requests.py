"""Rebuild the recorded synthetic requests with the current local normalizer.
Run from repository root: PYTHONPATH=. .venv/bin/python reports/.../prepare_requests.py
"""
import asyncio
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent

async def main():
    import config
    from src.converter.gemini_fix import normalize_gemini_request
    config._config_initialized = True
    for key in ('ANTIGRAVITY_SYSTEM_OBFUSCATION_MODE', 'ANTIGRAVITY_SYSTEM_VOWEL_OBFUSCATION_ENABLED',
                'ANTIGRAVITY_SYSTEM_AS_USER_ENABLED', 'ANTIGRAVITY_SYSTEM_PROMPT_ENABLED', 'SYSTEM_PROMPT_APPEND'):
        os.environ.pop(key, None)
    os.environ['SENSITIVE_WORD_REPLACE_ENABLED'] = 'false'
    tasks = json.loads((ROOT/'requests.json').read_text())
    for task in tasks:
        original = task['request']
        config._config_cache = {'antigravity_system_obfuscation_mode': 'protected',
                               'antigravity_system_as_user_enabled': task['variant'] == 'protected_user',
                               'antigravity_system_prompt_enabled': False, 'system_prompt_append': '',
                               'sensitive_word_replace_enabled': False}
        request = {'model': 'gemini-2.5-flash-lite', 'systemInstruction': {'parts': [{'text': task['original_system']}]},
                   'contents': [deepcopy(original['contents'][-1])], 'generationConfig': {'temperature': 0, 'maxOutputTokens': 512}}
        if 'tools' in original:
            request['tools'] = deepcopy(original['tools'])
        normalized = await normalize_gemini_request(request, mode='antigravity')
        normalized.pop('model', None)
        task['request'] = dict(normalized)
    (ROOT/'requests.json').write_text(json.dumps(tasks, ensure_ascii=False, indent=2)+'\n')
    source = (ROOT/'probe.py').read_text()
    head, tail = source.split('\nTASKS = ', 1)
    tail = tail[tail.index("\n\nif __name__ == '__main__':"):]
    (ROOT/'probe.py').write_text(head+'\nTASKS = '+repr(tasks)+tail)
    paths = ['src/converter/protected_leet.py', 'src/converter/antigravity_system_prompt.py', 'src/converter/gemini_fix.py']
    (ROOT/'source-sha256.json').write_text(json.dumps({p: hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in paths}, indent=2)+'\n')
    print('Prepared', len(tasks), 'requests')

if __name__ == '__main__':
    asyncio.run(main())
