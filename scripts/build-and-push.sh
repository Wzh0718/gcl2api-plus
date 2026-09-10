#!/usr/bin/env bash

set -Eeuo pipefail

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

readonly UPSTREAM_URL="${UPSTREAM_URL:-https://github.com/su-kaka/gcli2api.git}"
readonly UPSTREAM_BRANCH="${UPSTREAM_BRANCH:-master}"
readonly CUSTOM_OVERLAY_DIR="${CUSTOM_OVERLAY_DIR:-${REPO_ROOT}/custom-overlay/files}"
readonly IMAGE_NAME="${IMAGE_NAME:-ghcr.io/wzh0718/gcl2api-plus}"
readonly IMAGE_REGISTRY="${IMAGE_NAME%%/*}"
readonly RELEASE_DATE="${RELEASE_DATE:-$(TZ=Asia/Shanghai date +%Y%m%d)}"
readonly VERSION_TAG="v${RELEASE_DATE}"
readonly DATE_IMAGE="${IMAGE_NAME}:${VERSION_TAG}"
readonly LATEST_IMAGE="${IMAGE_NAME}:latest"

RELEASE_WORK_DIR=""
SOURCE_DIR=""

usage() {
    cat <<'EOF'
用法:
  ./scripts/build-and-push.sh

执行流程:
  1. 从 GitHub 拉取最新 master 到临时目录
  2. 使用 custom-overlay/files 覆盖定制文件
  3. 运行专项测试、完整测试和静态检查
  4. 构建一次 Docker 镜像
  5. 推送 vYYYYMMDD 和 latest 两个 Tag 到 GitHub Container Registry

首次使用前请登录镜像仓库（GitHub Container Registry）:
  echo "\$CR_PAT" | docker login ghcr.io -u <GitHub 用户名> --password-stdin

可选环境变量:
  UPSTREAM_URL        上游 Git 地址
  UPSTREAM_BRANCH     上游分支，默认 master
  CUSTOM_OVERLAY_DIR  定制覆盖目录
  IMAGE_NAME          GitHub Container Registry 镜像名，默认 ghcr.io/wzh0718/gcl2api-plus
  RELEASE_DATE        测试/补发日期，格式 YYYYMMDD
  KEEP_BUILD_DIR=1    保留临时构建目录用于排查
EOF
}

die() {
    printf '错误: %s\n' "$*" >&2
    exit 1
}

cleanup() {
    if [[ -z "${RELEASE_WORK_DIR}" ]] || [[ ! -d "${RELEASE_WORK_DIR}" ]]; then
        return
    fi
    if [[ "${KEEP_BUILD_DIR:-0}" == "1" ]]; then
        printf '构建目录保留: %s\n' "${RELEASE_WORK_DIR}"
        return
    fi
    rm -rf -- "${RELEASE_WORK_DIR}"
}

trap cleanup EXIT

if [[ "${1:-}" == "--help" ]] || [[ "${1:-}" == "-h" ]]; then
    usage
    exit 0
fi
[[ $# -eq 0 ]] || die "不支持参数: $*. 使用 --help 查看说明。"

require_command() {
    command -v "$1" >/dev/null 2>&1 || die "缺少命令: $1"
}

validate_configuration() {
    [[ "${RELEASE_DATE}" =~ ^[0-9]{8}$ ]] \
        || die "RELEASE_DATE 必须是 YYYYMMDD，例如 20260727。"
    [[ -d "${CUSTOM_OVERLAY_DIR}" ]] \
        || die "定制覆盖目录不存在: ${CUSTOM_OVERLAY_DIR}"
    [[ -n "$(find "${CUSTOM_OVERLAY_DIR}" -type f -print -quit)" ]] \
        || die "定制覆盖目录为空: ${CUSTOM_OVERLAY_DIR}"

    local forbidden_file
    forbidden_file="$(find "${CUSTOM_OVERLAY_DIR}" -type f \( \
        -name '.env' -o \
        -name '*.db' -o \
        -name '*.db-shm' -o \
        -name '*.db-wal' -o \
        -name '*.sqlite' -o \
        -name '*.sqlite3' \
        \) -print -quit)"
    [[ -z "${forbidden_file}" ]] \
        || die "定制覆盖目录包含禁止文件: ${forbidden_file}"

    local forbidden_dir
    forbidden_dir="$(find "${CUSTOM_OVERLAY_DIR}" -type d \( \
        -name '.git' -o \
        -name '.codegraph' -o \
        -name 'creds' \
        \) -print -quit)"
    [[ -z "${forbidden_dir}" ]] \
        || die "定制覆盖目录包含禁止目录: ${forbidden_dir}"
}

run_quality_gates() {
    local verify_tmp
    verify_tmp="$(mktemp -d "${TMPDIR:-/tmp}/gcli2api-release-verify.XXXXXX")"

    (
        set -Eeuo pipefail
        trap 'rm -rf -- "${verify_tmp}"' EXIT
        cd "${SOURCE_DIR}"

        export CREDENTIALS_DIR="${verify_tmp}/creds"
        export POSTGRESQL_URI=""
        export MONGODB_URI=""
        # 仅隔离发布前测试，避免测试连接操作者的 Redis；此子 Shell 不影响镜像或运行时配置。
        export REDIS_URL=""
        export PYTHONDONTWRITEBYTECODE=1
        export PYTHONPATH="${SOURCE_DIR}"
        export UV_CACHE_DIR="${UV_CACHE_DIR:-${verify_tmp}/uv-cache}"

        printf '\n[1/6] SQLite 计费与 Proxy 专项测试\n'
        uv run --with pytest --with pytest-asyncio pytest \
            tests/test_antigravity_403_recheck.py \
            tests/test_antigravity_account_health.py \
            tests/test_billing.py \
            tests/test_proxy_context.py \
            tests/test_proxy_groups.py \
            tests/test_sqlite_billing_schema.py -q

        printf '\n[2/6] 完整 pytest\n'
        uv run --with pytest --with pytest-asyncio pytest -q

        printf '\n[3/6] Python 编译检查\n'
        python3 -m compileall -q src tests web.py config.py

        printf '\n[4/6] JavaScript 语法检查\n'
        node --check front/common.js

        printf '\n[5/6] 将新覆盖文件加入差异检查范围\n'
        git add -N .

        printf '\n[6/6] 覆盖层差异空白检查\n'
        git diff --check
    )
}

validate_configuration
require_command git
require_command uv
require_command python3
require_command node
require_command docker

printf '发布目标:\n'
printf '  上游: %s (%s)\n' "${UPSTREAM_URL}" "${UPSTREAM_BRANCH}"
printf '  覆盖层: %s\n' "${CUSTOM_OVERLAY_DIR}"
printf '  日期镜像: %s\n' "${DATE_IMAGE}"
printf '  最新镜像: %s\n' "${LATEST_IMAGE}"

printf '\n==> 检查 Docker 服务\n'
docker info >/dev/null 2>&1 \
    || die "无法连接 Docker。请确认 Docker 已启动且当前用户有权限访问。"

RELEASE_WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/gcli2api-release.XXXXXX")"
SOURCE_DIR="${RELEASE_WORK_DIR}/source"

printf '\n==> 拉取最新 GitHub 源码\n'
git clone --depth 1 --single-branch --branch "${UPSTREAM_BRANCH}" \
    "${UPSTREAM_URL}" "${SOURCE_DIR}"

UPSTREAM_COMMIT="$(git -C "${SOURCE_DIR}" rev-parse HEAD)"
printf '上游提交: %s\n' "${UPSTREAM_COMMIT}"

printf '\n==> 覆盖定制插件文件\n'
cp -a "${CUSTOM_OVERLAY_DIR}/." "${SOURCE_DIR}/"
[[ -f "${SOURCE_DIR}/Dockerfile" ]] || die "覆盖后的源码缺少 Dockerfile。"
printf '已覆盖 %s 个文件。\n' "$(find "${CUSTOM_OVERLAY_DIR}" -type f | wc -l | tr -d ' ')"

printf '\n==> 运行发布质量检查\n'
run_quality_gates

BUILD_CREATED="$(TZ=Asia/Shanghai date -Iseconds)"
printf '\n==> 构建一次镜像，同时标记 %s 和 latest\n' "${VERSION_TAG}"
docker build --pull \
    --label "org.opencontainers.image.source=${UPSTREAM_URL}" \
    --label "org.opencontainers.image.revision=${UPSTREAM_COMMIT}" \
    --label "org.opencontainers.image.created=${BUILD_CREATED}" \
    -t "${DATE_IMAGE}" \
    -t "${LATEST_IMAGE}" \
    "${SOURCE_DIR}"

printf '\n==> 推送日期镜像 %s\n' "${DATE_IMAGE}"
docker push "${DATE_IMAGE}" \
    || die "日期镜像推送失败。请先执行 docker login ${IMAGE_REGISTRY}。"

printf '\n==> 推送 latest 镜像 %s\n' "${LATEST_IMAGE}"
docker push "${LATEST_IMAGE}" \
    || die "latest 镜像推送失败。请先执行 docker login ${IMAGE_REGISTRY}。"

printf '\n发布完成:\n'
printf '  %s\n' "${DATE_IMAGE}"
printf '  %s\n' "${LATEST_IMAGE}"
printf '  上游提交: %s\n' "${UPSTREAM_COMMIT}"
