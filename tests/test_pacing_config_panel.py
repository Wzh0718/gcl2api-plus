"""Run the real templates and common.js to verify pacing configuration round trips."""
from html import unescape
import json
from pathlib import Path
import re
import shutil
import subprocess

import pytest


FRONT = Path(__file__).resolve().parents[1] / "front"
REPLAY = """
window.onload = () => {};
document.addEventListener('DOMContentLoaded', async () => {
try {
    const lowKey = 'antigravity_pacing_min_interval_seconds';
    const highKey = 'antigravity_pacing_max_interval_seconds';
    const low = document.getElementById('antigravityPacingMinIntervalSeconds');
    const high = document.getElementById('antigravityPacingMaxIntervalSeconds');
    const ttft = document.getElementById('antigravityTtftFallbackSeconds');
    const ttftKey = 'antigravity_ttft_fallback_seconds';
    let values = {[lowKey]: 2, [highKey]: 10, antigravity_pacing_enabled: true};
    const defaults = {enabled: false, requests_per_minute: 0, requests_per_hour: 0,
                      generated_tokens_per_minute: 0, generated_tokens_per_hour: 0};
    const writes = [];
    window.setTimeout = () => 0;
    window.fetch = async (url, options) => {
        if (options?.method === 'POST') {
            const saved = JSON.parse(options.body).config;
            // Match /config/save normalization before /config/get reads it back.
            saved.text_account_retry_status_codes = saved.text_account_retry_status_codes
                .split(',').map(value => Number(value.trim()));
            writes.push(saved);
            values = {...values, ...saved};
            return new Response(JSON.stringify({saved_config: saved}));
        }
        return new Response(JSON.stringify({config: values, env_locked: [],
            account_usage_defaults: defaults, capture_schema: {}}));
    };
    await loadConfig();
    const snapshot = {loaded: [low.value, high.value], ttftDefault: ttft.value};
    ttft.value = '10';
    low.value = '.25'; high.value = '1.5';
    await saveConfig();
    snapshot.decimals = [writes[0]?.[lowKey], writes[0]?.[highKey]];
    low.value = '0'; high.value = '0'; ttft.value = '0';
    await saveConfig();
    snapshot.disabled = [writes[1]?.[lowKey], writes[1]?.[highKey]];
    await loadConfig();
    snapshot.reloaded = [low.value, high.value];
    snapshot.ttft = [writes[0]?.[ttftKey], writes[1]?.[ttftKey], ttft.value];
    document.getElementById('loginSection').style.display = 'none';
    document.getElementById('mainSection').classList.remove('hidden');
    document.querySelectorAll('.tab-content').forEach(tab => {
        tab.classList.remove('active'); tab.style.display = 'none';
    });
    const tab = low.closest('.tab-content');
    tab.classList.add('active'); tab.style.removeProperty('display');
    switchConfigTab('models');
    ttft.value = '1.5';
    await saveConfig();
    snapshot.ttftInvalidBlocked = writes.length === 2 && document.activeElement === ttft;
    ttft.value = '10';
    low.value = '10'; high.value = '2';
    await saveConfig();
    snapshot.invalidBoundBlocked = writes.length === 2 && document.activeElement === high;
    low.value = ''; high.value = '10';
    await saveConfig();
    snapshot.blankBlocked = writes.length === 2 && document.activeElement === low;
    values = {...values, [lowKey]: 2, [highKey]: 10};
    await loadConfig();
    await document.fonts.ready;
    window.revealCompatibilityField?.(low);
    low.scrollIntoView({block: 'center', behavior: 'instant'});
    snapshot.labels = [low, high].map(field => document.querySelector(`label[for="${field.id}"]`).textContent);
    snapshot.visible = [low, high, ttft].every(field => field.getBoundingClientRect().height > 0);
    snapshot.controlsFit = [low, high, ttft].every(field => {
        const rect = field.getBoundingClientRect();
        return rect.left >= 0 && rect.right <= innerWidth;
    });
    snapshot.viewport = innerWidth;
    document.getElementById('test-result').textContent = JSON.stringify(snapshot);
} catch (error) {
    document.getElementById('test-result').textContent = String(error.stack);
}
});
"""


@pytest.mark.parametrize("template,width", [
    ("control_panel.html", 1440), ("control_panel.html", 1024),
    ("control_panel_mobile.html", 768), ("control_panel_mobile.html", 320),
])
def test_dynamic_interval_controls_round_trip(template, width, tmp_path):
    chrome = shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chrome")
    if not chrome:
        pytest.skip("Chrome required for panel DOM regression")
    page = tmp_path / template
    source = FRONT.joinpath(template).read_text(encoding="utf-8")
    source = source.replace("<head>", f'<head><base href="{FRONT.parent.as_uri()}/">')
    page.write_text(source.replace(
        "</body>", '<output id="test-result" hidden></output><script>' + REPLAY + '</script></body>'
    ), encoding="utf-8")
    result = subprocess.run([
        chrome, "--headless", "--no-sandbox", "--disable-background-networking", "--disable-extensions",
        "--host-resolver-rules=MAP * ~NOTFOUND", f"--window-size={width},1000",
        "--user-data-dir=" + str(tmp_path / "profile"), "--virtual-time-budget=1000",
        "--screenshot=" + str(tmp_path / "pacing-config.png"), "--dump-dom", page.as_uri(),
    ], capture_output=True, encoding="utf-8", errors="replace", check=True, timeout=30)
    output = re.search(r'<output id="test-result"[^>]*>(.*?)</output>', result.stdout, re.S)
    assert output and output[1].startswith("{"), output and unescape(output[1])
    data = json.loads(unescape(output[1]))
    assert data["loaded"] == ["2", "10"]
    assert data["ttftDefault"] == "30" and data["ttft"] == [10, 0, "0"]
    assert data["ttftInvalidBlocked"]
    assert data["decimals"] == [.25, 1.5]
    assert data["disabled"] == [0, 0] and data["reloaded"] == ["0", "0"]
    assert data["invalidBoundBlocked"] and data["blankBlocked"]
    assert data["labels"] == ["动态发起间隔下限（秒/账号）:", "动态发起间隔上限（秒/账号）:"]
    assert data["visible"] and data["controlsFit"], data
