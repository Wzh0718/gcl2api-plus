# AGENTS.md

## 仓库形态：独立完整项目（无 overlay）

本仓库自 2026-10-16 起是独立维护的完整项目（见 `docs/ADR-001-完整项目化与agy-exe补充环境.md`）：

- **所有定制直接改仓库主树**，没有 custom-overlay 覆盖层，改什么就发布什么；
- 上游 `su-kaka/gcli2api` remote 保留只读，只做不定期**人工挑拣**（`git fetch upstream` → `git cherry-pick`），没有自动同步；
- 挑拣/合并冲突解决原则：定制功能（计费、调用监控、凭证池、403 复检、模型降级链、敏感词、图像协议等）以我方版本为准。

## 发布（打包推送 GitHub Container Registry）

```bash
./scripts/build-and-push.sh
```

- 流程：`git archive` 导出当前 HEAD → 质量门禁（专项测试、全量 pytest、compileall、`node --check front/common.js`、合并标记检查）→ 构建 → 推送 `ghcr.io/wzh0718/gcl2api-plus:vYYYYMMDD` 和 `latest`；
- **只有已提交进 Git 的内容会进入镜像**；可用 `BUILD_REF` 指定其它 commit/分支/tag；
- 需要先登录 `ghcr.io`；
- 可选环境变量见 `scripts/build-and-push.sh --help`；
- GitHub 侧另有 `.github/workflows/ghcr-release.yml`：**仅手动触发**，与脚本等价，详见 `docs/CUSTOMIZATION.md`。

## 测试

```bash
.venv/bin/python -m pytest tests/ -q
```

venv 中没有安装 ruff，不要假设可用。
