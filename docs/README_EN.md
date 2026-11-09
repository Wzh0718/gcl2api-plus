# gcl2api-plus

**Antigravity and Vertex integration with API key management, usage billing, and multi-account credential management.**

Antigravity provides OpenAI Chat Completions / Responses, Gemini, and Anthropic Messages compatible interfaces. Vertex provides OpenAI Chat Completions and Gemini compatible interfaces. The current version no longer provides the GeminiCLI channel.

[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![License: CNC-1.0](https://img.shields.io/badge/License-CNC--1.0-red.svg)](../LICENSE)
[![Docker](https://img.shields.io/badge/docker-available-blue.svg)](https://github.com/Wzh0718/gcl2api-plus/pkgs/container/gcl2api-plus)

[中文](../README.md) | English

---

## About this repository

This repository, [Wzh0718/gcl2api-plus](https://github.com/Wzh0718/gcl2api-plus), is a customized distribution based on [su-kaka/gcli2api](https://github.com/su-kaka/gcli2api) and follows the upstream license.

### Current features

This repository is maintained independently from upstream. Its main capabilities and custom improvements are:

| Feature | Details |
| --- | --- |
| API compatibility | Antigravity supports OpenAI Chat Completions / Responses, Gemini, and Anthropic Messages. Vertex supports OpenAI Chat Completions and Gemini. The GeminiCLI channel is no longer provided. |
| API key management | Issue, copy, enable, disable, and revoke keys; configure monetary quotas, expiration, and manual or monthly quota resets; inspect usage and remaining balances per key. |
| Login and credential pool | Antigravity OAuth login, Refresh Token (RT) import, and multi-account management; rotation across available accounts, per-account concurrency controls, and credential switching on errors. |
| Account checks and recovery | Account health, model quota, and network eligibility checks; periodic rechecks for recoverable 403 errors, manual rechecks only for explicit TOS / account_forbidden errors, and quota reconciliation. |
| Gradual account ramp-up | Enable per account or in bulk; progressively increase traffic using real requests, with concurrency and pacing limits per account and shared egress IP, plus backoff or suspension on errors. See the [state machine specification](ANTIGRAVITY_DYNAMIC_WARMUP_SPEC.md). |
| Account proxies and egress | Inherited proxies, account-specific proxies, direct connections, and proxy groups; optional exclusive account-to-node binding with egress IP, region eligibility, and proxy health checks. |
| Token usage and billing | Configurable model token prices, request deduplication, summaries, rankings, and per-key bills; historical bills retain their original prices. SQLite stores the records; optional Redis mirrors and dashboards fall back to SQLite on failure. |
| Call monitoring and logs | Inspect live calls, usage, and logs; compare requested models with the upstream execution model returned in `modelVersion`, and persist execution model records. |
| Streaming and continuation | Stream idle timeout protection and the `假流式/`, `流式抗截断/`, and `抗截断/` model prefixes; automatic continuation applies only to streaming requests, with the legacy prefix retained. |
| Quota fallback | Optional model fallback chains, disabled by default; triggered only by explicit quota-exhaustion 429 errors. Image requests do not use this fallback chain. |
| Image parameters and transport | Gemini image parameters, OpenAI Chat Completions top-level `size`, and legacy size suffixes; dedicated image transport, capacity error handling, and bounded retries. See the [image API guide](ANTIGRAVITY_IMAGE_API.md). |
| Client name filtering | Rewrite client names in system prompts or block matching requests locally, using built-in or custom rules; user messages and tool calls remain unchanged. |
| Deployment and releases | Desktop and mobile control panels, Docker / Compose deployment, and multi-architecture GHCR images; releases use a local script or manually triggered GitHub Actions, with no automatic upstream synchronization. |

See the [development and release guide](CUSTOMIZATION.md) for configuration, defaults, and activation requirements.

### Official image

```bash
docker pull ghcr.io/wzh0718/gcl2api-plus:latest
```

Date-versioned images use the `vYYYYMMDD` tag, for example:

```bash
docker pull ghcr.io/wzh0718/gcl2api-plus:v20260910
```

This repository is a standalone project (no overlay, no automatic upstream sync). See [CUSTOMIZATION.md](CUSTOMIZATION.md) for the development and release workflow.

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
| `REDIS_URL` | Optional billing mirror | Optional; SQLite remains the primary ledger |
| `BILLING_DASHBOARD_SOURCE` | Dashboard source: `sqlite` or `redis` | `sqlite`; an unavailable Redis mirror falls back to SQLite |
| `REDIS_USER` / `REDIS_PASSWORD` | Separate Redis authentication | Recommended |
| `ANTIGRAVITY_MODEL_FALLBACK_CHAIN` | Model fallback chain after quota exhaustion | Comma-separated; empty disables it |
| `MAX_NON_STREAM_BUFFER_BYTES` | Maximum non-stream response buffer | `16777216` |
| `CREDENTIAL_CANDIDATE_LIMIT` | Maximum credential candidates per request | `32` |

Configuration precedence is: explicit environment variables > `.env` > SQLite configuration > code defaults. SQLite is the only primary persistence ledger; Redis is only a billing/dashboard mirror.

### Model fallback chain example

Disabled by default (empty). Configure it explicitly to enable, for example:

```dotenv
ANTIGRAVITY_MODEL_FALLBACK_CHAIN=gemini-3.8-flash,gemini-3.8-flash-tiered
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

- [Development and release workflow](CUSTOMIZATION.md)
- [Antigravity image API](ANTIGRAVITY_IMAGE_API.md)
- [Account egress and region checks](ANTIGRAVITY_ACCOUNT_IP_BINDING_PLAN.md)

## Acknowledgements

- Thanks to the upstream project [su-kaka/gcli2api](https://github.com/su-kaka/gcli2api); this repository is a derivative work and follows the upstream license.
- Thanks to the [LINUX DO](https://linux.do) community for providing a platform for discussion and open-source promotion. This project acknowledges and links to the LINUX DO community.

## Disclaimer

- This project is for learning and technical research only. **Do not use it in any way that violates Google's Terms of Service, the upstream project's license, or applicable laws and regulations.**
- This project is not affiliated with or endorsed by Google. Gemini, GeminiCLI, Antigravity, and related names and trademarks belong to their respective owners.
- Using this project may involve risks such as account restriction, suspension, or quota deduction. **You assume all consequences and responsibilities arising from your use.**
- This project is provided "as is", without warranty of any kind, express or implied. The authors are not liable for any direct or indirect damages resulting from its use.
- Do not use this project for commercial resale, bulk abuse, or any activity that infringes the rights of third parties.
- If any content of this project infringes your legitimate rights, please open a GitHub Issue and it will be handled promptly after verification.
