import os
import re
import stat
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "build-and-push.sh"
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"
WORKFLOW_FILE = REPO_ROOT / ".github" / "workflows" / "ghcr-release.yml"

# 端到端执行发布脚本的用例只在本机 git 检出中有效：
# 发布门禁会在 git archive 导出的源码树里再跑一遍 pytest，
# 导出树没有 .git，脚本无法解析 BUILD_REF，这些用例必须跳过。
requires_git_checkout = pytest.mark.skipif(
    not (REPO_ROOT / ".git").exists(),
    reason="发布脚本端到端测试需要 git 检出（git archive 依赖 .git）",
)


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


def create_fake_toolchain(tmp_path: Path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log_file = tmp_path / "commands.log"

    uv = bin_dir / "uv"
    uv.write_text(
        "#!/usr/bin/env bash\n"
        "printf 'uv %s\\n' \"$*\" >> \"$RELEASE_TEST_LOG\"\n"
        # 质量门禁必须在导出源码目录内运行
        "[[ -f config.py ]] || exit 42\n"
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
        "if [[ \"$1\" == \"version\" && \"$2\" == \"buildx\" ]]; then exit 0; fi\n"
        "if [[ \"$1\" == \"build\" ]] || [[ \"$1\" == \"buildx\" && \"$2\" == \"build\" ]]; then\n"
        "  context=\"${@: -1}\"\n"
        "  printf 'context-config=%s\\n' \"$(head -1 \"$context/config.py\")\" >> \"$RELEASE_TEST_LOG\"\n"
        # 未提交进 Git 的文件不得进入构建上下文
        "  [[ ! -e \"$context/local-only-test-artifact.txt\" ]] || exit 53\n"
        "  [[ ! -e \"$context/.env\" ]] || exit 51\n"
        "  [[ ! -e \"$context/creds\" ]] || exit 52\n"
        "  [[ -f \"$context/.dockerignore\" ]] || exit 54\n"
        "fi\n",
        encoding="utf-8",
    )
    for executable in (uv, node, python, docker):
        executable.chmod(executable.stat().st_mode | stat.S_IXUSR)

    env = os.environ.copy()
    env.update({"PATH": f"{bin_dir}:{env['PATH']}", "RELEASE_TEST_LOG": str(log_file)})
    return env, log_file


def release_env(env):
    env.pop("PYTHONPATH", None)
    env.update(
        {
            "IMAGE_NAME": "ghcr.io/wzh0718/gcl2api-plus",
            "RELEASE_DATE": "20260727",
        }
    )
    return env


@pytest.fixture
def untracked_artifact():
    """在仓库根放一个未跟踪文件，验证它不会随 git archive 进入镜像。"""
    artifact = REPO_ROOT / "local-only-test-artifact.txt"
    artifact.write_text("must not enter image\n", encoding="utf-8")
    try:
        yield artifact
    finally:
        artifact.unlink(missing_ok=True)


@requires_git_checkout
def test_release_exports_committed_tree_and_pushes_date_and_latest(tmp_path, untracked_artifact):
    env, log_file = create_fake_toolchain(tmp_path)
    release_env(env)

    result = run([str(SCRIPT)], tmp_path, env=env, check=False)

    assert result.returncode == 0, result.stdout
    commands = log_file.read_text(encoding="utf-8")
    image = "ghcr.io/wzh0718/gcl2api-plus"
    assert f"-t {image}:v20260727" in commands
    assert f"-t {image}:latest" in commands
    assert "docker buildx build --platform linux/amd64,linux/arm64" in commands
    assert " --push " in commands
    assert "index:org.opencontainers.image.description=" in commands
    assert "docker push" not in commands
    assert "发布完成" in result.stdout


@requires_git_checkout
def test_release_defaults_to_github_container_registry(tmp_path):
    env, log_file = create_fake_toolchain(tmp_path)
    release_env(env)
    env.pop("IMAGE_NAME")

    result = run([str(SCRIPT)], tmp_path, env=env, check=False)

    assert result.returncode == 0, result.stdout
    commands = log_file.read_text(encoding="utf-8")
    image = "ghcr.io/wzh0718/gcl2api-plus"
    assert f"-t {image}:v20260727" in commands
    assert "--platform linux/amd64,linux/arm64" in commands
    assert " --push " in commands


@requires_git_checkout
def test_release_honors_platforms_override_for_single_arch(tmp_path):
    env, log_file = create_fake_toolchain(tmp_path)
    release_env(env)
    env["PLATFORMS"] = "linux/amd64"

    result = run([str(SCRIPT)], tmp_path, env=env, check=False)

    assert result.returncode == 0, result.stdout
    commands = log_file.read_text(encoding="utf-8")
    assert "docker buildx build --platform linux/amd64 " in commands
    assert "arm64" not in commands
    # 单平台推送没有 manifest index，不应传 index 注释
    assert "index:org.opencontainers.image.description=" not in commands


def test_image_description_label_and_manifest_annotation_stay_in_sync():
    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    script = SCRIPT.read_text(encoding="utf-8")

    label = re.search(
        r'org\.opencontainers\.image\.description="([^"]+)"', dockerfile
    )
    assert label, "Dockerfile 缺少 org.opencontainers.image.description 标签"

    assert (
        "index:org.opencontainers.image.description=${IMAGE_DESCRIPTION}" in script
    ), "构建脚本未把描述写入 manifest index 注释（多架构镜像的 GHCR 包描述）"
    # 两处文案必须一致：单架构镜像描述来自 Dockerfile 标签，多架构来自 index 注释
    assert f"IMAGE_DESCRIPTION:-{label.group(1)}" in script


@requires_git_checkout
def test_release_labels_image_source_with_repository_and_revision(tmp_path):
    env, log_file = create_fake_toolchain(tmp_path)
    release_env(env)

    result = run([str(SCRIPT)], tmp_path, env=env, check=False)

    assert result.returncode == 0, result.stdout
    commands = log_file.read_text(encoding="utf-8")
    # GitHub 用 source 标签做「包 → 仓库」关联，必须是构建发布方（本仓库）
    assert (
        "--label org.opencontainers.image.source=https://github.com/Wzh0718/gcl2api-plus"
        in commands
    )
    # revision 标签记录本仓库构建提交
    assert "--label org.opencontainers.image.revision=" in commands


@requires_git_checkout
def test_release_stops_before_docker_build_when_tests_fail(tmp_path):
    env, log_file = create_fake_toolchain(tmp_path)
    release_env(env)
    env["FAIL_RELEASE_TESTS"] = "1"

    result = run([str(SCRIPT)], tmp_path, env=env, check=False)

    assert result.returncode != 0
    commands = log_file.read_text(encoding="utf-8")
    assert "uv run" in commands
    assert "docker buildx build" not in commands
    assert "docker push" not in commands


@requires_git_checkout
def test_release_rejects_unresolvable_build_ref(tmp_path):
    env, log_file = create_fake_toolchain(tmp_path)
    release_env(env)
    env["BUILD_REF"] = "definitely-not-a-ref"

    result = run([str(SCRIPT)], tmp_path, env=env, check=False)

    assert result.returncode != 0
    assert "BUILD_REF" in result.stdout
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


def test_ghcr_workflow_sets_up_qemu_for_multi_arch_build():
    workflow = WORKFLOW_FILE.read_text(encoding="utf-8")

    setup_qemu = workflow.index("uses: docker/setup-qemu-action@")
    setup_buildx = workflow.index("uses: docker/setup-buildx-action@")
    run_release = workflow.index("run: ./scripts/build-and-push.sh")
    assert setup_qemu < setup_buildx < run_release


def test_ghcr_workflow_passes_github_image_name_to_release_script():
    workflow = WORKFLOW_FILE.read_text(encoding="utf-8")

    assert "IMAGE_NAME: ${{ env.GHCR_IMAGE }}" in workflow
    assert "HARBOR_IMAGE" not in workflow


def test_ghcr_workflow_is_manual_trigger_only():
    workflow = WORKFLOW_FILE.read_text(encoding="utf-8")

    assert "workflow_dispatch" in workflow
    assert "cron:" not in workflow
    assert "schedule:" not in workflow
