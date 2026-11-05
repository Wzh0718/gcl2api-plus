# 服务器镜像更新完成

目标：http://47.88.89.58:18080/ 。日期：2026-11-04（Asia/Singapore）。

- 部署提交：[5ad971cebb71271f164ed5308829ee3805f55648](https://github.com/Wzh0718/gcl2api-plus/commit/5ad971cebb71271f164ed5308829ee3805f55648)，分支 `codex/agy-server-update`。
- 镜像：服务器本地 `gcli2api:5ad971c-20261104`。
- 镜像 ID：`sha256:213e2c71295a024e397af56c73ac0914f6ab8fb9f1e6b663143ef47c126dc235`。
- 已合并可选原生传输与 400 分类修复。初次部署时开关为 false；按用户后续指令现已持久开启，核验结果见下。
- 使用仓库 Dockerfile 和已提交源码构建新镜像，400 修复不再依赖容器源码补丁。没有推送或改写 GHCR 的 latest。
- 保留服务器原 Compose 配置，仅把服务 image 改为上述本地版本。端口映射和凭证挂载完全保留。
- 更新前后账号均为 48 个、44 个 disabled，没有恢复之前复检失败的账号。

## 验证

- 合并源码全量测试：763 passed。
- Go 单元测试及 vet 通过，Python compileall、JavaScript 语法、Compose 与 diff 检查通过。
- 新镜像在不挂载生产数据、禁用外网的容器中验证了 400 防误停用、OAuth 分类及实际 Go helper 转发。
- 新线上容器 running/healthy；外部面板 HTTP 200；带认证 `/v1/models` HTTP 200，返回 66 个模型。
- 启动日志未发现 Traceback、ModuleNotFoundError、NameError、SyntaxError 或原生传输启动失败。

## 服务器回滚资料

- 回滚镜像：`gcli2api:rollback-20261104-pre5ad971c`，由更新前容器创建，保留当时已生效的 400 修复。
- 构建与部署记录：`/opt/gcli2api/releases/20261104-5ad971c/`。
- 私有配置备份：该目录下 `deployment-backup/`，权限受限，未下载或上传 GitHub。
- 当前 Compose：`/opt/gcli2api/docker-compose.yml`，已固定新镜像，正常重建容器不会丢失本次修复。

此前报告中“400 修复仅为容器源码补丁，重建会丢失”的限制已由本次镜像更新解决。本地 Kimi 未提交工作区保持不变。

## 原生传输已开启

- 服务器 Compose 环境变量 `ANTIGRAVITY_NATIVE_TRANSPORT_ENABLED=true`；数据库配置 `antigravity_native_transport_enabled=true`，均已持久化。
- 容器已重建并恢复 running/healthy。
- 实测运行配置 `native_enabled=true`，应用启动的 `agytransport` 进程 1 个，Unix socket 存在且权限为 0600。
- 通过正在运行的 Go helper 的 Unix socket，使用现有有效账号执行一次真实 `fetchAvailableModels`，上游返回 HTTP 200，返回 20 个模型。核验进程没有启动第二个 helper，也没有替换应用 socket。
- 当前为开启状态，正常重启或按现有 Compose 重建容器后继续生效。
