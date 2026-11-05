from pathlib import Path


def test_agy_logs_do_not_print_tokens_or_upstream_bodies():
    auth_source = Path("src/auth.py").read_text(encoding="utf-8")
    antigravity_source = Path("src/api/antigravity.py").read_text(encoding="utf-8")
    oauth_source = Path("src/google_oauth_api.py").read_text(encoding="utf-8")

    assert "使用的token:" not in auth_source
    assert "access_token[:20]" not in auth_source
    assert "响应: {error_body" not in antigravity_source
    assert "响应: {error_text" not in antigravity_source
    assert "Failed to fetch models ({response.status_code}): {response.text" not in antigravity_source
    assert "Failed to fetch quota ({response.status_code}): {response.text" not in antigravity_source
    assert "Response body:" not in oauth_source


def test_oauth_default_user_agent_matches_official_capture():
    from src.utils import get_antigravity_oauth_user_agent

    assert get_antigravity_oauth_user_agent() == "Go-http-client/2.0"
