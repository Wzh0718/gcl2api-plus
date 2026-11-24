"""Replay quota responses through the real panel templates and inspect the resulting DOM."""
import json
from html import unescape
from pathlib import Path
import re
import shutil
import subprocess

import pytest


FRONT = Path(__file__).resolve().parents[1] / "front"
QUOTA_REPLAY = """
(async () => {
    const filename = 'sample.json';
    const card = createCredCard({filename, status: {}, tier: 'pro'}, AppState.antigravityCreds);
    document.body.appendChild(card);
    const details = card.querySelector('.cred-quota-content').closest('[id^="quota-"]');
    const result = [];
    for (const [kind, name, windows] of [
        ['unknown', 'New Plan', []], ['paid', 'Google "Pro"', []],
        ['free', 'Starter', ['weekly']], ['free', 'Starter', ['weekly', '5h']],
    ]) {
        const groups = windows.length ? [{buckets: windows.map(window => ({window}))}] : null;
        const paid_tier = {kind, name, snapshot_conflict: kind === 'free', stored_tier: 'pro'};
        window.fetch = async () => new Response(JSON.stringify({success: true, paid_tier, models: {}, groups}));
        details.style.display = 'none';
        await toggleAntigravityQuotaDetails(details.id.slice('quota-'.length));
        const badge = card.querySelector('.cred-tier-badge');
        result.push([badge.textContent, badge.title, details.textContent, card.querySelector('.cred-tier-notice').textContent]);
    }
    document.getElementById('test-result').textContent = JSON.stringify(result);
})().catch(error => document.getElementById('test-result').textContent = String(error));
"""


@pytest.mark.parametrize(("template", "size"), [
    ("control_panel.html", "1440,900"),
    ("control_panel_mobile.html", "390,844"),
])
def test_live_tier_refresh(template, size, tmp_path):
    chrome = shutil.which("google-chrome") or shutil.which("chromium")
    if not chrome:
        pytest.skip("Chrome required for panel DOM regression")
    (tmp_path / "front").symlink_to(FRONT, target_is_directory=True)
    page = tmp_path / template
    page.write_text(FRONT.joinpath(template).read_text().replace(
        "</body>", '<output id="test-result"></output><script>' + QUOTA_REPLAY + '</script></body>'
    ))
    result = subprocess.run([
        chrome, "--headless", "--no-sandbox", "--disable-background-networking", "--disable-extensions",
        "--host-resolver-rules=MAP * ~NOTFOUND", "--window-size=" + size, "--user-data-dir=" + str(tmp_path / "profile"),
        "--virtual-time-budget=1000", "--dump-dom", page.as_uri(),
    ], capture_output=True, text=True, check=True, timeout=30)
    output = re.search(r'<output id="test-result">(.*?)</output>', result.stdout, re.S)
    assert output and output[1].startswith("["), output and output[1]
    unknown, quoted, weekly, both = json.loads(unescape(output[1]))
    assert unknown[0] == "实时: New Plan" and "New Plan" in unknown[2]
    assert quoted[0] == '实时: Google "Pro"（付费）' and 'Google "Pro"' in quoted[1]
    assert "额度窗口" not in quoted[1] and "5 小时" not in quoted[1]
    assert "每周" in weekly[1] and "5 小时" not in weekly[1]
    assert "每周" in weekly[3] and "5 小时" not in weekly[3]
    assert "5 小时" in both[1] and "5小时限额" in both[2] and "5 小时" in both[3]
