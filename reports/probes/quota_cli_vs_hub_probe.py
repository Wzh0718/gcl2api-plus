#!/usr/bin/env python3
"""用同一个凭证对比 Antigravity CLI 指纹 vs 桌面端(hub)指纹的额度返回差异。

只读 creds/credentials.db，刷新 access token 不落库。
对 loadCodeAssist / fetchAvailableModels / retrieveUserQuotaSummary
各打两次（仅 UA 不同），输出 diff 摘要 + 原始 JSON 落盘。
"""
import asyncio
import json
import sqlite3
import sys
import time
from pathlib import Path

REPO = Path("/home/libre/project/gcli2api")
sys.path.insert(0, str(REPO))

import httpx  # noqa: E402

from src.utils import get_antigravity_user_agent  # noqa: E402

API = "https://daily-cloudcode-pa.googleapis.com"
PROXY = "http://127.0.0.1:7890"
HUB_UA = "antigravity/hub/2.23.1 (aidev_client; os_type=linux; arch=amd64; cl=998881661)"
CLI_UA = get_antigravity_user_agent()
OUT = REPO / "reports" / "probes" / f"quota_cli_vs_hub_{time.strftime('%Y%m%d_%H%M%S')}.json"


def load_cred():
    # 可选：传入凭证 JSON 文件路径；默认读 DB 第一条
    if len(sys.argv) > 1:
        return json.loads(Path(sys.argv[1]).read_text())
    conn = sqlite3.connect(f"file:{REPO}/creds/credentials.db?mode=ro", uri=True)
    row = conn.execute("SELECT credential_data FROM antigravity_credentials LIMIT 1").fetchone()
    conn.close()
    return json.loads(row[0])


async def refresh_token(cred):
    async with httpx.AsyncClient(timeout=30, trust_env=False, proxy=PROXY) as c:
        r = await c.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": cred["client_id"],
                "client_secret": cred["client_secret"],
                "refresh_token": cred["refresh_token"],
                "grant_type": "refresh_token",
            },
        )
        r.raise_for_status()
        return r.json()["access_token"]


async def post(client, path, ua, token, body):
    r = await client.post(
        f"{API}{path}",
        headers={
            "User-Agent": ua,
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept-Encoding": "gzip",
        },
        json=body,
    )
    try:
        data = r.json()
    except Exception:
        data = {"_non_json": r.text[:500]}
    return {"status": r.status_code, "data": data}


async def main():
    cred = load_cred()
    project = cred["project_id"]
    token = await refresh_token(cred)
    print(f"[cred] project={project} token refreshed OK")
    print(f"[UA] cli = {CLI_UA}")
    print(f"[UA] hub = {HUB_UA}")

    endpoints = [
        ("loadCodeAssist", "/v1internal:loadCodeAssist", {"metadata": {"ideType": "ANTIGRAVITY"}}),
        ("fetchAvailableModels", "/v1internal:fetchAvailableModels", {"project": project}),
        ("retrieveUserQuotaSummary", "/v1internal:retrieveUserQuotaSummary", {"project": project}),
    ]

    result = {"project": project, "ua": {"cli": CLI_UA, "hub": HUB_UA}, "endpoints": {}}
    async with httpx.AsyncClient(timeout=30, trust_env=False, proxy=PROXY) as c:
        for name, path, body in endpoints:
            cli = await post(c, path, CLI_UA, token, body)
            hub = await post(c, path, HUB_UA, token, body)
            result["endpoints"][name] = {"cli": cli, "hub": hub}
            print(f"\n===== {name} =====")
            if name == "loadCodeAssist":
                for side, resp in (("cli", cli), ("hub", hub)):
                    d = resp["data"]
                    print(f"[{side}] status={resp['status']} "
                          f"paidTier={json.dumps(d.get('paidTier'), ensure_ascii=False)} "
                          f"currentTier={json.dumps({k: v for k, v in (d.get('currentTier') or {}).items() if k in ('id', 'name')}, ensure_ascii=False)} "
                          f"ineligible={json.dumps(d.get('ineligibleTiers'), ensure_ascii=False)} "
                          f"project={d.get('cloudaicompanionProject')}")
            elif name == "fetchAvailableModels":
                cm = (cli["data"].get("models") or {})
                hm = (hub["data"].get("models") or {})
                print(f"[cli] status={cli['status']} models={len(cm)}")
                print(f"[hub] status={hub['status']} models={len(hm)}")
                only_cli = sorted(set(cm) - set(hm))
                only_hub = sorted(set(hm) - set(cm))
                print(f"  only-in-cli ({len(only_cli)}): {only_cli}")
                print(f"  only-in-hub ({len(only_hub)}): {only_hub}")
                common = sorted(set(cm) & set(hm))
                diffs = []
                for m in common:
                    cq = (cm[m].get("quotaInfo") or {})
                    hq = (hm[m].get("quotaInfo") or {})
                    cr, hr = cq.get("remainingFraction"), hq.get("remainingFraction")
                    if cr != hr or cq.get("resetTime") != hq.get("resetTime"):
                        diffs.append((m, cr, hr, cq.get("resetTime"), hq.get("resetTime")))
                print(f"  quotaInfo 不同的模型 ({len(diffs)}/{len(common)}):")
                for m, cr, hr, ct, ht in diffs:
                    print(f"    {m}: cli={cr} ({ct})  hub={hr} ({ht})")
            elif name == "retrieveUserQuotaSummary":
                for side, resp in (("cli", cli), ("hub", hub)):
                    d = resp["data"]
                    print(f"[{side}] status={resp['status']} desc={d.get('description')!r}")
                    for g in d.get("groups") or []:
                        for b in g.get("buckets") or []:
                            print(f"    {g.get('displayName')}: {b.get('bucketId')} "
                                  f"window={b.get('window')} remaining={b.get('remainingFraction')} "
                                  f"reset={b.get('resetTime')}")

    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"\n[done] raw saved -> {OUT}")


asyncio.run(main())
