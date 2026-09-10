# GeminiCLI to API

**将 GeminiCLI 和 Antigravity 转换为 OpenAI 、GEMINI 和 Claude API 兼容接口**

[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![License: CNC-1.0](https://img.shields.io/badge/License-CNC--1.0-red.svg)](LICENSE)
[![Docker](https://img.shields.io/badge/docker-available-blue.svg)](https://github.com/su-kaka/gcli2api/pkgs/container/gcli2api)

[English](docs/README_EN.md) | 中文 | [日本語](docs/README_JA.md)

---

> ## 关于本仓库（二次开发说明）
>
> 本仓库（[Wzh0718/gcl2api-plus](https://github.com/Wzh0718/gcl2api-plus)）基于上游项目 **[su-kaka/gcli2api](https://github.com/su-kaka/gcli2api)** 二次开发，遵循上游原有许可证。以下「安装指南」「API 使用方式」等章节均来自上游文档，接口用法与上游一致。
>
> **二次开发新增的能力**（详见 [docs/CUSTOMIZATION.md](docs/CUSTOMIZATION.md) 与 [AGENTS.md](AGENTS.md)）：
>
> - **定制覆盖层发布体系**：`custom-overlay/files/` 覆盖层 + `scripts/build-and-push.sh`，发布时拉取上游最新 `master` 再应用定制，避免合并冲突；
> - **控制面板增强**：多 Antigravity 账号集中管理，Token 用量与费用（billing）统计、账号健康检查、403 复核与额度对账；
> - **API Key 管理**：独立的 Key 签发与管理（`src/api_keys.py`）；
> - **代理分组**：代理池分组管理（`src/proxy_groups.py`）；
> - **告警与集成**：钉钉告警（`src/dingtalk_alert.py`）、OAuth 客户端管理、Redis 配置、流式响应守护（stream guard）等；
> - **自动同步发布**：GitHub Action 每天定时拉取上游更新，应用覆盖层并跑完全量质量门禁后，自动构建镜像推送到 `ghcr.io/wzh0718/gcl2api-plus`（`vYYYYMMDD` + `latest` 两个 Tag）。
>
> 使用本仓库的镜像：
>
> ```bash
> docker pull ghcr.io/wzh0718/gcl2api-plus:latest
> ```
>
> ---

# 添加API Key 管理
<img width="1827" height="919" alt="image" src="https://github.com/user-attachments/assets/a3f498a2-0693-4391-a55c-9fbc345f3736" />

# 添加 RT和提供Antigravity登陆
<img width="1816" height="920" alt="image" src="https://github.com/user-attachments/assets/c714e6ee-1ce3-4a93-858c-076f3571936b" />

# 凭证管理功能优化
<img width="1806" height="914" alt="image" src="https://github.com/user-attachments/assets/49963ed8-0c50-4466-afdf-e14834d4626a" />

# 添加统计模块
<img width="1794" height="915" alt="image" src="https://github.com/user-attachments/assets/fe81dd96-5ae9-4600-b9ef-96dbcd2691f6" />

# 优化setting
<img width="1852" height="917" alt="image" src="https://github.com/user-attachments/assets/66ab8494-3ccd-499f-a9ba-987955e73f12" />

# 添加tokens统计
<img width="1831" height="931" alt="image" src="https://github.com/user-attachments/assets/2eec6103-190a-4237-803b-3c619abcb0c8" />

# 日志模块优化
<img width="1832" height="920" alt="image" src="https://github.com/user-attachments/assets/05593f22-7c89-4021-b7de-bd70d53d4222" />



