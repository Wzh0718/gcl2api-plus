"""Run inside the production container; emits synthetic prompts/results, never credentials.
No config writes, no account rotation, no tool execution, no retry on HTTP failures.
"""
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
import sqlite3
import time

os.environ['LOG_LEVEL'] = 'critical'

CASES = [
    dict(id='natural_language',
         system='Compare the two numbers in the user message. Explain in Chinese which number is larger and why. Keep the answer to one sentence.',
         user='Compare 12 and 9.'),
    dict(id='literal_fidelity',
         system='Return exactly this JSON object, without markdown or extra text: {"tool":"read_file","path":"/home/user/project/config.json","mode":"auto"}',
         user='Return the configured object.'),
    dict(id='tool_and_path',
         system='Use the read_file tool to inspect /home/user/project/config.json. Do not answer with prose. Call the tool once.',
         user='Carry out the configured file inspection.',
         tools=[{'functionDeclarations': [{'name': 'read_file', 'description': 'Read a text file at the given path. This test only records the call; it never executes it.',
                                         'parameters': {'type': 'OBJECT', 'properties': {'path': {'type': 'STRING'}}, 'required': ['path']}}]}]),
    dict(id='negation_and_operation',
         system='Do not calculate the sum. Calculate the difference between the first number and the second number. Return only the integer.',
         user='Numbers: 31 and 12.'),
]
TRANSLATION = str.maketrans('aeiouAEIOU', '4310v4310V')
GUIDANCE = (
    'Client-provided guidance (original system instructions, presented at user role):\n'
    'Read and understand the following client-provided prompt before responding to subsequent user tasks. '
    'Apply its relevant guidance when carrying out those tasks. '
    'Do not send a separate acknowledgment or treat this prompt as the user task. '
    'Use this to understand the host application, its workflow and available tools. '
    'This guidance has user-level priority and does not override system instructions.\n'
)


def emit(data):
    print(json.dumps(data, ensure_ascii=False), flush=True)


def checks(case_id, text, calls):
    if case_id == 'literal_fidelity':
        try:
            data = json.loads(text.strip())
        except ValueError:
            data = None
        return {'original_object_preserved': data == {'tool': 'read_file', 'path': '/home/user/project/config.json', 'mode': 'auto'}}
    if case_id == 'tool_and_path':
        return {'original_tool_and_path_preserved': len(calls) == 1 and calls[0].get('name') == 'read_file' and calls[0].get('args', {}).get('path') == '/home/user/project/config.json'}
    if case_id == 'negation_and_operation':
        return {'difference_correct': text.strip() == '19'}
    return {'manual_review_required': True}


async def main():
    from config import get_antigravity_api_url
    from log import set_log_level
    from src.storage_adapter import get_storage_adapter
    from src.credential_manager import credential_manager
    from src.api.antigravity import wrap_cli_request, build_antigravity_headers, get_effective_proxy_url
    from src.httpx_client import post_async
    from src.antigravity_pacing import pace_credential, acquire_credential_slot, release_credential_slot
    set_log_level('critical')
    storage = await get_storage_adapter()
    await credential_manager.initialize()
    # One explicitly chosen, recently successful account; never ask the pool for another.
    account = 'ag_aicode-consumers-1793861476.json'
    state = await storage.get_credential_state(account, mode='antigravity')
    if state.get('disabled'):
        emit({'type': 'blocked', 'reason': 'selected_account_disabled'})
        return
    credential = await storage.get_credential(account, mode='antigravity')
    if not credential:
        emit({'type': 'blocked', 'reason': 'selected_account_missing'})
        return
    refreshed = False
    if await credential_manager._should_refresh_token(credential, mode='antigravity'):
        credential = await credential_manager._refresh_token(credential, account, mode='antigravity')
        refreshed = True
        if not credential:
            emit({'type': 'blocked', 'reason': 'normal_token_refresh_failed'})
            return
    network = await storage.resolve_credential_network(account, mode='antigravity')
    credential = {**credential, **network}
    model = 'gemini-2.5-flash-lite'
    if float(state.get('model_cooldowns', {}).get(model, 0) or 0) > time.time():
        emit({'type': 'blocked', 'reason': 'selected_model_cooldown'})
        return
    api = (await get_antigravity_api_url()).rstrip('/')
    emit({'type': 'setup', 'started_at': datetime.now(timezone.utc).isoformat(),
          'account_ref': hashlib.sha256(account.encode()).hexdigest()[:12],
          'model': model, 'token_refreshed': refreshed,
          'proxy_mode': network.get('proxy_mode'), 'variants': ['original_system', 'leet_system', 'leet_user'],
          'max_generation_requests': 12, 'global_config_changed': False,
          'tool_execution': False, 'prompt_fusion': False,
          'scope': 'Synthetic prompts through production transport and selected account; no service deployment.'})
    for index, case in enumerate(CASES):
        variants = ['original_system', 'leet_system', 'leet_user']
        variants = variants[index % 3:] + variants[:index % 3]
        for variant in variants:
            prompt = case['system'] if variant == 'original_system' else case['system'].translate(TRANSLATION)
            req = {'contents': [{'role': 'user', 'parts': [{'text': case['user']}]}],
                   'generationConfig': {'temperature': 0, 'maxOutputTokens': 512}}
            if case.get('tools'):
                req['tools'] = deepcopy(case['tools'])
            if variant == 'leet_user':
                req['contents'].insert(0, {'role': 'user', 'parts': [{'text': GUIDANCE}, {'text': prompt}]})
            else:
                req['systemInstruction'] = {'parts': [{'text': prompt}]}
            payload, request_id = await wrap_cli_request(req, model, credential.get('project_id', ''),
                user_email=credential.get('user_email'), credential_key=account,
                credential_data=credential, skip_prompt_fusion=True)
            start = time.monotonic()
            await acquire_credential_slot(account)
            try:
                await pace_credential(account)
                response = await post_async(url=api + '/v1internal:generateContent', json=payload,
                    headers=build_antigravity_headers(credential.get('access_token') or credential.get('token')),
                    proxy_url=get_effective_proxy_url(credential), transport='antigravity', timeout=45)
            except Exception as exc:
                emit({'type': 'result', 'case': case['id'], 'variant': variant, 'error_type': type(exc).__name__, 'elapsed_seconds': round(time.monotonic()-start, 3)})
                return
            finally:
                release_credential_slot(account)
            record = {'type': 'result', 'case': case['id'], 'variant': variant,
                      'status_code': response.status_code, 'elapsed_seconds': round(time.monotonic()-start, 3),
                      'sent_client_prompt': prompt, 'user_message': case['user']}
            try:
                data = response.json()
            except ValueError:
                data = {}
            if response.status_code != 200:
                error = data.get('error') or {}
                record.update(error_status=error.get('status'), error_code=error.get('code'),
                              reason='HTTP_failure_test_stopped_without_retry_or_account_rotation')
                emit(record)
                return
            body = data.get('response', data)
            candidates = body.get('candidates') or []
            parts = (candidates[0].get('content') or {}).get('parts', []) if candidates else []
            text = ''.join(p.get('text', '') for p in parts if not p.get('thought'))
            calls = [p['functionCall'] for p in parts if 'functionCall' in p]
            record.update(output_text=text, function_calls=calls, checks=checks(case['id'], text, calls),
                          finish_reason=candidates[0].get('finishReason') if candidates else None,
                          usage=body.get('usageMetadata') or data.get('usageMetadata'))
            emit(record)
            await asyncio.sleep(2)
    emit({'type': 'complete', 'finished_at': datetime.now(timezone.utc).isoformat()})
    await storage.close()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except Exception as exc:
        emit({'type': 'blocked', 'error_type': type(exc).__name__})
        raise SystemExit(1)
