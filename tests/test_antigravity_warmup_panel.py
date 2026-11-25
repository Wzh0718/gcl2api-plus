"""Exercise the warmup dashboard in the real desktop and mobile templates."""
import json
from html import unescape
from pathlib import Path
import re
import shutil
import subprocess

import pytest


FRONT = Path(__file__).resolve().parents[1] / "front"
REPLAY = """
window.onload = () => {};
(async () => {
    const config = {
        account_initial_concurrency: 1, account_target_concurrency: 0,
        account_initial_interval_ms: 1500, account_target_interval_ms: 80,
        success_threshold: 10, window_seconds: 1800, max_level: 3,
    };
    const state = (filename, status, extra = {}) => ({
        credential_name: filename, user_email: filename.replace('.json', '@example.com'),
        status, config, level: 0, success_count: 4,
        window_started_at: Date.now() / 1000 - 300,
        limits: {account_concurrency: 1, account_interval: 1.5},
        target_limits: {account_concurrency: 15, account_interval: .08}, ...extra,
    });
    const data = {defaults: config, ip_groups: {}, accounts: {
        'first.json': state('first.json', 'warming'),
        'target.json': state('target.json', 'warming', {
            level: 3, limits: {account_concurrency: 15, account_interval: .08},
        }),
        'paused.json': state('paused.json', 'paused'),
        'forbidden.json': state('forbidden.json', 'account_forbidden'),
        'complete.json': state('complete.json', 'active'),
        'cancelled.json': state('cancelled.json', 'cancelled'),
        'off.json': state('off.json', 'off'),
    }};
    // The credential page contains only an unrelated account: dashboard totals
    // and names must still come from the full overview, not the current page.
    AppState.antigravityCreds.data = {'off.json': {filename: 'off.json'}};
    window.fetch = async () => new Response(JSON.stringify(data));
    await loadAntigravityWarmupOverview();
    const root = document.querySelector('.warmup-dashboard');
    const snapshot = {
        summary: document.getElementById('antigravityWarmupSummary').textContent,
        rows: [...root.querySelectorAll('tbody tr')].map(row => row.textContent),
        settingsClosed: !document.getElementById('antigravityWarmupSettings').open,
        visibleWithoutExpanding: root.querySelector('.warmup-table').closest('details') === null,
        completionButtons: root.querySelectorAll('[data-warmup-complete]').length,
    };
    root.querySelector('[data-warmup-manage]').click();
    snapshot.dialog = document.querySelector('dialog[open]').textContent;
    document.querySelector('dialog[open]').close();
    document.querySelector('[data-warmup-key="success_threshold"]').value = '42';
    await loadAntigravityWarmupOverview();
    snapshot.draftKept = document.querySelector('[data-warmup-key="success_threshold"]').value;
    window.fetch = async () => new Response('{}', {status: 503});
    await loadAntigravityWarmupOverview(false);
    snapshot.error = document.getElementById('antigravityWarmupError').textContent;
    snapshot.rowsAfterError = root.querySelectorAll('tbody tr').length;
    window.fetch = async () => new Response(JSON.stringify({defaults: config, accounts: {}, ip_groups: {}}));
    await loadAntigravityWarmupOverview();
    snapshot.empty = root.querySelector('.warmup-empty').textContent;
    snapshot.emptySummary = document.getElementById('antigravityWarmupSummary').textContent;
    snapshot.errorCleared = document.getElementById('antigravityWarmupError').hidden;
    // Restore representative data for visual inspection at both sizes.
    window.fetch = async () => new Response(JSON.stringify(data));
    await loadAntigravityWarmupOverview();
    document.getElementById('loginSection').style.display = 'none';
    document.getElementById('mainSection').classList.remove('hidden');
    document.querySelectorAll('.tab-content').forEach(el => {
        el.classList.remove('active');
        el.style.display = 'none';
    });
    const tab = root.closest('.tab-content');
    tab.classList.add('active');
    tab.style.display = 'block';
    await document.fonts.ready;
    root.scrollIntoView({block: 'start', behavior: 'instant'});
    snapshot.width = root.getBoundingClientRect().width;
    snapshot.overflow = root.scrollWidth > root.clientWidth;
    document.getElementById('test-result').textContent = JSON.stringify(snapshot);
})().catch(error => document.getElementById('test-result').textContent = String(error.stack));
"""


@pytest.mark.parametrize(("template", "size"), [
    ("control_panel.html", "1440,1000"),
    ("control_panel_mobile.html", "390,1000"),
])
def test_warmup_dashboard(template, size, tmp_path):
    chrome = shutil.which("google-chrome") or shutil.which("chromium")
    if not chrome:
        pytest.skip("Chrome required for panel DOM regression")
    (tmp_path / "front").symlink_to(FRONT, target_is_directory=True)
    page = tmp_path / template
    page.write_text(FRONT.joinpath(template).read_text().replace(
        "</body>", '<output id="test-result" hidden></output><script>' + REPLAY + '</script></body>'
    ))
    result = subprocess.run([
        chrome, "--headless", "--no-sandbox", "--disable-background-networking", "--disable-extensions",
        "--host-resolver-rules=MAP * ~NOTFOUND", "--window-size=" + size,
        "--user-data-dir=" + str(tmp_path / "profile"), "--virtual-time-budget=1000",
        "--screenshot=" + str(tmp_path / "dashboard.png"), "--dump-dom", page.as_uri(),
    ], capture_output=True, text=True, check=True, timeout=30)
    output = re.search(r'<output id="test-result"[^>]*>(.*?)</output>', result.stdout, re.S)
    assert output and output[1].startswith("{"), output and output[1]
    data = json.loads(unescape(output[1]))
    assert data["summary"] == "养号中 2 个已暂停 1 个已完成 1 个已退出 1 个"
    assert len(data["rows"]) == 4
    assert "first@example.com" in data["rows"][0]
    assert "并发 15" in data["rows"][0] and "间隔 0.08 秒" in data["rows"][0]
    assert "本档成功 4 / 10 次" in data["rows"][0] and "观察 5 分钟 / 30 分钟" in data["rows"][0]
    assert "已达目标，可完成养号" in data["rows"][1]
    assert "养号已暂停" in data["rows"][2] and "已永久退出养号" in data["rows"][3]
    assert data["completionButtons"] == 1
    assert data["settingsClosed"] and data["visibleWithoutExpanding"]
    assert "first.json" in data["dialog"] and "当前状态：养号中" in data["dialog"]
    assert data["draftKept"] == "42"
    assert "保留上次结果" in data["error"] and data["rowsAfterError"] == 4
    assert data["empty"] == "暂无正在养号的账号。" and "养号中 0 个" in data["emptySummary"]
    assert data["errorCleared"] and data["width"] > 300 and not data["overflow"]
