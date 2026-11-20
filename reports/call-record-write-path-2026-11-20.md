# call_records 写路径优化（2026-11-20）

生产 `43.110.150.44` 容器 `gcli2api`。排查起点：「关键词」这个 API key
（`b7ba5859`）近 1 小时 16,088 次进站，端到端 p50 41.2s，而上游真实推理只占 4.0s。

## 一、实测基线

近 1h 成功请求（n=10,487）的四段分解：

| 段 | p50 | 占比 |
|---|---:|---:|
| ① 网关排队（取凭证 → 发出上游） | 13.96s | 36% |
| ② 上游推理（首包 → 末包） | 4.04s | 9% |
| ③ 网关收尾（末包 → 落账完成） | **17.78s** | **43%** |
| 端到端 | 41.21s | — |

现场探针（非流式、`max_tokens=16`）：39.3s / 43.7s / 48.6s，其中上游 0.63–0.81s。

同时 `35%` 的进站请求以本地 429 结束（号池无可用凭证），且要**空等 7.21s** 才拿到 429。

## 二、根因

### 1. 收尾路径写放大

单库 `credentials.db` 3.78 GB，`call_records` 459 万行（正好 30 天保留期），
索引合计 1.18 GB。每个请求收尾要写 **5 次独立事务**：

| 步骤 | 连接 | commit/fsync | 索引维护 |
|---|---|---|---|
| `usage_attempt.confirm()`（`asyncio.to_thread`） | 1 | 1 | PK + bucket |
| `record_api_call_success` | 1 | 1 | — |
| `billing.record` | 1 | 1 | dedupe ×2 |
| `call_monitor.record` → `insert_call_record` | 1 | 1 | 5 棵 B-tree |
| `finish_timing` → `update_call_record_timing` | 1 | 1 | 2 棵 |

其中 `call_records` 是 **先 INSERT 再 UPDATE 同一行**（计时字段收尾才完整）。
`PRAGMA synchronous=2`（FULL）让每次 commit 都 fsync WAL；4.5 rps × 5 ≈ 22 次
fsync/s，全部排在单写锁上。`account_usage` 还挤在默认线程池（4 核仅 8 槽）。

### 2. 空间分布

| 项 | 大小 |
|---|---|
| `call_records` 表 | 2555.7 MB |
| `idx_call_records_request_id` | **554.8 MB** |
| 其余 4 个索引 | 625.0 MB |
| `billing_request_dedupe` + 自增索引 | 118 MB |

`timing_details` 平均 496 B/行（占行宽约 85%，明文 JSON）。

## 三、改动

1. **统一连接参数**（新增 `src/storage/sqlite_pragmas.py`，104 处连接点接入）
   - `synchronous=NORMAL`（WAL 官方推荐；崩溃只丢最后一次 checkpoint 后的事务）
   - `wal_autocheckpoint=20000`（默认 4MB 一次 checkpoint → 约 78MB）
   - `cache_size=-8192`（8MB：连接是短命的，缓存无法跨请求复用，调大只会按并发数放大内存）、
     `temp_store=MEMORY`、`busy_timeout=5000`
2. **call_records 批量写**：`record()` 入队（保留行对象引用），后台协程按 200ms
   窗口攒批、单事务 `executemany`；读路径「读前刷写」，队列满丢遥测不影响账务。
3. **去双写**：收尾时把计时字段回填到同一行对象；只有该行已被批量写出去
   （`_flushed`）才补一次 UPDATE。
4. **删 `idx_call_records_request_id`**：面板详情改按主键 `id` 查
   （`/call-monitor/content?record_id=`），旧 `request_id` 参数保留兼容。
5. **`timing_details` 压缩存储**：复用 `request_details` 的 zlib + 校验方案，
   实测 496 B → 160 B；读路径 `decode_details` 兼容旧明文。
6. **account_usage 专用线程池**：`SQLITE_THREAD_WORKERS`（默认 32），
   不再与其它阻塞调用抢默认执行器；写连接加 PRAGMA、锁等待 2s → 5s。
7. **维护 CLI**：`src/storage/call_record_content.py` 新增 `--column timing_details|both`
   与 `--drop-legacy-index`，用于历史压缩/删索引（分批、可中断、每批校验）。

## 四、验证

- 全量 `pytest tests/ -q`：**1544 passed**。
- 本地起服（`PORT=17861`）+ 真实请求：
  - 429 请求 50ms 返回（生产同路径 7.2s）；
  - `call_records` 写入成功，`timing_details` 落库为 blob(266B)；
  - `call_records` 索引只剩 4 个（`request_id` 索引已不存在）；
  - `/call-monitor/realtime` 返回记录含 `id`；`/call-monitor/content?record_id=1` → 200；
    旧 `?request_id=` → 200；无参数 → 422。
- 应用连接 PRAGMA 实测：`synchronous=1`、`wal_autocheckpoint=20000`、
  `cache_size=-65536`、`busy_timeout=5000`、`temp_store=2`。
- 200 条记录：入队 0.7ms，flush 落盘 11.8ms（约 0.06ms/条）。

## 五、上线步骤

1. 构建推送镜像 → 生产改 compose 镜像 tag → `docker compose up -d`；
2. 首次启动会执行一次 `DROP INDEX IF EXISTS idx_call_records_request_id`
   （4.6GB 库上约数十秒）。**建议先手工低峰执行**，让启动时成为 no-op：
   ```bash
   docker exec gcli2api python -m src.storage.call_record_content \
     /app/creds/credentials.db --drop-legacy-index --batch-size 8
   ```
3. 可选历史压缩（可中断、每批校验，先备份）：
   ```bash
   docker exec gcli2api python -m src.storage.call_record_content \
     /app/creds/credentials.db --column timing_details --batch-size 8
   ```
   压缩后如需回收磁盘再加 `--vacuum`（需要约 2× 库大小的临时空间）。
4. 观察 `call_records` 的增长与面板延迟；`timing_details` 读取走
   `decode_details`，新旧数据混存无需迁移。

## 六、回滚

代码回滚到本提交之前的镜像即可；数据无需回滚：
- 压缩后的 `timing_details` 旧代码用 `json.loads` 会失败 → 若必须回滚代码，
  先确认旧镜像是否已支持 `decode_details`（不支持则需回滚前不做压缩）。

## 七、未做（下一轮）

物理拆库（`credentials.db` / `analytics.db` 分离）。本轮五项已把收尾从
17.8s 移出请求路径，拆库的边际收益主要是消除剩余写锁争用；但涉及
`billing_daily × api_keys` 等跨表 JOIN 改造与 4.6GB 数据迁移窗口，
建议等本轮线上验证稳定后单独评估。
