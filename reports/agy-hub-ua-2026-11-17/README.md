# 官方 Hub 最新 UA 核验（2026-11-17）

本次直接查询官方 Hub releases 接口，列出的最高版本为 **2.32.2**，execution ID `4665802025205760`。不是将旧记录当作最新结果。

官方二进制 239952296 字节，SHA256 `ad10256e5a4e2e6d8b4e40680b708793681ee723969bdf807aecfbfd05874e0c`。这是本地计算的样本标识，未声称官方另行公布了相同 SHA256。`--stamp` 显示构建时间 2026-11-16 20:12:48 UTC、CL **1022991777**。

## 动态观察

在 `docker --network none` 容器内运行官方二进制，空白专用 HOME、合成 access token，将 cloud_code_endpoint 指向容器内 HTTP stub。stub 返回 401，不连接 Google、不使用生产账号。启动参数按已有 Hub 样本指定 ide_name=antigravity、subclient_type=hub、ide_version=2.32.2、user_agent_name=antigravity。版本名称来自官方 releases；CL 从官方二进制自行取得。

实际捕获 `POST /v1internal:loadCodeAssist`：

```text
antigravity/hub/2.32.2 (aidev_client; os_type=linux; arch=amd64; cl=1022991777)
```

这确认官方程序在该启动方式下实际生成并发送的 HTTP UA，不是网关拼接预测；401 为本地 stub 主动返回，不能用于判断 Google 是否接受新版本。

## 与 2.31.2 对比

| 字段 | 旧官方样本 | 新官方样本 |
| --- | --- | --- |
| 版本 | 2.31.2 | 2.32.2 |
| CL | 1019359392 | 1022991777 |
| 上述业务 UA 格式 | aidev_client、os_type、arch、cl | 相同 |

恢复 Go pclntab 并核对定向反汇编：两个样本的 `codeassistclient.userAgent` 函数长度均为 2976 字节，相关格式串相同，包括可选 `auth_method=%s` 和 `purpose=%s`。因此这两个可选字段不是 2.32.2 才新增。函数长度相同不单独证明全部语义相同；本次动态样本中这两项未出现。

当前仓库默认仍是 2.31.0 / 1017595969；生产面板可能覆盖默认，本轮没有读取或修改生产生效值。

## 范围

本轮仅动态捕获上述业务请求，没有重新捕获新版本的模型生成、OAuth 或遥测请求；不能把旧版的 Go-http-client/2.0 观察自动标为新版实测。没有进行模型可用性、429、风控或账号停用测试。

官方地址、时间、隔离方式、完整 UA、哈希和限制见 `summary.json`；原始 releases 快照、stamp、请求元数据及静态证据在相邻文件。二进制留在 /tmp，未纳入仓库。无提交或部署。
