# AGENTS.md

## 定制覆盖层（custom-overlay）必须同步

本仓库发布到 Harbor 的镜像 = GitHub 上游 master + `custom-overlay/files/` 覆盖层。
**任何对运行时代码的修改，如果只改了仓库主树而没有同步覆盖层，发布时会被上游原版顶掉，改动不会进镜像。**

规则：

- 修改了 `custom-overlay/files/` 中已存在的文件（如 `src/api/antigravity.py`、`src/storage/sqlite_manager.py`、`front/common.js` 等），改完必须把主树文件拷贝到 overlay 对应路径；
- 新增了需要进镜像的文件（含新测试），既要拷入 overlay，也要把相对路径登记进 `tests/test_build_and_push_script.py` 的 `CUSTOM_FILE_PATHS`；
- `tests/test_build_and_push_script.py::test_repository_overlay_is_an_exact_copy_of_custom_runtime_files` 会强制校验 overlay 与清单完全一致，改完跑一遍全量 pytest 确认。

## 发布（打包推送 Harbor）

```bash
./scripts/build-and-push.sh
```

- 流程：拉上游 master → 应用 overlay → 质量门禁（专项测试、全量 pytest、compileall、`node --check front/common.js`、diff 空白检查）→ 构建 → 推送 `harbor.beeintel.com/crawler-platform/gcli2api:vYYYYMMDD` 和 `latest`；
- 需要先 `docker login harbor.beeintel.com`；
- 可选环境变量见 `scripts/build-and-push.sh --help`。

## 测试

```bash
.venv/bin/python -m pytest tests/ -q
```

venv 中没有安装 ruff，不要假设可用。
