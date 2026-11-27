#!/usr/bin/env python3
"""客户端指纹对照：定位上游 429 RESOURCE_EXHAUSTED 到底由哪一维触发。

背景：同一把网关 key、同一账号下，codex 形态请求能过，DSH 形态请求 429。
变量只有两个：system 文本、tools 声明。这个脚本把两者拆开逐维跑一遍，
每个臂只改一个维度，输出状态码与上游错误正文。

用法：

    python3 scripts/diagnose_client_fingerprint.py \
        --base-url http://127.0.0.1:7861 --key <API_KEY> --model gemini-2.5-flash

只发最小生成请求（maxOutputTokens=16、temperature=0），不改任何配置。
结果同时打印成表并写入 --out 指定的 JSON（默认 ./client-fingerprint-<ts>.json）。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

DSH_SYSTEM = """You are an AI agent powered by DeepSeek Harness.

You are a coding assistant operating inside DeepSeek Harness. Work in the user's workspace and follow the tool contracts below.

# Runtime Context

Working directory: /repo
Platform: linux
Shell: bash

# Tool Specifications

- bash: run a shell command
- read: read a file
- write: create or replace a file
- edit: replace literal text in a file
- grep: search file contents
- glob: find files by path pattern
"""

CODEX_SYSTEM = """You are Codex, a coding agent based on GPT-5. You and the user share the same workspace and collaborate to achieve the user's goals.

# General

- When searching for text or files, prefer using `rg` or `rg --files` respectively.
- Do not use shell to read files; use the dedicated file tools.

<environment_context>
  <cwd>/repo</cwd>
  <approval_policy>never</approval_policy>
  <network_access>restricted</network_access>
</environment_context>
"""


def _fn(name, description, props=None, required=None):
    parameters = {'type': 'object', 'properties': props or {}}
    if required:
        parameters['required'] = required
    return {'name': name, 'description': description, 'parameters': parameters}


DSH_TOOLS = [
    _fn('bash', 'Run a shell command', {'command': {'type': 'string'}, 'cwd': {'type': 'string'}}, ['command']),
    _fn('read', 'Read a text file', {'path': {'type': 'string'}}, ['path']),
    _fn('write', 'Create or replace a file', {'path': {'type': 'string'}, 'content': {'type': 'string'}}, ['path', 'content']),
    _fn('edit', 'Replace literal text in a file', {'path': {'type': 'string'}, 'old': {'type': 'string'}, 'new': {'type': 'string'}}, ['path', 'old', 'new']),
    _fn('grep', 'Search file contents', {'pattern': {'type': 'string'}, 'path': {'type': 'string'}}, ['pattern']),
    _fn('glob', 'Find files by pattern', {'pattern': {'type': 'string'}}, ['pattern']),
    _fn('working_directory', 'Report or change the working directory', {'path': {'type': 'string'}}),
    _fn('todo_write', 'Maintain a task list', {'todos': {'type': 'array', 'items': {'type': 'object'}}}),
    _fn('web_search', 'Search the web', {'queries': {'type': 'array', 'items': {'type': 'string'}}}, ['queries']),
    _fn('web_fetch', 'Fetch a URL', {'url': {'type': 'string'}}, ['url']),
    _fn('job_output', 'Read a background job', {'job_id': {'type': 'string'}}, ['job_id']),
    _fn('job_list', 'List background jobs', {}),
    _fn('job_kill', 'Stop a background job', {'job_id': {'type': 'string'}}, ['job_id']),
    _fn('send_message', 'Message another agent', {'agent': {'type': 'string'}, 'message': {'type': 'string'}}, ['agent', 'message']),
    _fn('list_agents', 'List collaborator agents', {}),
    _fn('wait_agent', 'Wait for agent input', {'timeout_ms': {'type': 'integer'}}),
]

CODEX_TOOLS = [
    _fn('shell', 'Run a shell command', {'command': {'type': 'array', 'items': {'type': 'string'}}}, ['command']),
    _fn('apply_patch', 'Apply a patch to files', {'input': {'type': 'string'}}, ['input']),
    _fn('update_plan', 'Update the task plan', {'plan': {'type': 'array', 'items': {'type': 'object'}}}),
    _fn('view_image', 'View a local image', {'path': {'type': 'string'}}, ['path']),
    _fn('web_search', 'Search the web', {'query': {'type': 'string'}}, ['query']),
]

USER_TEXT = 'Reply with the single word OK.'


def arms():
    """每个臂只改一个维度，便于归因。"""
    return [
        # 基线：能过的那个
        ('codex-full', CODEX_SYSTEM, CODEX_TOOLS),
        # 问题臂：现场 429 的形态
        ('dsh-full', DSH_SYSTEM, DSH_TOOLS),
        # 只换 system：codex 工具 + dsh system
        ('dsh-system-only', DSH_SYSTEM, CODEX_TOOLS),
        # 只换工具：codex system + dsh 工具
        ('dsh-tools-only', CODEX_SYSTEM, DSH_TOOLS),
        # 无 system：单独看工具集
        ('no-system-dsh-tools', '', DSH_TOOLS),
        # 无 system、无工具：纯通道对照
        ('no-system-no-tools', '', []),
        # 身份句单点：只加一行 harness 身份，其余用 codex
        ('identity-line-only', 'You are an AI agent powered by DeepSeek Harness.\n\n' + CODEX_SYSTEM, CODEX_TOOLS),
    ]


def body(system_text, tools):
    payload = {
        'contents': [{'role': 'user', 'parts': [{'text': USER_TEXT}]}],
        'generationConfig': {'temperature': 0, 'maxOutputTokens': 16},
    }
    if system_text:
        payload['systemInstruction'] = {'parts': [{'text': system_text}]}
    if tools:
        payload['tools'] = [{'functionDeclarations': tools}]
    return payload


def post(url, key, payload, timeout):
    data = json.dumps(payload).encode('utf-8')
    request = urllib.request.Request(url, data=data, method='POST')
    request.add_header('Content-Type', 'application/json')
    request.add_header('x-goog-api-key', key)
    request.add_header('x-api-key', key)
    started = time.time()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode('utf-8', 'replace')
            return response.status, raw, time.time() - started
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode('utf-8', 'replace')
        return exc.code, raw, time.time() - started
    except Exception as exc:  # 连接层失败也要落盘，不能吞掉
        return None, f'{type(exc).__name__}: {exc}', time.time() - started


def summarize(raw, limit=600):
    try:
        data = json.loads(raw)
    except Exception:
        return raw[:limit]
    error = data.get('error') if isinstance(data, dict) else None
    if isinstance(error, dict):
        message = str(error.get('message', ''))[:limit]
        status = error.get('status', '')
        details = error.get('details')
        reason = ''
        if isinstance(details, list):
            for item in details:
                if isinstance(item, dict) and item.get('reason'):
                    reason = str(item['reason'])
                    break
        return f"status={status} reason={reason} message={message}"
    return raw[:limit]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--base-url', default='http://127.0.0.1:7861')
    parser.add_argument('--key', required=True)
    parser.add_argument('--model', default='gemini-2.5-flash')
    parser.add_argument('--out', default='')
    parser.add_argument('--gap', type=float, default=2.0, help='相邻请求间隔秒数，避免速率干扰')
    parser.add_argument('--arms', default='', help='只跑指定臂（逗号分隔），默认全部')
    parser.add_argument('--timeout', type=float, default=180.0)
    args = parser.parse_args()

    url = f"{args.base_url.rstrip('/')}/antigravity/v1beta/models/{args.model}:generateContent"
    results = []
    print(f'target: {url}')
    print(f"{'arm':24} {'code':>5}  {'secs':>6}  detail")
    print('-' * 110)
    selected = [item.strip() for item in args.arms.split(',') if item.strip()]
    plan = [arm for arm in arms() if not selected or arm[0] in selected]
    unknown = sorted(set(selected) - {arm[0] for arm in plan})
    if unknown:
        parser.error(f'unknown arm(s): {", ".join(unknown)}')

    for index, (name, system_text, tools) in enumerate(plan):
        if index:
            time.sleep(args.gap)
        status, raw, elapsed = post(url, args.key, body(system_text, tools), args.timeout)
        detail = summarize(raw)
        results.append({
            'arm': name,
            'code': status,
            'seconds': round(elapsed, 2),
            'system_chars': len(system_text),
            'tool_count': len(tools),
            'detail': detail,
            'raw': raw[:4000],
        })
        print(f'{name:24} {str(status):>5}  {elapsed:6.1f}  {detail[:90]}')

    print()
    codes = {item['arm']: item['code'] for item in results}
    if codes.get('dsh-full') == 200 and codes.get('codex-full') == 200:
        print('结论：dsh-full 已返回 200，当前部署不再区分两种形态 '
              '（兼容已生效，或本次未复现；与部署前基线对比后再下结论）')
    elif codes.get('codex-full') == 200 and codes.get('dsh-full') == 429:
        if codes.get('dsh-system-only') == 429 and codes.get('dsh-tools-only') == 200:
            verdict = 'system 维度触发（工具集无关）'
        elif codes.get('dsh-tools-only') == 429 and codes.get('dsh-system-only') == 200:
            verdict = 'tools 维度触发（system 无关）'
        elif codes.get('dsh-system-only') == 429 and codes.get('dsh-tools-only') == 429:
            verdict = '两维都触发，需分别修'
        elif codes.get('dsh-system-only') is None and codes.get('dsh-tools-only') is None:
            verdict = '只跑了整臂；用 --arms 补齐单维臂才能归因'
        else:
            verdict = '两个单维臂都过：429 依赖组合或与本次无关（检查并发/账号状态）'
        print(f'结论：{verdict}')
    else:
        print('结论：基线形态异常（codex-full 不是 200），先看账号、配额与入口')

    out_path = args.out or f"client-fingerprint-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.json"
    with open(out_path, 'w', encoding='utf-8') as handle:
        json.dump({'url': url, 'model': args.model, 'results': results}, handle, ensure_ascii=False, indent=2)
    print(f'已写入 {out_path}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
