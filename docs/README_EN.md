# GeminiCLI to API

**Convert GeminiCLI and Antigravity into OpenAI, GEMINI, and Claude API-compatible interfaces.**

[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![License: CNC-1.0](https://img.shields.io/badge/License-CNC--1.0-red.svg)](../LICENSE)
[![Docker](https://img.shields.io/badge/docker-available-blue.svg)](https://github.com/Wzh0718/gcl2api-plus/pkgs/container/gcl2api-plus)

[中文](../README.md) | English

---

## About this repository

This repository, [Wzh0718/gcl2api-plus](https://github.com/Wzh0718/gcl2api-plus), is a customized distribution based on [su-kaka/gcli2api](https://github.com/su-kaka/gcli2api) and follows the upstream license.

The customized distribution adds or improves:

- API key issuance and management;
- Antigravity / RT login and multi-account credential management;
- Token usage, billing, and statistics modules;
- Account health checks, 403 rechecks, and quota reconciliation;
- Proxy groups, OAuth clients, and Redis billing mirrors;
- Stream guarding, image parameter normalization, and model quota fallback chains;
- GitHub Actions synchronization and publishing to GitHub Container Registry.

### Official image

```bash
docker pull ghcr.io/wzh0718/gcl2api-plus:latest
```

Date-versioned images use the `vYYYYMMDD` tag, for example:

```bash
docker pull ghcr.io/wzh0718/gcl2api-plus:v20260910
```

See [CUSTOMIZATION.md](CUSTOMIZATION.md) for the overlay and release workflow.

## Quick deployment

### Docker Run

```bash
docker run -d \
  --name gcli2api \
  --network host \
  -e PASSWORD=pwd \
  -e PORT=7861 \
  -v "$(pwd)/data/creds:/app/creds" \
  ghcr.io/wzh0718/gcl2api-plus:latest
```

For production, use separate API and control-panel passwords:

```bash
docker run -d \
  --name gcli2api \
  --network host \
  -e API_PASSWORD=your_api_password \
  -e PANEL_PASSWORD=your_panel_password \
  -e PORT=7861 \
  -v "$(pwd)/data/creds:/app/creds" \
  ghcr.io/wzh0718/gcl2api-plus:latest
```

### Docker Compose

```bash
docker compose up -d
```

The repository [`docker-compose.yml`](../docker-compose.yml) defaults to `ghcr.io/wzh0718/gcl2api-plus:latest`. After changing `.env`, run `docker compose up -d` again; rebuilding the image is not required.

## Configuration recommendations

Start from the example file:

```bash
cp .env.example .env
```

| Setting | Purpose | Default / recommendation |
| --- | --- | --- |
| `API_PASSWORD` | API endpoint password | Set separately in production |
| `PANEL_PASSWORD` | Control-panel password | Set separately in production |
| `PASSWORD` | Shared password for legacy deployments | Highest priority when set |
| `CREDENTIALS_DIR` | SQLite database and credentials directory | `./creds` |
| `PROXY` | Global proxy | Use `http`, `https`, or `socks5` as needed |
| `REDIS_URL` | Billing mirror and dashboard acceleration | Optional; SQLite remains the fallback |
| `REDIS_USER` / `REDIS_PASSWORD` | Separate Redis authentication | Recommended |
| `ANTIGRAVITY_MODEL_FALLBACK_CHAIN` | Model fallback chain after quota exhaustion | Comma-separated; empty disables it |
| `MAX_NON_STREAM_BUFFER_BYTES` | Maximum non-stream response buffer | `16777216` |
| `CREDENTIAL_CANDIDATE_LIMIT` | Maximum credential candidates per request | `32` |

Configuration precedence is: explicit environment variables > `.env` > SQLite configuration > code defaults. SQLite is the only primary persistence ledger; Redis is only a billing/dashboard mirror.

### Model fallback chain example

```dotenv
ANTIGRAVITY_MODEL_FALLBACK_CHAIN=gemini-2.5-flash,gemini-2.5-flash-lite,gemini-3.1-flash-lite,gemini-3.6-flash-tiered,gemini-3.7-flash-tiered
```

Only an explicitly recognized quota-exhaustion 429 triggers fallback. Image requests do not switch models automatically. See [`config.py`](../config.py) and the control-panel settings page for the complete mapping.

## Control-panel feature previews

### API key management

<img width="1827" height="919" alt="API key management" src="https://github.com/user-attachments/assets/a3f498a2-0693-4391-a55c-9fbc345f3736" />

### RT and Antigravity login

<img width="1816" height="920" alt="RT and Antigravity login" src="https://github.com/user-attachments/assets/c714e6ee-1ce3-4a93-858c-076f3571936b" />

### Credential management improvements

<img width="1806" height="914" alt="Credential management" src="https://github.com/user-attachments/assets/49963ed8-0c50-4466-afdf-e14834d4626a" />

### Statistics module

<img width="1794" height="915" alt="Statistics" src="https://github.com/user-attachments/assets/fe81dd96-5ae9-4600-b9ef-96dbcd2691f6" />

### Settings improvements

<img width="1852" height="917" alt="Settings" src="https://github.com/user-attachments/assets/66ab8494-3ccd-499f-a9ba-987955e73f12" />

### Token statistics

<img width="1831" height="931" alt="Token statistics" src="https://github.com/user-attachments/assets/2eec6103-190a-4237-803b-3c619abcb0c8" />

### Logging improvements

<img width="1832" height="920" alt="Logs" src="https://github.com/user-attachments/assets/05593f22-7c89-4021-b7de-bd70d53d4222" />

## Related documentation

- [Customization and release workflow](CUSTOMIZATION.md)
- [Antigravity image API](ANTIGRAVITY_IMAGE_API.md)
- [Account egress and region checks](ANTIGRAVITY_ACCOUNT_IP_BINDING_PLAN.md)
