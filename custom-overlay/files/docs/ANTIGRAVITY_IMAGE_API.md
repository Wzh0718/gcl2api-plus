# Antigravity 图片生成 API 对接说明

本文面向调用 `gemini-3.1-flash-image` 的服务接入方，记录 gcli2api 的上游调用链、Google 官方参数兼容方式、OpenAI 兼容方式、尺寸映射以及验证边界。

最后更新：2026-08-06。

## 1. 结论

- 推荐直接使用基础模型名 `gemini-3.1-flash-image`，不再要求把尺寸或比例拼到模型名后面。
- Google REST 请求优先使用 `generationConfig.responseFormat.image`。
- Google SDK 风格的 `imageConfig` / `image_config` 继续兼容。
- OpenAI Chat Completions 风格通过顶层 `size` 指定图片意图尺寸。
- 历史模型后缀仍保留为最低优先级兜底，例如 `-2k-9x16`，新接入不要依赖它。
- 图片响应仍从 Gemini `candidates[].content.parts[].inlineData` 读取；OpenAI 入口会由现有响应转换器转成 OpenAI Chat Completions 响应。

Google 当前文档把 Interactions API 作为主要图片生成接口，并把本文使用的 `generateContent` 标为 legacy。gcli2api 目前仍以现有 `generateContent` 路由为兼容目标，尚未实现 `/interactions`；因此这里的“官方兼容”特指 Google 官方 `generateContent` 请求字段兼容，不表示已经覆盖 Google 的全部新接口。

参数优先级固定为：

```text
responseFormat.image
  > imageConfig / image_config
  > OpenAI 顶层 size
  > 历史模型后缀
  > 后端默认值
```

### 1.1 图片专用稳定性策略

图片请求不再复用普通 Agent 的通用传输策略：

- 上游包装使用 `requestType=image_gen`，不发送 `enabledCreditTypes`。
- 每个账号按 `fetchAvailableModels` 结果在同档位内解析动态图片模型，例如
  `gemini-3.1-flash-image` 可回落到该账号实际暴露的 `gemini-3-flash-image`；
  Pro 图片模型不会静默降级为 Flash。若携带 project 的模型查询返回 `403`，会去掉
  project 头和请求体后再查询一次，以兼容账号间的 project 校验差异。
- 账号默认按近期图片健康度排序；健康度相同时使用 `ULTRA/UTRL -> PRO -> FREE`。
  最近发生容量失败的账号会在当前进程内临时降级 60 秒，成功后恢复优先级。
- 每个账号的 `generateContent` 按 `sandbox -> daily -> prod` 尝试；仅
  `404`、`408`、`5xx` 或连接异常切换 host，账号级 `429` 不切换 host。
- 每张图片最多尝试 3 个不同账号，避免账号数和 host 数相乘造成无界重试。
- `503 MODEL_CAPACITY_EXHAUSTED` 作为模型/节点容量问题处理：允许在预算内换账号，
  但不写入账号额度冷却，也不触发“所有账号不可用”告警。
- 图片请求固定使用非流式、带 `Content-Length` 的 `generateContent`；即使启用了
  stream-to-non-stream，图片也不会改走流式收集路径。

运行时健康排序是进程内状态；多 worker 部署时，各 worker 会独立学习最近的图片
成功和容量失败。动态模型列表按账号和 project 缓存 5 分钟。

账号优先级使用数据库中已持久化的 `tier`，不会根据凭证文件名猜测套餐。新导入的原始
JSON 若没有 tier，需在面板执行一次 Antigravity 凭证检验，让 `loadCodeAssist` 的
`ULTRA/PRO/FREE` 结果写入账号状态；未检验账号会按存储层默认的 `pro` 处理。

## 2. 接口地址与认证

Gemini 原生格式：

```text
POST {BASE_URL}/models/gemini-3.1-flash-image:generateContent
POST {BASE_URL}/models/gemini-3.1-flash-image:streamGenerateContent
```

项目当前部署前缀可使用 `/antigravity/v1` 或 `/antigravity/v1beta`。认证头：

```http
x-goog-api-key: YOUR_API_KEY
Content-Type: application/json
```

OpenAI Chat Completions 格式：

```text
POST {HOST}/antigravity/v1/chat/completions
```

认证头：

```http
Authorization: Bearer YOUR_API_KEY
Content-Type: application/json
```

不要把密钥写进源码或提交到 Git。当前示例环境使用 HTTP 明文地址时，密钥和请求内容没有 TLS 保护；正式外部接入应在前面提供 HTTPS。

## 3. 完整上游调用链

### 3.1 Gemini 原生格式

```text
客户端
  -> POST /antigravity/v1{beta}/models/{model}:generateContent
  -> src/router/antigravity/gemini.py
     - GeminiRequest 校验
     - role 缺省时补为 user
     - camelCase / snake_case 图片字段解析
  -> model_to_dict
  -> normalize_gemini_request(mode="antigravity")
  -> prepare_image_generation_request
     - 根据固定优先级选择图片参数
     - 转换为 Antigravity 上游使用的 generationConfig.imageConfig
     - 模型归一化为 gemini-3.1-flash-image
     - 保留 responseModalities、seed、systemInstruction、tools 等字段
  -> src/api/antigravity.non_stream_request 或 stream_request
  -> wrap_cli_request
     - 注入 project、requestId、sessionId、labels、toolConfig
  -> Antigravity 上游 /v1internal:generateContent
     或 /v1internal:streamGenerateContent?alt=sse
  -> gcli2api 解包 response
  -> 返回 Gemini candidates / inlineData
```

### 3.2 OpenAI Chat Completions 格式

```text
客户端
  -> POST /antigravity/v1/chat/completions
  -> src/router/antigravity/openai.py
  -> convert_openai_to_gemini_request
     - messages 转 contents
     - image_url 转 inlineData
     - 顶层 size 保留给图片归一化阶段
  -> normalize_gemini_request(mode="antigravity")
  -> prepare_image_generation_request
     - size 转 aspectRatio + imageSize
  -> 与 Gemini 原生格式共用 Antigravity 上游调用链
  -> Gemini 响应转 OpenAI Chat Completions 响应
```

## 4. Google 官方格式

### 4.1 REST 推荐请求

```bash
curl -sS \
  -X POST \
  "${BASE_URL}/models/gemini-3.1-flash-image:generateContent" \
  -H "x-goog-api-key: ${API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{
    "contents": [{
      "parts": [{"text": "生成一张夜晚上海街头的电影感照片"}]
    }],
    "generationConfig": {
      "responseModalities": ["IMAGE"],
      "responseFormat": {
        "image": {
          "aspectRatio": "16:9",
          "imageSize": "2K"
        }
      }
    }
  }'
```

`contents[].role` 可以省略，代理会按 `user` 处理。

### 4.2 Google SDK snake_case 格式

```json
{
  "contents": [
    {
      "role": "user",
      "parts": [{"text": "生成一张产品海报"}]
    }
  ],
  "generationConfig": {
    "response_modalities": ["IMAGE"],
    "image_config": {
      "aspect_ratio": "4:5",
      "image_size": "1K"
    }
  }
}
```

兼容的 legacy camelCase 形式：

```json
{
  "generationConfig": {
    "imageConfig": {
      "aspectRatio": "4:5",
      "imageSize": "1K"
    }
  }
}
```

### 4.3 `responseModalities`

| 值 | 代理行为 | 备注 |
|---|---|---|
| `["IMAGE"]` | 原样透传 | 推荐用于纯图片输出 |
| `["TEXT", "IMAGE"]` | 原样透传 | 允许模型同时返回文字与图片 |
| 省略 | 不主动补值 | 由上游模型决定 |

### 4.4 Google `generateContent` 完整参数字典

状态说明：

- **生产验证**：已通过当前生产环境真实请求确认。
- **透传**：代理会发送给 Antigravity，但没有验证参数对图片内容的具体影响。
- **接收但忽略**：请求校验可以通过，但当前路由不会使用该字段，不能期待它产生效果。
- **忽略**：请求可以被解析，但不会传给上游或不会产生对应行为。
- **不支持**：不要发送，可能得到 400/422 或与预期不一致。

#### 顶层参数

| JSON 路径 | 类型 | 必填 | 可填写的值 | 代理行为与生产状态 |
|---|---|---:|---|---|
| `contents` | array | 是 | 至少一个 Content | 图片提示词和输入图片的容器 |
| `contents[].role` | string | 否 | 推荐 `user`；历史内容可用 `model` | 省略时自动补 `user`，生产验证 |
| `contents[].parts` | array | 是 | 一个或多个 Part | 按顺序传给模型 |
| `contents[].parts[].text` | string | 否 | 任意非空提示词 | 文生图提示词，生产验证 |
| `contents[].parts[].inlineData` | object | 否 | `{mimeType, data}` | Base64 图片输入，用于图片编辑，生产验证 |
| `contents[].parts[].inline_data` | object | 否 | `{mime_type, data}` | snake_case 图片输入兼容，生产验证 |
| `contents[].parts[].fileData` | object | 否 | Gemini 文件引用结构 | 透传，图片模型未做生产验证 |
| `systemInstruction` | object | 否 | `{parts: [{text: "..."}]}` | 系统级图片风格或约束，生产验证 |
| `generationConfig` | object | 否 | 见下表 | 图片输出控制 |
| `tools` | array | 否 | 推荐 `[ {"googleSearch": {}} ]` | Google Search 图片生成已生产验证 |
| `toolConfig` | object | 否 | Gemini toolConfig | 透传；代理缺省时会注入函数调用默认配置 |
| `safetySettings` | array | 否 | Gemini SafetySetting | 当前 Antigravity 包装层会移除，不能依赖 |
| `cachedContent` | string | 否 | Gemini cached content 名称 | 透传，图片模型未验证 |
| `size` | string | 否 | 如 `1024x1536` | 非 Google 官方字段；仅兼容旧客户端，推荐改用 `responseFormat.image` |

`inlineData` 示例：

```json
{
  "inlineData": {
    "mimeType": "image/jpeg",
    "data": "BASE64_IMAGE_DATA"
  }
}
```

#### `generationConfig` 参数

| JSON 路径 | 类型 | 必填 | 支持值/范围 | 代理行为与生产状态 |
|---|---|---:|---|---|
| `responseModalities` | string[] | 否 | `["IMAGE"]`、`["TEXT","IMAGE"]` | 两种均生产验证 |
| `response_modalities` | string[] | 否 | 同上 | SDK snake_case 兼容，转为 camelCase |
| `responseFormat.image.aspectRatio` | string | 否 | `1:1`、`1:4`、`1:8`、`2:3`、`3:2`、`3:4`、`4:1`、`4:3`、`4:5`、`5:4`、`8:1`、`9:16`、`16:9`、`21:9` | 14 种全部生产验证；非法值返回 400 |
| `responseFormat.image.imageSize` | string | 否 | `512`、`1K`、`2K`、`4K` | 全部支持；值不区分大小写是代理扩展，建议使用官方大小写 |
| `imageConfig.aspectRatio` | string | 否 | 与 `responseFormat` 相同 | legacy camelCase 兼容 |
| `imageConfig.imageSize` | string | 否 | `512`、`1K`、`2K`、`4K` | legacy camelCase 兼容 |
| `image_config.aspect_ratio` | string | 否 | 与 `responseFormat` 相同 | SDK snake_case 兼容 |
| `image_config.image_size` | string | 否 | `512`、`1K`、`2K`、`4K` | SDK snake_case 兼容 |
| `candidateCount` | integer | 否 | 代理接收 `1–8`，随后强制改为 `1` | Antigravity 图片模型单次只支持一个候选；超过 `8` 会在请求校验阶段返回 422，生产验证 |
| `seed` | integer | 否 | 整数 | 透传；不保证相同 seed 产生逐像素相同图片 |
| `temperature` | number | 否 | `0.0–2.0` | 透传；对图片效果未做确定性验证 |
| `topP` | number | 否 | `0.0–1.0` | 透传；图片效果未验证 |
| `topK` | integer | 否 | `>=1` | 透传；图片效果未验证 |
| `maxOutputTokens` | integer | 否 | `>=1` | 透传，主要影响文本输出，不控制图片像素 |
| `stopSequences` | string[] | 否 | 字符串数组 | 透传，只对文本部分有意义 |
| `frequencyPenalty` | number | 否 | `-2.0–2.0` | 透传，图片效果未验证 |
| `presencePenalty` | number | 否 | `-2.0–2.0` | 透传，图片效果未验证 |
| `thinkingConfig` | object | 否 | Gemini thinking 配置 | 透传；图片模型思考过程由上游管理，不建议主动设置 |
| `responseMimeType` | string | 否 | Gemini MIME 配置 | 不用于控制生成图片格式，不要用它请求 PNG/WebP |
| `responseSchema` | object | 否 | JSON Schema | 只适合结构化文本，不适合图片输出 |

#### 图片参数优先级

当多个来源同时出现时，只采用最高优先级：

```text
generationConfig.responseFormat.image
  > generationConfig.imageConfig / image_config
  > 顶层 size
  > 历史模型名后缀
  > 上游默认值
```

例如下面请求最终使用 `9:16/2K`，而不是顶层 `size`：

```json
{
  "size": "1024x1024",
  "generationConfig": {
    "responseFormat": {
      "image": {
        "aspectRatio": "9:16",
        "imageSize": "2K"
      }
    }
  }
}
```

## 5. OpenAI 兼容格式

### 5.1 OpenAI Images API

> 2026-07-29 已在生产环境验证此入口：HTTP 200，响应包含 `created`、`data`、`usage`，`data[].b64_json` 可解码为实际图片。

标准图片生成入口：

```text
POST {HOST}/antigravity/v1/images/generations
```

```bash
curl -sS \
  -X POST \
  "${HOST}/antigravity/v1/images/generations" \
  -H "Authorization: Bearer ${API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-3.1-flash-image",
    "prompt": "生成一张竖版旅行海报",
    "size": "1024x1536",
    "n": 1,
    "response_format": "b64_json"
  }'
```

响应（OpenAI 官方 ImagesResponse 形状）：

```json
{
  "created": 1785312000,
  "data": [
    {"b64_json": "BASE64_IMAGE_DATA"}
  ],
  "size": "1024x1536",
  "quality": "auto",
  "output_format": "jpeg",
  "background": "auto",
  "usage": {"input_tokens": 100, "output_tokens": 1134, "total_tokens": 1234}
}
```

当前实现边界：

- `response_format` 只支持 `b64_json`，不返回临时图片 URL；`url` 返回 HTTP 400（OpenAI 错误形状）。
- `n` 支持 `1` 到 `4`。由于 Antigravity 单次只允许一个候选，`n>1` 会顺序执行多次上游图片生成请求。
- `size="auto"` 使用后端默认比例和分辨率；其他尺寸按下表映射。
- 协议层 `model` 原样接受（如 `gpt-image-1`、`dall-e-3`），不影响后端模型选择；上游统一使用默认图片模型。
- 上游返回 JPEG Base64。`output_format` 仅支持 `jpeg` / `jpg`；`png`、`webp`、`output_compression` 属于上游不具备的能力，返回 HTTP 400（OpenAI 错误形状）。
- `quality`、`background`、`moderation`、`style`、`user` 按协议接受并校验枚举值，上游无对应能力，作为 no-op 处理；`quality`/`background`/`output_format`/`size` 在响应中原样回显。
- `stream=true` 时按 OpenAI 流式协议返回 SSE，每张图片一个 `image_generation.completed` 事件，`usage` 附在末帧；`partial_images` 接受 0–3 但不产生 partial 事件。
- 参数错误统一返回 OpenAI 错误形状 `{"error": {"message", "type", "param", "code"}}`。

### 5.2 OpenAI Chat Completions 图片格式

原有 Chat Completions 图片调用继续兼容：

```bash
curl -sS \
  -X POST \
  "${HOST}/antigravity/v1/chat/completions" \
  -H "Authorization: Bearer ${API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-3.1-flash-image",
    "messages": [
      {"role": "user", "content": "生成一张竖版旅行海报"}
    ],
    "size": "1024x1536",
    "stream": false
  }'
```

两个 OpenAI 入口中的 `size` 都支持 `宽x高`、`宽X高`、`宽*高`、`宽×高`。它会被映射到最接近的官方宽高比和分辨率档位，不承诺最终图片像素与输入数字完全相同。

常见映射：

| OpenAI `size` | 转换后的 `aspectRatio` | 转换后的 `imageSize` |
|---|---:|---:|
| `512x512` | `1:1` | `512` |
| `1024x1024` | `1:1` | `1K` |
| `1024x1536` | `2:3` | `2K` |
| `1536x1024` | `3:2` | `2K` |
| `4096x1024` | `4:1` | `4K` |

分辨率档位按输入最大边映射：最大边 `<=512` 为 `512`，`<=1280` 为 `1K`，`<=2560` 为 `2K`，更大为 `4K`。

### 5.3 OpenAI Images API 完整参数字典

接口：

```text
POST /antigravity/v1/images/generations
```

| 参数 | 类型 | 必填 | OpenAI 官方值/含义 | 当前代理允许值与行为 |
|---|---|---:|---|---|
| `model` | string | 是 | 官方接口可按模型选择 GPT Image 或 DALL-E | 按协议原样接受任意模型名（如 `gpt-image-1`、`dall-e-3`、`gemini-3.1-flash-image`）；不影响后端模型选择，上游统一使用默认图片模型 |
| `prompt` | string | 是 | 文生图提示词；官方长度上限随模型变化 | 当前只在本地校验非空，最终长度限制由 Antigravity 上游决定 |
| `n` | integer | 否 | 官方通常为 `1–10`，部分模型只允许 `1` | 当前为 `1–4`，默认 `1`；每张图片执行一次独立上游请求，生产已验证 `n=2` |
| `size` | string | 否 | 官方值随模型变化，常见为 `auto`、`1024x1024`、`1024x1536`、`1536x1024` | 当前接受 `auto` 或可解析的 `宽x高`；映射到最接近的 Gemini 比例和档位，不执行精确 resize；无法解析的字符串当前会退回上游默认值，因此调用方必须自行校验格式 |
| `response_format` | string | 否 | DALL-E 接口可用 `url` 或 `b64_json`；GPT Image 默认直接返回 Base64 | 当前仅 `b64_json`，默认即为该值；`url` 返回 HTTP 400（OpenAI 错误形状） |
| `output_format` | string | 否 | GPT Image 支持 `png`、`jpeg`、`webp` | 仅 `jpeg` / `jpg`（上游即 JPEG）；`png`、`webp` 上游不具备该能力，返回 HTTP 400 |
| `output_compression` | integer | 否 | JPEG/WebP 压缩比例 `0–100` | 不支持；传入返回 HTTP 400 |
| `quality` | string | 否 | `auto`、`low`、`medium`、`high`（DALL-E 3 为 `standard`、`hd`） | 接受全部官方枚举值；上游无对应能力，作为 no-op 并在响应中原样回显 |
| `background` | string | 否 | `auto`、`transparent`、`opaque` | 接受全部官方枚举值；上游无对应能力，作为 no-op 并在响应中原样回显 |
| `moderation` | string | 否 | `auto`、`low` | 接受官方枚举值；作为 no-op |
| `style` | string | 否 | DALL-E 3 的 `vivid` / `natural` | 接受官方枚举值；作为 no-op，建议把风格要求写入 `prompt` |
| `stream` | boolean | 否 | GPT Image 可流式返回生成事件 | 支持；返回 SSE，每张图片一个 `image_generation.completed` 事件，`usage` 附在末帧 |
| `partial_images` | integer | 否 | 流式过程中返回的局部图片数量 `0–3` | 接受并校验范围；当前不产生 partial 事件 |
| `user` | string | 否 | 官方用于标识终端用户，当前官方接口已标记为弃用并建议使用新的安全标识字段 | 当前接受任意字符串，但不会传给 Antigravity，也不影响生成结果 |

参数校验失败（枚举值非法、`response_format="url"`、数值越界等）统一返回 HTTP 400，错误体为 OpenAI 官方形状：

```json
{"error": {"message": "...", "type": "invalid_request_error", "param": null, "code": null}}
```

注意：`action`、`input_fidelity` 等字段属于 OpenAI Responses 图片工具，不是本代理已经实现的能力。即使把这些字段放进当前请求，它们也只会作为未知扩展字段被接收并忽略。图片编辑可以使用下文 `POST /antigravity/v1/images/edits` 入口，或本文第 9 节的 Gemini / OpenAI Chat 多模态输入方式。

#### /images/edits 图片编辑入口

标准 OpenAI 图片编辑入口（multipart/form-data）：

```text
POST {HOST}/antigravity/v1/images/edits
```

```bash
curl -sS \
  -X POST \
  "${HOST}/antigravity/v1/images/edits" \
  -H "Authorization: Bearer ${API_KEY}" \
  -F "model=gemini-3.1-flash-image" \
  -F "prompt=把背景改成海边，保留主体" \
  -F "image=@input.png" \
  -F "size=1024x1024" \
  -F "n=1"
```

| 参数 | 类型 | 必填 | 当前代理允许值与行为 |
|---|---|---:|---|
| `model` | string | 是 | 同 `/images/generations`，按协议原样接受任意模型名 |
| `prompt` | string | 是 | 编辑指令，非空 |
| `image` / `image[]` | file 或 string | 是 | 至少一张；支持 multipart 文件上传，也支持 data URL 或裸 Base64 字符串；多张图可重复该字段 |
| `n` | integer | 否 | `1–4`，默认 `1`；每张图片执行一次独立上游请求 |
| `size` | string | 否 | `auto` 或 `宽x高`，映射规则与 `/images/generations` 相同 |
| `response_format` | string | 否 | 仅 `b64_json`，`url` 返回 HTTP 400 |
| `output_format` / `output_compression` | string / integer | 否 | 与 `/images/generations` 相同，仅 `jpeg`；`png`/`webp`/`output_compression` 返回 HTTP 400 |
| `quality` / `background` / `moderation` / `style` | string | 否 | 接受官方枚举值，作为 no-op |
| `stream` / `partial_images` | boolean / integer | 否 | 与 `/images/generations` 相同，SSE `image_generation.completed` 事件 |
| `mask` | file | 否 | 不支持（上游无局部重绘能力）；传入会明确返回 HTTP 400 |

响应结构与 `/images/generations` 相同（官方 ImagesResponse 形状）。编辑语义由 Antigravity 上游图片模型完成，本代理只做协议转换，不执行本地像素处理。

OpenAI 官方 Images API 的 `quality`、`background`、`moderation`、`style` 等参数在本代理仅做协议层接受，不改变上游生成行为。官方参数存在不代表本代理已经实现对应效果。

OpenAI 官方参考：

- <https://developers.openai.com/api/reference/resources/images/methods/generate>
- <https://platform.openai.com/docs/guides/image-generation>

### 5.4 OpenAI `size` 映射规则

| 调用参数 | Gemini 内部参数 | 生产或预期输出 |
|---|---|---|
| `auto` | 不指定比例和档位 | 由上游决定，不保证固定尺寸 |
| `512x512` | `1:1/512` | 生产实测 512×512 |
| `1024x1024` | `1:1/1K` | 生产实测 1024×1024 |
| `1024x1536` | `2:3/2K` | 生产实测 1696×2528 |
| `1536x1024` | `3:2/2K` | 映射为横向 2K 档 |
| `2048x2048` | `1:1/2K` | 生产实测 2048×2048 |
| `4096x4096` | `1:1/4K` | 生产实测 4096×4096 |

`size` 是“比例 + 分辨率档位”映射，不是图片生成完成后的强制 resize。任意宽高都会先寻找最接近的 14 种 Gemini 官方比例。

### 5.5 OpenAI Chat Completions 图片参数

接口：

```text
POST /antigravity/v1/chat/completions
```

| 参数 | 类型 | 必填 | 可填写内容 | 图片模型行为 |
|---|---|---:|---|---|
| `model` | string | 是 | `gemini-3.1-flash-image` | 不需要尺寸后缀 |
| `messages` | array | 是 | OpenAI Chat 消息数组 | 文本和图片输入容器 |
| `messages[].role` | string | 是 | `system`、`user`、`assistant` | system 会转换为 Gemini systemInstruction |
| `messages[].content` | string/array | 是 | 文本，或多模态 content 数组 | 支持文生图和图片编辑 |
| `content[].type="text"` | object | 否 | `{type:"text", text:"..."}` | 图片提示词 |
| `content[].type="image_url"` | object | 否 | data URL：`data:image/jpeg;base64,...` | 图片输入，生产验证 |
| `size` | string | 否 | `auto` 或 `宽x高` | 使用与 Images API 相同的映射规则 |
| `stream` | boolean | 否 | `false` / `true` | 支持 Chat SSE；第三方只需要完整图片时建议 `false` |
| `n` | integer | 否 | 最终固定为 `1` | Chat 入口不通过单请求生成多张图；多图请用 Images API |
| `temperature` | number | 否 | `0.0–2.0` | 转换并透传，图片效果未验证 |
| `top_p` | number | 否 | `0.0–1.0` | 转换并透传，图片效果未验证 |
| `max_tokens` | integer | 否 | `>=1` | 主要影响文本部分，不控制图片尺寸 |
| `response_format` | object | 否 | OpenAI 结构化文本格式 | 不是图片文件格式，不要用它请求 PNG/JPEG |

OpenAI Chat 图片响应通常位于：

```text
choices[0].message.content
```

内容中使用 Markdown data URL：

```text
![gemini-generated-content](data:image/jpeg;base64,...)
```

如果调用方希望直接读取 `data[].b64_json`，应使用 `/images/generations`。

### 5.6 OpenAI Python SDK 示例

```python
import base64
from openai import OpenAI

client = OpenAI(
    api_key="YOUR_API_KEY",
    base_url="http://47.88.76.213:18317/antigravity/v1",
)

result = client.images.generate(
    model="gemini-3.1-flash-image",
    prompt="生成一张竖版旅行海报",
    size="1024x1536",
    n=1,
    output_format="jpeg",
)

image_bytes = base64.b64decode(result.data[0].b64_json)
with open("generated.jpg", "wb") as file:
    file.write(image_bytes)
```

如果 SDK 版本不允许自定义模型名或参数，直接使用前面的 HTTP/curl 方式。

### 5.7 常见 HTTP 状态码

| HTTP 状态 | 常见原因 | 调用方处理 |
|---:|---|---|
| `200` | 图片生成成功 | 解码 Base64 并按 JPEG 保存 |
| `400` | 比例、尺寸或输出参数不支持 | 检查本文参数枚举，不要原样重试 |
| `401` / `403` | API Key 缺失、错误或无权限 | 更换有效密钥 |
| `413` | 非流式响应超过缓冲上限 | 当前 4K 已验证通过；若再次出现应联系服务端检查配置 |
| `422` | JSON 结构或字段类型错误 | 检查必填字段和 JSON 类型 |
| `429` | 上游额度或频率限制 | 延迟后重试，不要立即并发重试 |
| `500` | 当前没有可用上游凭证等服务端问题 | 记录错误体并联系服务端 |
| `503` + `MODEL_CAPACITY_EXHAUSTED` | 图片模型或节点当前没有容量 | 不封禁账号；降低并发并延迟重试 |
| 其他 `503` | 上游暂时不可用 | 按有限账号/host 预算重试 |

## 6. 官方宽高比与输出像素

以下是 Google 官方文档在 2026-07-29 列出的尺寸。官方说明 `imageSize` 的值区分大小写。

| 宽高比 | `512` | `1K` | `2K` | `4K` |
|---|---:|---:|---:|---:|
| `1:1` | 512×512 | 1024×1024 | 2048×2048 | 4096×4096 |
| `1:4` | 256×1024 | 512×2048 | 1024×4096 | 2048×8192 |
| `1:8` | 192×1536 | 384×3072 | 768×6144 | 1536×12288 |
| `2:3` | 424×632 | 848×1264 | 1696×2528 | 3392×5056 |
| `3:2` | 632×424 | 1264×848 | 2528×1696 | 5056×3392 |
| `3:4` | 448×600 | 896×1200 | 1792×2400 | 3584×4800 |
| `4:1` | 1024×256 | 2048×512 | 4096×1024 | 8192×2048 |
| `4:3` | 600×448 | 1200×896 | 2400×1792 | 4800×3584 |
| `4:5` | 464×576 | 928×1152 | 1856×2304 | 3712×4608 |
| `5:4` | 576×464 | 1152×928 | 2304×1856 | 4608×3712 |
| `8:1` | 1536×192 | 3072×384 | 6144×768 | 12288×1536 |
| `9:16` | 384×688 | 768×1376 | 1536×2752 | 3072×5504 |
| `16:9` | 688×384 | 1376×768 | 2752×1536 | 5504×3072 |
| `21:9` | 792×168 | 1584×672 | 3168×1344 | 6336×2688 |

官方参考：<https://ai.google.dev/gemini-api/docs/generate-content/image-generation>

注意：表格是官方目标像素，不是代理自行缩放后的固定保证。上游模型版本、裁切和服务端策略可能让实际结果出现差异，调用方应读取返回图片文件本身的宽高。

## 7. 已验证参数矩阵

### 7.1 当前源码自动化验证

`tests/test_antigravity_image_protocol.py` 覆盖：

- 14 种官方 `aspectRatio` × 4 种官方 `imageSize`，共 56 组参数保持不变并正确转换。
- Gemini REST `responseFormat.image`。
- Gemini `imageConfig` camelCase。
- Google SDK `image_config` snake_case。
- OpenAI `size` 映射。
- 缺省 `contents[].role` 自动补 `user`。
- 官方参数高于 legacy、OpenAI `size` 和模型后缀。
- `responseModalities`、`seed`、`systemInstruction`、`tools` 保留。
- Gemini 和 OpenAI 两条完整转换链均进入相同的 Antigravity `imageConfig`。

运行：

```bash
UV_CACHE_DIR=/tmp/gcli2api-uv-cache PYTHONPATH=. \
  uv run --with pytest --with pytest-asyncio \
  pytest tests/test_antigravity_image_protocol.py -q
```

### 7.2 2026-07-29 修改前生产基线

以下是修改部署前，对现有生产实例的真实请求结果，用于说明原问题，不代表修改后的最终验收：

| 请求 | HTTP | 实际图片 | 结论 |
|---|---:|---:|---|
| 基础模型 + `responseFormat` `16:9/1K` | 200 | JPEG 1408×768 | 参数被旧逻辑丢弃，尺寸是后端默认 |
| 基础模型 + `responseFormat` `9:16/512` | 200 | JPEG 1408×768 | 参数被旧逻辑丢弃 |
| 基础模型 + camelCase `imageConfig` `9:16/1K` | 200 | JPEG 1408×768 | 参数被旧逻辑覆盖 |
| 基础模型 + snake_case `image_config` `9:16/1K` | 200 | JPEG 1408×768 | 参数被旧逻辑覆盖 |
| Gemini 顶层 `size=512x512` | 200 | JPEG 1024×1024 | 旧代码没有 `512` 档，错误映射为 `1K` |
| OpenAI Chat `size=1024x1536` | 200 | JPEG 1696×2528 | 已能映射到 `2:3/2K` |
| OpenAI Chat 图片输入编辑 | 200 | JPEG 1024×1024 | 链路可用；语义编辑准确性未做视觉判定 |
| Gemini `inline_data` 图片输入编辑 | 200 | JPEG 1408×768 | 链路可用；输出尺寸仍被旧逻辑忽略 |

### 7.3 2026-07-29 首次兼容版本生产实测

部署兼容版本后，使用基础模型名和官方 `responseFormat.image` 对 14 种比例逐一请求 `512` 档，实际均返回 HTTP 200 和 JPEG 图片：

| `aspectRatio` | 生产实际像素 | Google 公布目标像素 | 是否完全一致 |
|---|---:|---:|---|
| `1:1` | 512×512 | 512×512 | 是 |
| `1:4` | 256×1024 | 256×1024 | 是 |
| `1:8` | 176×1456 | 192×1536 | 否 |
| `2:3` | 416×624 | 424×632 | 否 |
| `3:2` | 624×416 | 632×424 | 否 |
| `3:4` | 448×592 | 448×600 | 否 |
| `4:1` | 1024×256 | 1024×256 | 是 |
| `4:3` | 592×448 | 600×448 | 否 |
| `4:5` | 464×576 | 464×576 | 是 |
| `5:4` | 576×464 | 576×464 | 是 |
| `8:1` | 1456×176 | 1536×192 | 否 |
| `9:16` | 384×688 | 384×688 | 是 |
| `16:9` | 688×384 | 688×384 | 是 |
| `21:9` | 784×336 | 792×168 | 否 |

这说明宽高比参数全部生效，但 Antigravity 上游的像素取整和部分极端比例与 Google 公网页面并不完全相同。接入方必须读取响应图片的真实像素，不能把官方表格当成强保证。

其他生产结果：

| 参数或能力 | HTTP | 实际结果 |
|---|---:|---|
| `1:1/1K` | 200 | JPEG 1024×1024 |
| `1:1/2K` | 200 | JPEG 2048×2048 |
| `1:1/4K` | 200 | JPEG 4096×4096，约 7.1 MB；64 MiB 图片缓冲修复已生产验证 |
| `imageSize="1k"` | 200 | 代理宽松归一化为 `1K`，JPEG 1024×1024 |
| `imageSize="3K"` | 400 | 上游 `INVALID_ARGUMENT` |
| `aspectRatio="7:5"` | 400 | 上游 `INVALID_ARGUMENT` |
| `["TEXT", "IMAGE"]` | 200 | 同时返回 1 段文本和 1 张图片 |
| Gemini `candidateCount=2` | 200 | 代理归一化为单候选，返回一张 JPEG 512×512 |
| `systemInstruction` | 200 | 返回图片 |
| `tools=[{"googleSearch":{}}]` | 200 | 返回图片 |
| OpenAI `size=1024x1536` | 200 | JPEG 1696×2528 |
| OpenAI Images API `n=2` | 200 | 返回两张独立 JPEG 512×512 |
| OpenAI 图片输入编辑 | 200 | JPEG 1024×1024；语义准确性仍需人工验收 |

上游错误消息还显示可识别 `512P`、`512PX`，但它们不是本文推荐的 Google 官方公开值。第三方接入请使用 `512`。

### 7.4 最终镜像与生产验收

2026-07-29 已将包含 4K 缓冲修复和 OpenAI Images API 的镜像推送为：

```text
harbor.beeintel.com/crawler-platform/gcli2api:v20260729
harbor.beeintel.com/crawler-platform/gcli2api:latest
sha256:2284df759026df219a6bc3c81a8e13a59c51860f318adfc416f21000edea1de3
```

生产容器更新到该镜像后，最终复测得到：

| 请求 | 结果 |
|---|---|
| `POST /antigravity/v1/images/generations`，`n=1` | HTTP 200，JPEG 512×512，标准 `data[].b64_json` 响应 |
| `POST /antigravity/v1/images/generations`，`n=2` | HTTP 200，返回两张 JPEG 512×512 |
| Google 官方 `1:1/4K` | HTTP 200，JPEG 4096×4096 |
| Google 官方 `candidateCount=2` | HTTP 200，代理归一化为一个候选 |

以上结果证明最终生产容器已经加载 4K 缓冲修复、Google 官方参数兼容和 OpenAI Images API 路由。

## 8. 支持与不支持

| 能力 | 状态 | 说明 |
|---|---|---|
| Gemini `generateContent` | 支持 | 主要接入方式 |
| Gemini `streamGenerateContent` | 代码链支持 | 使用相同图片参数归一化；生产图片流需部署后复测 |
| `responseFormat.image.aspectRatio` | 支持 | 14 种官方比例 |
| `responseFormat.image.imageSize` | 支持 | `512`、`1K`、`2K`、`4K` |
| `imageConfig` / `image_config` | 支持 | camelCase 和 snake_case 均可 |
| OpenAI Chat 顶层 `size` | 支持 | 转成最接近的比例和档位 |
| 文生图 | 支持 | 已验证返回图片 |
| 图片输入 / 编辑 | 支持链路 | 已验证 HTTP 200，语义质量需人工验收 |
| Gemini `candidateCount` | 固定为 1 | Antigravity 图片模型拒绝单次多候选 |
| OpenAI Images API `n` | 支持 1–4 | 拆成多次单候选上游请求 |
| `systemInstruction` | 支持 | 已生产验证 |
| Google Search tool | 支持 | `tools=[{"googleSearch":{}}]` 已生产验证 |
| 历史模型后缀 | 兼容 | 仅兜底，不推荐新接入 |
| `/images/generations` | 支持 | 官方 ImagesResponse 形状，返回 `data[].b64_json` |
| `/images/edits` | 支持 | multipart 图片编辑；不支持 `mask` |
| OpenAI `stream` | 支持 | 假流式 SSE，每张图一个 `image_generation.completed` 事件 |
| OpenAI `output_format` | 仅 `jpeg` | `png`/`webp`/`output_compression` 上游不具备该能力，返回 HTTP 400 |
| OpenAI `quality`、`background`、`moderation`、`style` | 协议接受 | 枚举值校验通过，上游无对应能力，作为 no-op |
| OpenAI `response_format="url"` | 不支持 | 无法托管临时 URL，返回 HTTP 400（OpenAI 错误形状） |
| `/responses` | 不支持 | 当前没有该路由 |
| `/interactions` | 不支持 | 当前没有该路由 |
| `responseFormat.image.mimeType` | 未映射 | 当前上游只转换比例和尺寸 |
| `responseFormat.image.delivery` | 未映射 | 当前上游只转换比例和尺寸 |
| OpenAI `size` 精确像素输出 | 不保证 | 它是比例与分辨率档位映射，不是后处理 resize |

## 9. 图片输入示例

Gemini 原生格式使用 `inlineData`：

```json
{
  "contents": [{
    "role": "user",
    "parts": [
      {"text": "把背景改成海边，保留主体"},
      {
        "inlineData": {
          "mimeType": "image/png",
          "data": "BASE64_IMAGE_DATA"
        }
      }
    ]
  }],
  "generationConfig": {
    "responseFormat": {
      "image": {
        "aspectRatio": "1:1",
        "imageSize": "1K"
      }
    }
  }
}
```

OpenAI Chat 格式使用 data URL：

```json
{
  "model": "gemini-3.1-flash-image",
  "messages": [{
    "role": "user",
    "content": [
      {"type": "text", "text": "把背景改成海边，保留主体"},
      {
        "type": "image_url",
        "image_url": {
          "url": "data:image/png;base64,BASE64_IMAGE_DATA"
        }
      }
    ]
  }],
  "size": "1024x1024"
}
```

### 9.1 Gemini SDK 图片编辑

Gemini SDK 没有独立的 `images.edit()` 方法。图片编辑使用与图片生成相同的
`models.generate_content()`：将编辑指令和输入图片共同放入 `contents`，并明确
请求 `IMAGE` 输出。

仓库提供可直接运行的示例：

```bash
export GEMINI_IMAGE_API_BASE=http://127.0.0.1:7861/antigravity/v1
export GEMINI_IMAGE_API_KEY=your_api_key

uv run --with google-genai --with pillow \
  python examples/gemini_sdk_image_edit.py \
  input.png "保留主体，只把背景改成海边" \
  --output edited_image.png \
  --aspect-ratio 1:1 \
  --image-size 1K
```

示例会把带 `/v1` 或 `/v1beta` 的 API 地址拆成 SDK 所需的 `base_url` 与
`api_version`，发送以下等价调用：

```python
response = client.models.generate_content(
    model="gemini-3.1-flash-image",
    contents=[prompt, Image.open(input_path)],
    config=types.GenerateContentConfig(
        response_modalities=["IMAGE"],
        image_config=types.ImageConfig(
            aspect_ratio="1:1",
            image_size="1K",
        ),
    ),
)
```

返回图片按 `part.inline_data.mime_type` 决定扩展名，不假定服务一定返回 PNG。

## 10. 响应读取

Gemini 响应图片：

```json
{
  "candidates": [{
    "content": {
      "parts": [{
        "inlineData": {
          "mimeType": "image/jpeg",
          "data": "BASE64_IMAGE_DATA"
        }
      }]
    }
  }]
}
```

调用方必须：

1. 遍历所有 `candidates[].content.parts[]`。
2. 同时兼容 `inlineData` 和 `inline_data`。
3. 按返回的 `mimeType` 决定文件扩展名，不要假定一定是 PNG。
4. Base64 解码后读取图片真实宽高，不要只根据请求参数推断。

仓库示例：

- 文生图/原始 HTTP：`examples/gemini_image_demo.py`
- Gemini SDK 图片编辑：`examples/gemini_sdk_image_edit.py`

## 11. 部署后验收清单

- 使用基础模型名，不带任何尺寸后缀。
- 官方 `responseFormat.image` 的横图、竖图、方图均产生对应方向的图片。
- `512` 不再被提升为 `1K`。
- OpenAI `size=1024x1536` 仍得到 `2:3/2K` 对应输出。
- `responseFormat.image` 与 `size` 同时存在时，以官方参数为准。
- 文本加图片输入仍可生成图片。
- 非流式与流式接口分别验证。
- 记录 HTTP 状态、MIME、真实像素、耗时和错误体；不要记录密钥或完整 Base64 图片。

## 12. Banana 图片压力测试脚本

仓库提供：

```text
scripts/banana_image_stress.py
```

它会按照给定的并发档位逐级加压，每个档位运行固定时间，并统计：

- 测试期间成功生成的图片总数。
- 请求 RPM：每分钟完成的 HTTP 请求数。
- 图片 RPM：每分钟成功解码的图片数；当 OpenAI `n>1` 时，它可能高于请求 RPM。
- 请求成功率、HTTP 状态码和错误分布。
- 平均、P50、P95、P99 请求延迟。
- 返回图片字节数、MIME 和实际宽高。
- 本次测试的最佳图片 RPM、最高稳定并发和建议并发。

测试结束后会生成一个自包含 HTML 文件，不依赖 CDN 或额外 Web 服务，可以直接用浏览器打开。报告不保存 API Key、生成图片或 Base64，只保存统计指标和经过截断的错误摘要。

通过本机 OpenAI Images 入口运行该脚本时，请求会经过当前服务的完整图片链路，
包括按账号动态模型解析、图片账号排序与轮换、上游 host 回退和容量错误分类；它比
直接请求单个 Google host 的指纹 A/B 更适合作为优化后的回归冒烟。

首次验证建议只跑单并发、最多 10 次：

```bash
export BANANA_API_KEY='YOUR_API_KEY'

uv run python scripts/banana_image_stress.py \
  --execute \
  --api-base 'http://127.0.0.1:7861/antigravity/v1' \
  --protocol openai \
  --model gemini-3.1-flash-image \
  --concurrency-levels 1 \
  --stage-seconds 600 \
  --max-requests-per-stage 10 \
  --minimum-success-rate 0.90 \
  --output reports/banana-image-smoke.html \
  --save-json
```

### 12.1 OpenAI 协议压测

API Key 推荐只通过环境变量提供：

```bash
export BANANA_API_KEY='YOUR_API_KEY'

uv run python scripts/banana_image_stress.py \
  --execute \
  --api-base 'http://127.0.0.1:7861/antigravity/v1' \
  --protocol openai \
  --model gemini-3.1-flash-image \
  --concurrency-levels 1,2,4,8 \
  --stage-seconds 60 \
  --size 512x512 \
  --images-per-request 1 \
  --output reports/banana-stress-openai.html \
  --save-json \
  --open-report
```

如果不使用 `uv`，也可以执行：

```bash
.venv/bin/python scripts/banana_image_stress.py ...
```

### 12.2 Google 官方协议压测

```bash
export BANANA_API_KEY='YOUR_API_KEY'

uv run python scripts/banana_image_stress.py \
  --execute \
  --api-base 'http://127.0.0.1:7861/antigravity/v1' \
  --protocol google \
  --model gemini-3.1-flash-image \
  --concurrency-levels 1,2,4,8 \
  --stage-seconds 60 \
  --aspect-ratio 1:1 \
  --image-size 512 \
  --output reports/banana-stress-google.html
```

Google 协议当前每个 HTTP 请求只生成一张图片，因此 `--images-per-request` 必须是 `1`。

### 12.3 压测参数

| 参数 | 默认值 | 作用 |
|---|---:|---|
| `--execute` | 关闭 | 必须显式提供；确认允许脚本发送真实图片请求并消耗额度 |
| `--api-base` | `http://127.0.0.1:7861/antigravity/v1` | API 基础地址；不要附加 `/images/generations` |
| `--protocol` | `openai` | `openai` 调用 `/images/generations`；`google` 调用 `:generateContent` |
| `--model` | `gemini-3.1-flash-image` | 被测试的图片模型，不需要尺寸后缀 |
| `--prompt` | 内置简单方块提示词 | 每次请求使用的提示词；应保持测试期间一致 |
| `--concurrency-levels` | `1,2,4` | 逐级测试的并发工作数，按给定顺序执行；单档最大 `256` |
| `--stage-seconds` | `60` | 每个并发档位允许启动新请求的时间 |
| `--request-timeout-seconds` | `300` | 单个图片请求超时 |
| `--size` | `512x512` | OpenAI 协议图片尺寸，按代理规则映射到 Gemini 比例和档位 |
| `--aspect-ratio` | `1:1` | Google 协议宽高比，支持本文列出的 14 种值 |
| `--image-size` | `512` | Google 协议分辨率档位：`512`、`1K`、`2K`、`4K` |
| `--images-per-request` | `1` | OpenAI `n`，范围 `1–4`；Google 只能是 `1` |
| `--target-rpm` | `0` | 客户端请求启动速率上限；`0` 表示不主动限速。它不是测试结果 RPM |
| `--max-requests-per-stage` | `0` | 每个并发档位最多请求数；`0` 表示只受阶段时间限制 |
| `--minimum-success-rate` | `0.95` | 判断档位稳定的最低成功率 |
| `--max-consecutive-failures` | `10` | 连续失败达到该数量时提前终止当前档；`0` 表示关闭熔断 |
| `--cooldown-seconds` | `5` | 稳定档位之间的等待时间 |
| `--output` | 自动时间文件名 | HTML 报告路径，必须以 `.html` 或 `.htm` 结尾 |
| `--save-json` | 关闭 | 同时保存与 HTML 同名的 JSON 指标文件 |
| `--open-report` | 关闭 | 完成后尝试在默认浏览器打开 HTML |

支持的环境变量：

| 环境变量 | 作用 |
|---|---|
| `BANANA_API_KEY` | 推荐的 API Key 来源，优先级最高 |
| `OPENAI_API_KEY` | OpenAI 协议未设置 `BANANA_API_KEY` 时的备用来源 |
| `GEMINI_IMAGE_API_KEY` | Google 协议未设置 `BANANA_API_KEY` 时的备用来源 |
| `BANANA_API_BASE` | API 基础地址 |
| `BANANA_PROTOCOL` | `openai` 或 `google` |
| `BANANA_MODEL` | 图片模型 |
| `BANANA_PROMPT` | 压测提示词 |
| `BANANA_CONCURRENCY_LEVELS` | 并发档位 |
| `BANANA_STAGE_SECONDS` | 每档时长 |
| `BANANA_REQUEST_TIMEOUT_SECONDS` | 请求超时 |
| `BANANA_SIZE` | OpenAI `size` |
| `BANANA_ASPECT_RATIO` | Google `aspectRatio` |
| `BANANA_IMAGE_SIZE` | Google `imageSize` |
| `BANANA_IMAGES_PER_REQUEST` | OpenAI `n` |
| `BANANA_TARGET_RPM` | 客户端 RPM 上限 |
| `BANANA_MAX_REQUESTS_PER_STAGE` | 每档最大请求数 |
| `BANANA_MINIMUM_SUCCESS_RATE` | 稳定成功率阈值 |
| `BANANA_MAX_CONSECUTIVE_FAILURES` | 连续失败熔断值 |
| `BANANA_COOLDOWN_SECONDS` | 档位冷却时间 |
| `BANANA_REPORT_PATH` | HTML 输出路径 |

### 12.4 如何理解报告结果

```text
请求 RPM = 阶段完成请求数 / 阶段实际耗时 × 60
图片 RPM = 成功解码图片数 / 阶段实际耗时 × 60
```

“最高稳定并发”要求：

1. 该档位至少完成一个请求。
2. 成功率达到 `--minimum-success-rate`。
3. 该档位没有出现 HTTP 429。

“建议并发”是所有稳定档位中图片 RPM 最高的档位。某个档位一旦不稳定或出现 HTTP 429，脚本会停止继续增加并发，避免在已经限流或大量失败时继续扩大生产压力。

报告中的“最佳图片 RPM”是本次客户端、网络、API Key、上游凭证池、提示词和图片尺寸共同作用下的实测值，不是服务端保证的固定配额。要评估持续生产能力，建议至少让每个档位运行 5–10 分钟，并同时观察服务端凭证冷却、CPU、内存、网络带宽和上游错误。

### 12.5 压测安全建议

- 第一次先使用 `512x512` 或 Google `512` 档，从 `1,2,4` 三档开始。
- 先使用 `--max-requests-per-stage` 控制费用，例如每档最多 `20` 个请求。
- `2K`、`4K` 返回体更大，客户端和代理都会占用更多内存，不要直接使用高并发测试。
- 使用生产 Key 之前确认额度、计费和允许的测试时间窗口。
- HTTP 明文地址会暴露 Key 和提示词，正式环境应使用 HTTPS。
- 不要把 Key 写进命令参数、报告文件、Git 或聊天记录；使用环境变量或未提交的 `.env`。
