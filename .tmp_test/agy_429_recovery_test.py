#!/usr/bin/env python3
"""等待 aicode-consumers 账号 gemini-2.5-flash 的 quotaResetTimeStamp (11:30:43Z) 过后，
验证「429 冷却后是否自动解除」：先直测上游，再走本地网关验证 record_success 清冷却。"""
import json, sqlite3, time, urllib.request, urllib.error, datetime, subprocess

DB = "/home/libre/project/gcli2api/creds/credentials.db"
RESET_TS = 1792409443.0  # 2026-10-19T11:30:43Z
PROXY = "http://localhost:7890"
HDRS = {
    "Content-Type": "application/json",
    "User-Agent": "antigravity/1.4.2 darwin/arm64",
    "X-Goog-Api-Client": "google-cloud-sdk vscode_cloudshelleditor/0.1",
    "Client-Metadata": '{"ideType":"IDE_UNSPECIFIED","platform":"PLATFORM_UNSPECIFIED","pluginType":"GEMINI"}',
}

def post(url, payload, token=None, proxy=PROXY, timeout=60):
    handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy}) if proxy else urllib.request.ProxyHandler({})
    opener = urllib.request.build_opener(handler)
    headers = dict(HDRS)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers, method="POST")
    try:
        with opener.open(req, timeout=timeout) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()

def load_cred():
    db = sqlite3.connect(DB)
    row = db.execute("SELECT credential_data FROM antigravity_credentials WHERE filename LIKE '%aicode%'").fetchone()
    return json.loads(row[0])

def ensure_fresh_token(cred):
    expiry = datetime.datetime.fromisoformat(cred["expiry"])
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=datetime.timezone.utc)
    if (expiry - datetime.datetime.now(datetime.timezone.utc)).total_seconds() > 300:
        return cred["access_token"]
    st, body = post("https://oauth2.googleapis.com/token", {
        "client_id": cred["client_id"], "client_secret": cred["client_secret"],
        "refresh_token": cred["refresh_token"], "grant_type": "refresh_token"})
    data = json.loads(body)
    assert st == 200 and "access_token" in data, f"refresh failed: {st} {body[:300]}"
    return data["access_token"]

wait = RESET_TS + 90 - time.time()
print(f"[{datetime.datetime.now(datetime.timezone.utc):%H:%M:%S}Z] sleep {wait:.0f}s until quota reset", flush=True)
if wait > 0:
    time.sleep(wait)

cred = load_cred()
token = ensure_fresh_token(cred)

st, body = post("https://daily-cloudcode-pa.googleapis.com/v1internal:generateContent", {
    "project": "aicode-consumers", "model": "gemini-2.5-flash",
    "request": {"contents": [{"role": "user", "parts": [{"text": "say ok"}]}]}}, token=token)
print(f"UPSTREAM gemini-2.5-flash status={st} body={body[:500]}", flush=True)

out = subprocess.run(["curl", "-s", "http://127.0.0.1:7861/v1/chat/completions",
    "-H", "Authorization: Bearer pwd", "-H", "Content-Type: application/json",
    "-d", '{"model":"gemini-2.5-flash","messages":[{"role":"user","content":"say ok"}],"stream":false}',
    "--max-time", "90"], capture_output=True, text=True)
print(f"GATEWAY response={out.stdout[:500]}", flush=True)

db = sqlite3.connect(DB)
row = db.execute("SELECT call_count, error_codes, model_cooldowns, last_success FROM antigravity_credentials WHERE filename LIKE '%aicode%'").fetchone()
print(f"DB final: call_count={row[0]} error_codes={row[1]} model_cooldowns={row[2]} last_success={row[3]}", flush=True)
