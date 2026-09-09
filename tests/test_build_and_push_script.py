import os
import stat
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "build-and-push.sh"
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"
WORKFLOW_FILE = REPO_ROOT / ".github" / "workflows" / "ghcr-sync-release.yml"
OVERLAY_FILES = REPO_ROOT / "custom-overlay" / "files"

CUSTOM_FILE_PATHS = {
    ".dockerignore",
    ".env.example",
    "config.py",
    "docs/ANTIGRAVITY_ACCOUNT_IP_BINDING_PLAN.md",
    "docs/ANTIGRAVITY_IMAGE_API.md",
    "examples/gemini_image_demo.env.example",
    "examples/gemini_image_demo.py",
    "examples/gemini_sdk_image_edit.py",
    "front/common.js",
    "front/control_panel.css",
    "front/control_panel.html",
    "front/control_panel_mobile.html",
    "src/api/antigravity.py",
    "src/api/geminicli.py",
    "src/api/utils.py",
    "src/api/vertex.py",
    "src/antigravity_403_recheck.py",
    "src/antigravity_account_health.py",
    "src/antigravity_error_classifier.py",
    "src/antigravity_full_check.py",
    "src/api_keys.py",
    "src/auth.py",
    "src/billing.py",
    "src/credential_manager.py",
    "src/dingtalk_alert.py",
    "src/google_oauth_api.py",
    "src/httpx_client.py",
    "src/models.py",
    "src/oauth_clients.py",
    "src/panel/__init__.py",
    "src/panel/api_keys.py",
    "src/panel/auth.py",
    "src/panel/billing.py",
    "src/panel/config_routes.py",
    "src/panel/creds.py",
    "src/panel/proxy_groups.py",
    "src/proxy_groups.py",
    "src/shadowsocks.py",
    "src/redis_config.py",
    "src/converter/anti_truncation.py",
    "src/converter/gemini_fix.py",
    "src/router/antigravity/anthropic.py",
    "src/router/antigravity/gemini.py",
    "src/router/antigravity/model_list.py",
    "src/router/antigravity/openai.py",
    "src/router/geminicli/anthropic.py",
    "src/router/geminicli/gemini.py",
    "src/router/geminicli/openai.py",
    "src/router/stream_passthrough.py",
    "src/storage/sqlite_manager.py",
    "src/storage_adapter.py",
    "src/stream_guard.py",
    "src/utils.py",
    "tests/test_api_keys.py",
    "tests/test_antigravity_403_recheck.py",
    "tests/test_antigravity_account_health.py",
    "tests/test_antigravity_credit_cooldown.py",
    "tests/test_antigravity_image_protocol.py",
    "tests/test_antigravity_quota_reconcile.py",
    "tests/test_billing.py",
    "tests/test_dingtalk_alert.py",
    "tests/test_gemini_image_demo.py",
    "tests/test_gemini_sdk_image_edit.py",
    "tests/test_credential_success_persistence.py",
    "tests/test_log_websocket_lifecycle.py",
    "tests/test_model_stats.py",
    "tests/test_openai_image_generations.py",
    "tests/test_openai_image_edits.py",
    "tests/test_oauth_clients.py",
    "tests/test_rt_login.py",
    "tests/test_proxy_context.py",
    "tests/test_proxy_groups.py",
    "tests/test_redis_config.py",
    "tests/test_sqlite_billing_schema.py",
    "requirements-termux.txt",
    "requirements.txt",
    "scripts/antigravity_image_ab_demo.py",
    "scripts/banana_image_stress.py",
    "pyproject.toml",
    "tests/test_antigravity_image_ab_demo.py",
    "tests/test_antigravity_image_transport.py",
    "tests/test_banana_image_stress.py",
    "web.py",
}


def run(command, cwd, *, env=None, check=True):
    result = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    if check and result.returncode != 0:
        pytest.fail(f"command failed ({result.returncode}): {' '.join(command)}\n{result.stdout}")
    return result


def git(repo, *args):
    return run(["git", *args], repo)


def create_upstream(tmp_path: Path):
    upstream = tmp_path / "upstream"
    git(tmp_path, "init", "--initial-branch=master", str(upstream))
    git(upstream, "config", "user.name", "Release Script Test")
    git(upstream, "config", "user.email", "release-script@example.invalid")

    files = {
        "Dockerfile": "FROM scratch\n",
        "config.py": "SOURCE = 'upstream'\n",
        "front/common.js": "const upstream = true;\n",
        "src/example.py": "SOURCE = 'upstream'\n",
        "tests/test_billing.py": "def test_placeholder():\n    assert True\n",
        "tests/test_proxy_context.py": "def test_placeholder():\n    assert True\n",
        "tests/test_sqlite_billing_schema.py": "def test_placeholder():\n    assert True\n",
        "web.py": "SOURCE = 'upstream'\n",
    }
    for relative_path, content in files.items():
        path = upstream / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    git(upstream, "add", ".")
    git(upstream, "commit", "-m", "initial upstream")
    return upstream


def create_overlay(tmp_path: Path):
    overlay = tmp_path / "overlay"
    overlay.mkdir()
    (overlay / ".dockerignore").write_text(".git\n.env\ncreds/\n", encoding="utf-8")
    (overlay / "config.py").write_text("SOURCE = 'custom-overlay'\n", encoding="utf-8")
    (overlay / "custom-plugin.txt").write_text("enabled\n", encoding="utf-8")
    return overlay


def create_fake_toolchain(tmp_path: Path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log_file = tmp_path / "commands.log"

    uv = bin_dir / "uv"
    uv.write_text(
        "#!/usr/bin/env bash\n"
        "printf 'uv %s\\n' \"$*\" >> \"$RELEASE_TEST_LOG\"\n"
        "[[ -f custom-plugin.txt ]] || exit 41\n"
        "[[ \"$(cat config.py)\" == \"SOURCE = 'custom-overlay'\" ]] || exit 42\n"
        "[[ \"${PYTHONPATH:-}\" == \"$PWD\" ]] || exit 43\n"
        "[[ \"${FAIL_RELEASE_TESTS:-0}\" == \"1\" ]] && exit 17\n"
        "exit 0\n",
        encoding="utf-8",
    )
    node = bin_dir / "node"
    node.write_text(
        "#!/usr/bin/env bash\n"
        "printf 'node %s\\n' \"$*\" >> \"$RELEASE_TEST_LOG\"\n",
        encoding="utf-8",
    )
    python = bin_dir / "python3"
    python.write_text(
        "#!/usr/bin/env bash\n"
        "printf 'python3 %s\\n' \"$*\" >> \"$RELEASE_TEST_LOG\"\n",
        encoding="utf-8",
    )
    docker = bin_dir / "docker"
    docker.write_text(
        "#!/usr/bin/env bash\n"
        "printf 'docker %s\\n' \"$*\" >> \"$RELEASE_TEST_LOG\"\n"
        "if [[ \"$1\" == \"info\" ]]; then exit 0; fi\n"
        "if [[ \"$1\" == \"build\" ]]; then\n"
        "  context=\"${@: -1}\"\n"
        "  printf 'context-config=%s\\n' \"$(cat \"$context/config.py\")\" >> \"$RELEASE_TEST_LOG\"\n"
        "  printf 'context-plugin=%s\\n' \"$(cat \"$context/custom-plugin.txt\")\" >> \"$RELEASE_TEST_LOG\"\n"
        "  [[ ! -e \"$context/.env\" ]] || exit 51\n"
        "  [[ ! -e \"$context/creds\" ]] || exit 52\n"
        "  [[ ! -e \"$context/local-only.txt\" ]] || exit 53\n"
        "  [[ -f \"$context/.dockerignore\" ]] || exit 54\n"
        "fi\n",
        encoding="utf-8",
    )
    for executable in (uv, node, python, docker):
        executable.chmod(executable.stat().st_mode | stat.S_IXUSR)

    env = os.environ.copy()
    env.update({"PATH": f"{bin_dir}:{env['PATH']}", "RELEASE_TEST_LOG": str(log_file)})
    return env, log_file


def release_env(env, upstream: Path, overlay: Path):
    env.pop("PYTHONPATH", None)
    env.update(
        {
            "UPSTREAM_URL": str(upstream),
            "UPSTREAM_BRANCH": "master",
            "CUSTOM_OVERLAY_DIR": str(overlay),
            "HARBOR_IMAGE": "harbor.beeintel.com/crawler-platform/gcli2api",
            "RELEASE_DATE": "20260727",
        }
    )
    return env


def test_release_uses_fresh_upstream_overlay_and_pushes_date_and_latest(tmp_path):
    upstream = create_upstream(tmp_path)
    overlay = create_overlay(tmp_path)
    env, log_file = create_fake_toolchain(tmp_path)
    release_env(env, upstream, overlay)
    caller = tmp_path / "caller"
    caller.mkdir()
    (caller / "local-only.txt").write_text("must not enter image\n", encoding="utf-8")

    result = run([str(SCRIPT)], caller, env=env, check=False)

    assert result.returncode == 0, result.stdout
    commands = log_file.read_text(encoding="utf-8")
    image = "harbor.beeintel.com/crawler-platform/gcli2api"
    assert f"-t {image}:v20260727" in commands
    assert f"-t {image}:latest" in commands
    assert f"docker push {image}:v20260727" in commands
    assert f"docker push {image}:latest" in commands
    assert "context-config=SOURCE = 'custom-overlay'" in commands
    assert "context-plugin=enabled" in commands
    assert "发布完成" in result.stdout


def test_release_stops_before_docker_build_when_tests_fail(tmp_path):
    upstream = create_upstream(tmp_path)
    overlay = create_overlay(tmp_path)
    env, log_file = create_fake_toolchain(tmp_path)
    release_env(env, upstream, overlay)
    env["FAIL_RELEASE_TESTS"] = "1"

    result = run([str(SCRIPT)], tmp_path, env=env, check=False)

    assert result.returncode != 0
    commands = log_file.read_text(encoding="utf-8")
    assert "uv run" in commands
    assert "docker build" not in commands
    assert "docker push" not in commands


def test_release_rejects_sensitive_files_in_overlay(tmp_path):
    upstream = create_upstream(tmp_path)
    overlay = create_overlay(tmp_path)
    (overlay / ".env").write_text("SECRET=must-not-copy\n", encoding="utf-8")
    env, log_file = create_fake_toolchain(tmp_path)
    release_env(env, upstream, overlay)

    result = run([str(SCRIPT)], tmp_path, env=env, check=False)

    assert result.returncode != 0
    assert ".env" in result.stdout
    assert not log_file.exists() or "docker " not in log_file.read_text(encoding="utf-8")


def test_compose_passes_split_redis_config_from_dotenv_to_container():
    compose = COMPOSE_FILE.read_text(encoding="utf-8")

    assert "- REDIS_URL=${REDIS_URL:-}" in compose
    assert "- REDIS_USER=${REDIS_USER:-}" in compose
    assert "- REDIS_PASSWORD=${REDIS_PASSWORD:-}" in compose


def test_ghcr_workflow_installs_uv_before_running_release_script():
    workflow = WORKFLOW_FILE.read_text(encoding="utf-8")

    setup_uv = workflow.index("uses: astral-sh/setup-uv@")
    run_release = workflow.index("run: ./scripts/build-and-push.sh")
    assert setup_uv < run_release


def test_repository_overlay_is_an_exact_copy_of_custom_runtime_files():
    actual_paths = {
        path.relative_to(OVERLAY_FILES).as_posix()
        for path in OVERLAY_FILES.rglob("*")
        if path.is_file()
    }
    assert actual_paths == CUSTOM_FILE_PATHS
    for relative_path in CUSTOM_FILE_PATHS:
        assert (OVERLAY_FILES / relative_path).read_bytes() == (REPO_ROOT / relative_path).read_bytes()
