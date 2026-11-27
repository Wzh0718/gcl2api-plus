#!/usr/bin/env python3
"""账号权益与封禁体检（只读，除非显式打开 --probe-generation）。

回答两个问题：
1. 账号的 paidTier 现在是什么（Google AI Pro / Antigravity Starter Quota / 无）；
2. generateContent 是否被 Google 以 TOS_VIOLATION 之类禁用。

结论分档：
- pro            paidTier 仍是付费档，且生成探针成功（若开启）
- starter        订阅权益已回收（paidTier=Antigravity Starter Quota，仅剩免费配额）
- no_entitlement 连 paidTier 都没有
- tos_blocked    服务被禁用（PERMISSION_DENIED / TOS_VIOLATION），与订阅无关
- model_forbidden / quota_exhausted / unknown

用法：
    python scripts/diagnose_entitlement.py                     # 扫 ./creds/*.json
    python scripts/diagnose_entitlement.py creds/a.json ...     # 指定文件
    python scripts/diagnose_entitlement.py --probe-generation   # 追加一次最小生成探针
    python scripts/diagnose_entitlement.py --proxy http://127.0.0.1:7890

注意：凭据文件只读，不会写回；token 不打印。
"""

from __future__ import annotations

import argparse
import asyncio
import glob
import json
import os
import sys
from typing import Any, Dict, List, Optional

import httpx

OAUTH_URL = "https://oauth2.googleapis.com/token"
API_URL = os.getenv("ANTIGRAVITY_API_URL", "https://daily-cloudcode-pa.googleapis.com")
STARTER_NAME = "Antigravity Starter Quota"
DEFAULT_UA = "antigravity/hub/2.35.2 (aidev_client; os_type=linux; arch=amd64; cl=1029311855; datacollection_opt_in=false)"


def redact(text: str, limit: int = 300) -> str:
    for marker in ("access_token", "refresh_token", "client_secret"):
        if marker in text:
            text = text.split(marker)[0] + marker + "=***"
    return text[:limit].replace("\n", " ")


async def refresh_access_token(client: httpx.AsyncClient, data: Dict[str, Any]) -> Optional[str]:
    payload = {
        "client_id": data.get("client_id"),
        "client_secret": data.get("client_secret"),
        "refresh_token": data.get("refresh_token"),
        "grant_type": "refresh_token",
    }
    if not all(payload[key] for key in ("client_id", "client_secret", "refresh_token")):
        return None
    response = await client.post(OAUTH_URL, data=payload)
    if response.status_code != 200:
        return None
    return response.json().get("access_token")


def headers_for(token: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": DEFAULT_UA,
        "X-Goog-Api-Client": "google-cloud-sdk vscode_cloudshelleditor/0.1",
    }


async def check_account(
    client: httpx.AsyncClient, path: str, *, probe_generation: bool
) -> Dict[str, Any]:
    result: Dict[str, Any] = {"file": os.path.basename(path), "verdict": "unknown", "detail": ""}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception as exc:  # noqa: BLE001
        result["detail"] = f"无法读取: {type(exc).__name__}"
        return result

    token = await refresh_access_token(client, data)
    if not token:
        result["detail"] = "refresh_token 刷新失败（凭据失效或被撤销）"
        return result
    headers = headers_for(token)

    # 1) 权益档位
    tier_resp = await client.post(
        f"{API_URL}/v1internal:loadCodeAssist",
        headers=headers,
        json={"metadata": {"ideType": "ANTIGRAVITY"}},
    )
    paid = current = None
    if tier_resp.status_code == 200:
        payload = tier_resp.json()
        paid = payload.get("paidTier") or {}
        current = payload.get("currentTier") or {}
        result["paid_tier"] = {"id": paid.get("id"), "name": paid.get("name")}
        result["current_tier"] = {"id": current.get("id"), "name": current.get("name")}
        if paid.get("name") == STARTER_NAME or paid.get("id") == "free-tier":
            result["verdict"] = "starter"
            result["detail"] = "订阅权益已回收，只剩 Antigravity Starter（免费）配额"
        elif paid.get("id"):
            result["verdict"] = "pro"
            result["detail"] = "paidTier 仍为付费档"
        else:
            result["verdict"] = "no_entitlement"
            result["detail"] = "响应里没有 paidTier"
    else:
        result["detail"] = f"loadCodeAssist HTTP {tier_resp.status_code}: {redact(tier_resp.text)}"
        if tier_resp.status_code == 403:
            result["verdict"] = "tos_blocked"

    # 2) 生成探针（唯一能区分“订阅在但服务被封”的动作，会产生一次极小调用）
    if probe_generation and result["verdict"] in {"pro", "starter", "unknown"}:
        project = str(data.get("project_id") or "")
        if not project:
            result["generation"] = "skipped: 缺少 project_id"
        else:
            body = {
                "model": os.getenv("DIAGNOSE_PROBE_MODEL", "gemini-2.5-flash"),
                "project": project,
                "request": {
                    "contents": [{"role": "user", "parts": [{"text": "reply with ok"}]}]
                },
            }
            probe = await client.post(
                f"{API_URL}/v1internal:generateContent", headers=headers, json=body
            )
            if probe.status_code == 200:
                result["generation"] = "ok"
            else:
                text = probe.text
                result["generation"] = f"HTTP {probe.status_code}"
                if "TOS_VIOLATION" in text or "violation of Terms of Service" in text:
                    result["verdict"] = "tos_blocked"
                    result["detail"] = "服务被 Google 禁用（TOS_VIOLATION），订阅本身未必失效"
                elif "MODEL_FORBIDDEN" in text:
                    result["verdict"] = "model_forbidden"
                    result["detail"] = redact(text, 160)
                elif "QUOTA_EXHAUSTED" in text or "RESOURCE_EXHAUSTED" in text:
                    result["verdict"] = "quota_exhausted"
                    result["detail"] = redact(text, 160)
                else:
                    result["detail"] = redact(text, 160)
    return result


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", nargs="*", help="凭据 JSON 文件；省略时扫 creds/*.json")
    parser.add_argument("--creds-dir", default=os.getenv("CREDENTIALS_DIR", "creds"))
    parser.add_argument("--proxy", default=os.getenv("DIAGNOSE_PROXY") or None)
    parser.add_argument("--probe-generation", action="store_true")
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args()

    paths: List[str] = args.files or sorted(glob.glob(os.path.join(args.creds_dir, "*.json")))
    if not paths:
        print(f"没有找到凭据文件：{args.creds_dir}/*.json", file=sys.stderr)
        return 2

    async with httpx.AsyncClient(proxy=args.proxy, timeout=args.timeout) as client:
        for path in paths:
            outcome = await check_account(client, path, probe_generation=args.probe_generation)
            print(json.dumps(outcome, ensure_ascii=False))

    print(
        "\n分档说明：pro=付费档仍在；starter=订阅被回收；"
        "tos_blocked=服务被禁用（需申诉 https://forms.gle/hGzM9MEUv2azZsrb9）；"
        "quota_exhausted=额度耗尽；model_forbidden=该模型无权限",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
