#!/usr/bin/env python3
"""从数据库日志详情抽检引导词注入情况。

用法：
    .venv/bin/python scripts/audit_bridge_records.py                 # 最近 20 条带详情的记录
    .venv/bin/python scripts/audit_bridge_records.py --limit 200
    .venv/bin/python scripts/audit_bridge_records.py --db path/to/credentials.db
    .venv/bin/python scripts/audit_bridge_records.py --replay        # 用当前管线重放过期记录

退出码：`--strict` 下，出现 bridge_missing/bridge_duplicated/bridge_not_at_tail/
runtime_block_leaked/undeclared_tool_call 任一即为 1。

只读数据库，不发起任何上游请求。内容捕获关闭时旧记录没有详情，脚本会如实报告。
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.bridge_audit import (  # noqa: E402
    audit_details,
    extract_system_text,
    render_table,
    summarize,
)
from src.storage.call_record_content import decode_details  # noqa: E402

FATAL = {
    'bridge_missing', 'bridge_duplicated', 'bridge_not_at_tail',
    'runtime_block_leaked', 'undeclared_tool_call',
}


def default_db() -> Path:
    return Path(os.getenv('CREDENTIALS_DIR', str(ROOT / 'creds'))) / 'credentials.db'


def load(db_path: Path, limit: int):
    with sqlite3.connect(f'file:{db_path}?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            'SELECT id, channel, model_name, request_details FROM call_records '
            'WHERE length(request_details) > 2 ORDER BY id DESC LIMIT ?',
            (limit,),
        ).fetchall()
    reports = []
    for row in rows:
        try:
            details = decode_details(row['request_details'])
        except Exception as exc:  # 损坏或旧格式：如实计数，不静默跳过
            reports.append({'id': row['id'], 'model': row['model_name'],
                            'audit': {'baseline': 'decode_error', 'bridge_new': 0,
                                      'bridge_legacy': 0, 'tools': [], 'response_chars': 0,
                                      'problems': ['details_decode_error'], 'notes': [str(exc)[:80]]}})
            continue
        reports.append({'id': row['id'], 'model': row['model_name'],
                        'audit': audit_details(details, row['model_name']), 'details': details})
    return reports


def replay(reports):
    """把历史记录按当前管线重新注入，只看注入层结论。"""
    import asyncio

    from src.converter.gemini_fix import normalize_gemini_request

    results = []
    for report in reports:
        details = report.get('details') or {}
        upstream = details.get('upstream') or {}
        if not extract_system_text(details):
            continue

        request = {
            'model': report.get('model') or 'gemini-2.5-flash',
            'contents': upstream.get('messages') or [],
            'tools': upstream.get('tools') or [],
        }
        if isinstance(upstream.get('parameters'), dict):
            request.update({k: v for k, v in upstream['parameters'].items() if k == 'generationConfig'})

        normalized = asyncio.run(normalize_gemini_request(request, mode='antigravity'))
        results.append({'id': report['id'], 'audit': audit_details({'upstream': {
            'system': normalized.get('systemInstruction'),
            'tools': normalized.get('tools'),
        }})})
    return results


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--db', type=Path, default=default_db())
    parser.add_argument('--limit', type=int, default=20)
    parser.add_argument('--replay', action='store_true', help='按当前管线重放历史记录')
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--strict', action='store_true')
    args = parser.parse_args()

    if not args.db.is_file():
        print(f'database not found: {args.db}', file=sys.stderr)
        return 2

    reports = load(args.db, args.limit)
    if not reports:
        print('no call_records with request_details; 内容捕获可能未开启')
        return 0

    with_system = sum(1 for r in reports if extract_system_text(r.get('details') or {}))
    if args.replay:
        replayed = replay(reports)
        if replayed:
            reports = replayed

    audit_reports = [{'id': r['id'], 'model': r.get('model'), 'audit': r['audit']} for r in reports]

    if args.json:
        print(json.dumps({'records': audit_reports, 'summary': summarize(audit_reports)},
                         ensure_ascii=False, indent=1))
    else:
        print(render_table(audit_reports))
        summary = summarize(audit_reports)
        print('\nsummary: ' + ', '.join(f'{k}={v}' for k, v in summary.items()))
        print(f'records with system prompt: {with_system}/{len(reports)}')

    if args.strict:
        bad = [r for r in audit_reports if FATAL & set(r['audit']['problems'])]
        if bad:
            print('strict: %d record(s) failed injection invariants' % len(bad), file=sys.stderr)
            return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
