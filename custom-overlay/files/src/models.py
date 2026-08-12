from typing import Any, Dict, List, Optional, Union

from pydantic import AliasChoices, BaseModel, ConfigDict, Field


# Pydantic v1/v2 兼容性辅助函数
def model_to_dict(model: BaseModel) -> Dict[str, Any]:
    """
    兼容 Pydantic v1 和 v2 的模型转字典方法，排除 None 值
    - v1: model.dict(exclude_none=True)
    - v2: model.model_dump(exclude_none=True)
    """
    if hasattr(model, 'model_dump'):
        # Pydantic v2
        return model.model_dump(exclude_none=True)
    else:
        # Pydantic v1
        return model.dict(exclude_none=True)


# Common Models
class Model(BaseModel):
    id: str
    object: str = "model"
    created: Optional[int] = None
    owned_by: Optional[str] = "google"


class ModelList(BaseModel):
    object: str = "list"
    data: List[Model]


# OpenAI Models
class OpenAIToolFunction(BaseModel):
    name: str
    arguments: str  # JSON string


class OpenAIToolCall(BaseModel):
    id: str
    type: str = "function"
    function: OpenAIToolFunction


class OpenAITool(BaseModel):
    type: str = "function"
    function: Dict[str, Any]


class OpenAIChatMessage(BaseModel):
    role: str
    content: Union[str, List[Dict[str, Any]], None] = None
    reasoning_content: Optional[str] = None
    name: Optional[str] = None
    tool_calls: Optional[List[OpenAIToolCall]] = None
    tool_call_id: Optional[str] = None  # for role="tool"


class OpenAIChatCompletionRequest(BaseModel):
    model: str
    messages: List[OpenAIChatMessage]
    stream: bool = False
    temperature: Optional[float] = Field(None, ge=0.0, le=2.0)
    top_p: Optional[float] = Field(None, ge=0.0, le=1.0)
    max_tokens: Optional[int] = Field(None, ge=1)
    stop: Optional[Union[str, List[str]]] = None
    frequency_penalty: Optional[float] = Field(None, ge=-2.0, le=2.0)
    presence_penalty: Optional[float] = Field(None, ge=-2.0, le=2.0)
    n: Optional[int] = Field(1, ge=1, le=128)
    seed: Optional[int] = None
    response_format: Optional[Dict[str, Any]] = None
    top_k: Optional[int] = Field(None, ge=1)
    tools: Optional[List[OpenAITool]] = None
    tool_choice: Optional[Union[str, Dict[str, Any]]] = None

    class Config:
        extra = "allow"  # Allow additional fields not explicitly defined


# 通用的聊天完成请求模型（兼容OpenAI和其他格式）
ChatCompletionRequest = OpenAIChatCompletionRequest


class OpenAIChatCompletionChoice(BaseModel):
    index: int
    message: OpenAIChatMessage
    finish_reason: Optional[str] = None
    logprobs: Optional[Dict[str, Any]] = None


class OpenAIChatCompletionResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: List[OpenAIChatCompletionChoice]
    usage: Optional[Dict[str, Any]] = None
    system_fingerprint: Optional[str] = None


class OpenAIImageGenerationRequest(BaseModel):
    """OpenAI Images API compatible generation request."""

    model_config = ConfigDict(extra="allow")

    model: str
    prompt: str = Field(..., min_length=1)
    n: int = Field(1, ge=1, le=4)
    size: str = "auto"
    quality: str = "auto"
    background: str = "auto"
    moderation: str = "auto"
    style: Optional[str] = None
    output_format: Optional[str] = None
    output_compression: Optional[int] = Field(None, ge=0, le=100)
    response_format: str = "b64_json"
    stream: bool = False
    partial_images: int = Field(0, ge=0, le=3)
    user: Optional[str] = None


class OpenAIDelta(BaseModel):
    role: Optional[str] = None
    content: Optional[str] = None
    reasoning_content: Optional[str] = None


class OpenAIChatCompletionStreamChoice(BaseModel):
    index: int
    delta: OpenAIDelta
    finish_reason: Optional[str] = None
    logprobs: Optional[Dict[str, Any]] = None


class OpenAIChatCompletionStreamResponse(BaseModel):
    id: str
    object: str = "chat.completion.chunk"
    created: int
    model: str
    choices: List[OpenAIChatCompletionStreamChoice]
    system_fingerprint: Optional[str] = None


# Gemini Models
class GeminiPart(BaseModel):
    text: Optional[str] = None
    inlineData: Optional[Dict[str, Any]] = None
    fileData: Optional[Dict[str, Any]] = None
    thought: Optional[bool] = None  # 改为 None，避免序列化时包含 False
    
    class Config:
        extra = "allow"  # 允许额外字段（如 functionCall, functionResponse）


class GeminiContent(BaseModel):
    role: str = "user"
    parts: List[GeminiPart]


class GeminiSystemInstruction(BaseModel):
    parts: List[GeminiPart]


class GeminiImageConfig(BaseModel):
    """图片生成配置"""
    model_config = ConfigDict(populate_by_name=True, extra="allow")

    aspect_ratio: Optional[str] = Field(
        None,
        validation_alias=AliasChoices("aspectRatio", "aspect_ratio"),
    )
    image_size: Optional[str] = Field(
        None,
        validation_alias=AliasChoices("imageSize", "image_size"),
    )


class GeminiResponseFormat(BaseModel):
    """Google 图片生成 responseFormat 配置。"""

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    image: Optional[GeminiImageConfig] = None


class GeminiGenerationConfig(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")

    temperature: Optional[float] = Field(None, ge=0.0, le=2.0)
    topP: Optional[float] = Field(None, ge=0.0, le=1.0)
    topK: Optional[int] = Field(None, ge=1)
    maxOutputTokens: Optional[int] = Field(None, ge=1)
    stopSequences: Optional[List[str]] = None
    responseMimeType: Optional[str] = None
    responseSchema: Optional[Dict[str, Any]] = None
    candidateCount: Optional[int] = Field(None, ge=1, le=8)
    seed: Optional[int] = None
    frequencyPenalty: Optional[float] = Field(None, ge=-2.0, le=2.0)
    presencePenalty: Optional[float] = Field(None, ge=-2.0, le=2.0)
    thinkingConfig: Optional[Dict[str, Any]] = None
    # 图片生成相关参数
    response_modalities: Optional[List[str]] = Field(
        None,
        validation_alias=AliasChoices("responseModalities", "response_modalities"),
    )  # ["TEXT", "IMAGE"]
    image_config: Optional[GeminiImageConfig] = Field(
        None,
        validation_alias=AliasChoices("imageConfig", "image_config"),
    )
    response_format: Optional[GeminiResponseFormat] = Field(
        None,
        validation_alias=AliasChoices("responseFormat", "response_format"),
    )


class GeminiSafetySetting(BaseModel):
    category: str
    threshold: str


class GeminiRequest(BaseModel):
    contents: List[GeminiContent]
    systemInstruction: Optional[GeminiSystemInstruction] = None
    generationConfig: Optional[GeminiGenerationConfig] = None
    safetySettings: Optional[List[GeminiSafetySetting]] = None
    tools: Optional[List[Dict[str, Any]]] = None
    toolConfig: Optional[Dict[str, Any]] = None
    cachedContent: Optional[str] = None

    class Config:
        extra = "allow"  # 允许透传未定义的字段


class GeminiCandidate(BaseModel):
    content: GeminiContent
    finishReason: Optional[str] = None
    index: int = 0
    safetyRatings: Optional[List[Dict[str, Any]]] = None
    citationMetadata: Optional[Dict[str, Any]] = None
    tokenCount: Optional[int] = None


class GeminiUsageMetadata(BaseModel):
    promptTokenCount: Optional[int] = None
    candidatesTokenCount: Optional[int] = None
    totalTokenCount: Optional[int] = None
    cachedContentTokenCount: Optional[int] = None
    thoughtsTokenCount: Optional[int] = None


class GeminiResponse(BaseModel):
    candidates: List[GeminiCandidate]
    usageMetadata: Optional[GeminiUsageMetadata] = None
    modelVersion: Optional[str] = None


# Claude Models
class ClaudeContentBlock(BaseModel):
    type: str  # "text", "image", "tool_use", "tool_result"
    text: Optional[str] = None
    source: Optional[Dict[str, Any]] = None  # for image type
    id: Optional[str] = None  # for tool_use
    name: Optional[str] = None  # for tool_use
    input: Optional[Dict[str, Any]] = None  # for tool_use
    tool_use_id: Optional[str] = None  # for tool_result
    content: Optional[Union[str, List[Dict[str, Any]]]] = None  # for tool_result


class ClaudeMessage(BaseModel):
    role: str  # "user" or "assistant"
    content: Union[str, List[ClaudeContentBlock]]


class ClaudeTool(BaseModel):
    name: str
    description: Optional[str] = None
    input_schema: Optional[Dict[str, Any]] = None


class ClaudeMetadata(BaseModel):
    user_id: Optional[str] = None


class ClaudeRequest(BaseModel):
    model: str
    messages: List[ClaudeMessage]
    max_tokens: int = Field(..., ge=1)
    system: Optional[Union[str, List[Dict[str, Any]]]] = None
    temperature: Optional[float] = Field(None, ge=0.0, le=1.0)
    top_p: Optional[float] = Field(None, ge=0.0, le=1.0)
    top_k: Optional[int] = Field(None, ge=1)
    stop_sequences: Optional[List[str]] = None
    stream: bool = False
    metadata: Optional[ClaudeMetadata] = None
    tools: Optional[List[ClaudeTool]] = None
    tool_choice: Optional[Union[str, Dict[str, Any]]] = None

    class Config:
        extra = "allow"


class ClaudeUsage(BaseModel):
    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: Optional[int] = None
    cache_read_input_tokens: Optional[int] = None


class ClaudeResponse(BaseModel):
    id: str
    type: str = "message"
    role: str = "assistant"
    content: List[ClaudeContentBlock]
    model: str
    stop_reason: Optional[str] = None
    stop_sequence: Optional[str] = None
    usage: ClaudeUsage


class ClaudeStreamEvent(BaseModel):
    type: str  # "message_start", "content_block_start", "content_block_delta", "content_block_stop", "message_delta", "message_stop"
    message: Optional[ClaudeResponse] = None
    index: Optional[int] = None
    content_block: Optional[ClaudeContentBlock] = None
    delta: Optional[Dict[str, Any]] = None
    usage: Optional[ClaudeUsage] = None

    class Config:
        extra = "allow"


# Error Models
class APIError(BaseModel):
    message: str
    type: str = "api_error"
    code: Optional[int] = None


class ErrorResponse(BaseModel):
    error: APIError


# Control Panel Models
class SystemStatus(BaseModel):
    status: str
    timestamp: str
    credentials: Dict[str, int]
    config: Dict[str, Any]
    current_credential: str


class CredentialInfo(BaseModel):
    filename: str
    project_id: Optional[str] = None
    status: Dict[str, Any]
    size: Optional[int] = None
    modified_time: Optional[str] = None
    error: Optional[str] = None


class LogEntry(BaseModel):
    timestamp: str
    level: str
    message: str
    module: Optional[str] = None


class ConfigValue(BaseModel):
    key: str
    value: Any
    env_locked: bool = False
    description: Optional[str] = None


# Authentication Models
class AuthRequest(BaseModel):
    project_id: Optional[str] = None
    user_session: Optional[str] = None


class AuthResponse(BaseModel):
    success: bool
    auth_url: Optional[str] = None
    state: Optional[str] = None
    error: Optional[str] = None
    credentials: Optional[Dict[str, Any]] = None
    file_path: Optional[str] = None
    requires_manual_project_id: Optional[bool] = None
    requires_project_selection: Optional[bool] = None
    available_projects: Optional[List[Dict[str, str]]] = None


class CredentialStatus(BaseModel):
    disabled: bool = False
    error_codes: List[int] = []
    last_success: Optional[str] = None


# Web Routes Models
class LoginRequest(BaseModel):
    password: str


class AuthStartRequest(BaseModel):
    project_id: Optional[str] = None  # 现在是可选的
    mode: Optional[str] = "geminicli"  # 凭证模式: geminicli 或 antigravity


class AuthCallbackRequest(BaseModel):
    project_id: Optional[str] = None  # 现在是可选的
    mode: Optional[str] = "geminicli"  # 凭证模式: geminicli 或 antigravity


class AuthCallbackUrlRequest(BaseModel):
    callback_url: str  # OAuth回调完整URL
    project_id: Optional[str] = None  # 可选的项目ID
    mode: Optional[str] = "geminicli"  # 凭证模式: geminicli 或 antigravity


class AuthAddByRefreshTokenRequest(BaseModel):
    refresh_token: str  # 必填的 refresh_token（仅支持 antigravity）
    proxy_url: Optional[str] = None  # 可选代理地址
    proxy_group_id: Optional[int] = None  # 可选代理组ID（优先于 proxy_url）


class CredFileActionRequest(BaseModel):
    filename: str
    action: str  # enable, disable, delete


class CredFileBatchActionRequest(BaseModel):
    action: str  # "enable", "disable", "delete"
    filenames: List[str]  # 批量操作的文件名列表


class ConfigSaveRequest(BaseModel):
    config: dict
