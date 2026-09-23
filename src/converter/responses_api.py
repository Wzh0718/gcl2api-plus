"""
OpenAI Responses API 双向转换器

将 OpenAI Responses API (POST /v1/responses) 请求转换为内部 chat completions
请求字典（走现有 openai2gemini 管道），并把 chat completions 响应
（非流式 JSON / 流式 chunk）翻译回 Responses API 格式，
流式时输出 Codex CLI / OpenAI SDK 所需的完整 SSE 事件序列：

    response.created -> response.in_progress -> response.output_item.added
    -> response.content_part.added -> response.output_text.delta*
    -> response.output_text.done -> response.content_part.done
    -> response.output_item.done -> response.completed

工具调用时输出：

    response.output_item.added (function_call)
    -> response.function_call_arguments.delta* -> response.function_call_arguments.done
    -> response.output_item.done
"""

import time
import uuid
from typing import Any, Dict, List, Optional

from log import log


# ==================== 请求转换：Responses -> Chat Completions ====================

def _text_from_content_parts(parts: Any) -> str:
    """从 Responses 消息 content 数组中提取纯文本（input_text/output_text/text）。"""
    if not isinstance(parts, list):
        return ""
    texts = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        part_type = part.get("type")
        if part_type in ("input_text", "output_text", "text"):
            text = part.get("text")
            if isinstance(text, str):
                texts.append(text)
        else:
            log.warning(f"[RESPONSES-API] 跳过暂不支持的 content part 类型: {part_type}")
    return "".join(texts)


def _message_from_item(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """把 Responses input 中的 message 项转为 chat message；不支持的类型返回 None。"""
    item_type = item.get("type", "message")
    if item_type == "message":
        role = item.get("role", "user")
        # developer 角色在下游管道中按 system 处理
        if role == "developer":
            role = "system"
        content = item.get("content")
        if isinstance(content, str):
            text = content
        else:
            text = _text_from_content_parts(content)
        return {"role": role, "content": text}
    return None


def _messages_from_input(resp: Dict[str, Any]) -> List[Dict[str, Any]]:
    """把 instructions + input（字符串或 Responses item 列表）展开为 chat messages。"""
    messages: List[Dict[str, Any]] = []

    instructions = resp.get("instructions")
    if isinstance(instructions, str) and instructions:
        messages.append({"role": "system", "content": instructions})

    inp = resp.get("input", "")
    if isinstance(inp, str):
        if inp:
            messages.append({"role": "user", "content": inp})
        return messages

    if not isinstance(inp, list):
        raise ValueError("Field 'input' must be a string or an array of items")

    pending_tool_calls: List[Dict[str, Any]] = []

    def flush_tool_calls():
        """把连续的 function_call 项合并为一条 assistant tool_calls 消息。"""
        nonlocal pending_tool_calls
        if pending_tool_calls:
            messages.append({"role": "assistant", "content": None, "tool_calls": pending_tool_calls})
            pending_tool_calls = []

    for item in inp:
        if not isinstance(item, dict):
            raise ValueError("Each 'input' item must be an object")

        item_type = item.get("type", "message")

        if item_type == "function_call":
            pending_tool_calls.append({
                "id": item.get("call_id") or f"call_{uuid.uuid4().hex[:24]}",
                "type": "function",
                "function": {
                    "name": item.get("name", ""),
                    "arguments": item.get("arguments", "") or "",
                },
            })
            continue

        flush_tool_calls()

        if item_type == "function_call_output":
            output = item.get("output", "")
            if isinstance(output, list):
                output = _text_from_content_parts(output)
            messages.append({
                "role": "tool",
                "tool_call_id": item.get("call_id", ""),
                "content": output if isinstance(output, str) else str(output),
            })
            continue

        if item_type in ("reasoning", "item_reference"):
            # reasoning（加密思维链）无法回放到 Gemini，客户端配合 store:false 时
            # 应内联完整历史；item_reference 依赖服务端存储，均跳过。
            log.debug(f"[RESPONSES-API] 跳过 input 项类型: {item_type}")
            continue

        message = _message_from_item(item)
        if message is not None:
            messages.append(message)
        else:
            log.warning(f"[RESPONSES-API] 跳过不支持的 input 项类型: {item_type}")

    flush_tool_calls()
    return messages


def _convert_tools(tools: Any) -> Optional[List[Dict[str, Any]]]:
    """Responses tools -> chat tools。只保留 function 类型，内置工具跳过。"""
    if not isinstance(tools, list):
        return None
    chat_tools = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        if tool.get("type") == "function":
            function = {
                "name": tool.get("name", ""),
                "description": tool.get("description", ""),
                "parameters": tool.get("parameters") or {"type": "object", "properties": {}},
            }
            if tool.get("strict") is not None:
                function["strict"] = tool["strict"]
            chat_tools.append({"type": "function", "function": function})
        else:
            log.warning(
                f"[RESPONSES-API] 跳过暂不支持的 tool 类型: {tool.get('type')}"
            )
    return chat_tools or None


def _convert_tool_choice(tool_choice: Any) -> Optional[Any]:
    """Responses tool_choice -> chat tool_choice。"""
    if tool_choice is None:
        return None
    if isinstance(tool_choice, str):
        return tool_choice
    if isinstance(tool_choice, dict) and tool_choice.get("type") == "function":
        return {"type": "function", "function": {"name": tool_choice.get("name", "")}}
    return None


def responses_request_to_chat_dict(resp: Dict[str, Any]) -> Dict[str, Any]:
    """
    OpenAI Responses API 请求体 -> 内部 chat completions 请求字典。

    Raises:
        ValueError: 请求体缺少 model 或 input 结构非法。
    """
    model = resp.get("model")
    if not model or not isinstance(model, str):
        raise ValueError("Field 'model' is required")

    chat: Dict[str, Any] = {
        "model": model,
        "messages": _messages_from_input(resp),
    }

    stream = bool(resp.get("stream"))
    chat["stream"] = stream
    if stream:
        # 让上游 chat chunk 流携带 usage，供 response.completed 使用
        chat["stream_options"] = {"include_usage": True}

    if resp.get("max_output_tokens") is not None:
        chat["max_tokens"] = resp["max_output_tokens"]
    if resp.get("temperature") is not None:
        chat["temperature"] = resp["temperature"]
    if resp.get("top_p") is not None:
        chat["top_p"] = resp["top_p"]
    if resp.get("parallel_tool_calls") is not None:
        chat["parallel_tool_calls"] = resp["parallel_tool_calls"]

    tools = _convert_tools(resp.get("tools"))
    if tools:
        chat["tools"] = tools

    tool_choice = _convert_tool_choice(resp.get("tool_choice"))
    if tool_choice is not None:
        chat["tool_choice"] = tool_choice

    # store / metadata / prompt_cache_key / include 等 Responses 专有字段在此截止，
    # 不向下游管道透传。
    return chat


# ==================== 响应转换：Chat Completions -> Responses ====================

def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _build_message_item(text: str, item_id: Optional[str] = None) -> Dict[str, Any]:
    return {
        "id": item_id or _new_id("msg"),
        "type": "message",
        "status": "completed",
        "role": "assistant",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }


def _build_function_call_item(
    name: str,
    arguments: str,
    call_id: str,
    item_id: Optional[str] = None,
    status: str = "completed",
) -> Dict[str, Any]:
    return {
        "id": item_id or _new_id("fc"),
        "type": "function_call",
        "status": status,
        "name": name,
        "arguments": arguments,
        "call_id": call_id,
    }


def _convert_usage(usage: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """chat usage -> Responses usage。"""
    if not usage:
        return None
    converted = {
        "input_tokens": usage.get("prompt_tokens", 0),
        "input_tokens_details": {"cached_tokens": 0},
        "output_tokens": usage.get("completion_tokens", 0),
        "output_tokens_details": {"reasoning_tokens": 0},
        "total_tokens": usage.get("total_tokens", 0),
    }
    prompt_details = usage.get("prompt_tokens_details") or {}
    if isinstance(prompt_details.get("cached_tokens"), int):
        converted["input_tokens_details"]["cached_tokens"] = prompt_details["cached_tokens"]
    completion_details = usage.get("completion_tokens_details") or {}
    if isinstance(completion_details.get("reasoning_tokens"), int):
        converted["output_tokens_details"]["reasoning_tokens"] = completion_details["reasoning_tokens"]
    return converted


def chat_completion_to_responses(
    chat: Dict[str, Any],
    fallback_model: str = "",
) -> Dict[str, Any]:
    """chat completion 响应 -> Responses API response 对象（status=completed）。"""
    output: List[Dict[str, Any]] = []
    text_parts: List[str] = []
    function_items: List[Dict[str, Any]] = []

    choices = chat.get("choices") or []
    if choices:
        message = choices[0].get("message") or {}
        content = message.get("content")
        if isinstance(content, str) and content:
            text_parts.append(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    text_parts.append(part["text"])
        for tool_call in message.get("tool_calls") or []:
            function = tool_call.get("function") or {}
            function_items.append(
                _build_function_call_item(
                    name=function.get("name", ""),
                    arguments=function.get("arguments", "") or "",
                    call_id=tool_call.get("id") or _new_id("call"),
                )
            )

    if text_parts:
        output.append(_build_message_item("".join(text_parts)))
    elif not function_items:
        # 空回复也保持 message item，避免客户端拿到空 output
        output.append(_build_message_item(""))
    output.extend(function_items)

    return {
        "id": _new_id("resp"),
        "object": "response",
        "created_at": int(time.time()),
        "status": "completed",
        "error": None,
        "incomplete_details": None,
        "instructions": None,
        "max_output_tokens": None,
        "model": chat.get("model") or fallback_model,
        "output": output,
        "parallel_tool_calls": True,
        "previous_response_id": None,
        "reasoning": {"effort": None, "summary": None},
        "store": False,
        "temperature": None,
        "text": {"format": {"type": "text"}},
        "tool_choice": "auto",
        "tools": [],
        "top_p": None,
        "usage": _convert_usage(chat.get("usage")),
        "metadata": {},
        "user": None,
    }


# ==================== 流式转换：Chat Chunks -> Responses SSE 事件 ====================

class ResponsesStreamTranslator:
    """
    把 chat.completion.chunk 流翻译为 Responses API SSE 事件序列。

    用法：
        translator = ResponsesStreamTranslator(model)
        events = translator.start()
        for chunk in chat_chunks:
            events += translator.on_chat_chunk(chunk)
        events += translator.finalize()
    """

    def __init__(self, model: str):
        self.model = model
        self.response_id = _new_id("resp")
        self.created_at = int(time.time())
        self._seq = 0
        self._terminal = False
        self._incomplete = False

        self._output_items: List[Dict[str, Any]] = []
        self._usage: Optional[Dict[str, Any]] = None
        self._next_output_index = 0

        # 当前打开的 message item 状态
        self._msg_open = False
        self._msg_item_id: Optional[str] = None
        self._msg_output_index = 0
        self._msg_text = ""

        # tool index -> 累积状态
        self._tools: Dict[int, Dict[str, Any]] = {}

    # ---------- 基础事件 ----------

    def _event(self, event_type: str, **fields: Any) -> Dict[str, Any]:
        event = {"type": event_type, "sequence_number": self._seq}
        self._seq += 1
        event.update(fields)
        return event

    def _response_object(self, status: str = "in_progress") -> Dict[str, Any]:
        return {
            "id": self.response_id,
            "object": "response",
            "created_at": self.created_at,
            "status": status,
            "error": None,
            "incomplete_details": None,
            "instructions": None,
            "max_output_tokens": None,
            "model": self.model,
            "output": list(self._output_items),
            "parallel_tool_calls": True,
            "previous_response_id": None,
            "reasoning": {"effort": None, "summary": None},
            "store": False,
            "temperature": None,
            "text": {"format": {"type": "text"}},
            "tool_choice": "auto",
            "tools": [],
            "top_p": None,
            "usage": self._usage,
            "metadata": {},
            "user": None,
        }

    # ---------- 生命周期 ----------

    def has_output(self) -> bool:
        """是否已产生任何输出 item（用于异常断流时判断能否正常收尾）。"""
        return bool(self._output_items) or self._msg_open or bool(self._tools)

    def start(self) -> List[Dict[str, Any]]:
        return [
            self._event("response.created", response=self._response_object()),
            self._event("response.in_progress", response=self._response_object()),
        ]

    def on_error(self, message: str) -> List[Dict[str, Any]]:
        """上游出错时输出 response.failed 终止事件。"""
        if self._terminal:
            return []
        self._terminal = True
        response = self._response_object(status="failed")
        response["error"] = {
            "code": "server_error",
            "message": message or "Upstream stream error",
        }
        return [self._event("response.failed", response=response)]

    def finalize(self) -> List[Dict[str, Any]]:
        """流正常结束时闭合未关闭 item 并输出 response.completed。"""
        if self._terminal:
            return []
        events: List[Dict[str, Any]] = []
        events.extend(self._close_message())
        events.extend(self._close_all_tools())
        self._terminal = True

        if self._incomplete:
            response = self._response_object(status="incomplete")
            response["incomplete_details"] = {"reason": "max_output_tokens"}
        else:
            response = self._response_object(status="completed")
        events.append(self._event("response.completed", response=response))
        return events

    # ---------- chat chunk 处理 ----------

    def on_chat_chunk(self, chunk: Dict[str, Any]) -> List[Dict[str, Any]]:
        if self._terminal:
            return []

        if "error" in chunk and "choices" not in chunk:
            error = chunk.get("error")
            message = error.get("message") if isinstance(error, dict) else str(error)
            return self.on_error(message or "Upstream stream error")

        if chunk.get("usage"):
            self._usage = _convert_usage(chunk["usage"])

        events: List[Dict[str, Any]] = []
        choices = chunk.get("choices") or []
        for choice in choices[:1]:  # Gemini 管道单 candidate
            delta = choice.get("delta") or {}

            content = delta.get("content")
            if isinstance(content, str) and content:
                events.extend(self._on_text_delta(content))

            for tool_call in delta.get("tool_calls") or []:
                events.extend(self._on_tool_call_delta(tool_call))

            finish_reason = choice.get("finish_reason")
            if finish_reason:
                if finish_reason == "length":
                    self._incomplete = True
                events.extend(self._close_message())
                events.extend(self._close_all_tools())

        return events

    # ---------- 内部：文本 ----------

    def _on_text_delta(self, delta_text: str) -> List[Dict[str, Any]]:
        events: List[Dict[str, Any]] = []
        if not self._msg_open:
            self._msg_open = True
            self._msg_item_id = _new_id("msg")
            self._msg_output_index = self._next_output_index
            self._next_output_index += 1
            message_item = _build_message_item("", item_id=self._msg_item_id)
            message_item["status"] = "in_progress"
            message_item["content"] = []
            events.append(self._event(
                "response.output_item.added",
                output_index=self._msg_output_index,
                item=message_item,
            ))
            events.append(self._event(
                "response.content_part.added",
                item_id=self._msg_item_id,
                output_index=self._msg_output_index,
                content_index=0,
                part={"type": "output_text", "text": "", "annotations": []},
            ))

        self._msg_text += delta_text
        events.append(self._event(
            "response.output_text.delta",
            item_id=self._msg_item_id,
            output_index=self._msg_output_index,
            content_index=0,
            delta=delta_text,
        ))
        return events

    def _close_message(self) -> List[Dict[str, Any]]:
        if not self._msg_open:
            return []
        self._msg_open = False
        item_id = self._msg_item_id
        output_index = self._msg_output_index
        text = self._msg_text

        item = _build_message_item(text, item_id=item_id)
        self._output_items.append(item)
        return [
            self._event(
                "response.output_text.done",
                item_id=item_id,
                output_index=output_index,
                content_index=0,
                text=text,
            ),
            self._event(
                "response.content_part.done",
                item_id=item_id,
                output_index=output_index,
                content_index=0,
                part={"type": "output_text", "text": text, "annotations": []},
            ),
            self._event(
                "response.output_item.done",
                output_index=output_index,
                item=item,
            ),
        ]

    # ---------- 内部：工具调用 ----------

    def _on_tool_call_delta(self, tool_call: Dict[str, Any]) -> List[Dict[str, Any]]:
        events: List[Dict[str, Any]] = []
        index = tool_call.get("index", 0)
        function = tool_call.get("function") or {}

        state = self._tools.get(index)
        if state is None:
            state = {
                "item_id": _new_id("fc"),
                "call_id": tool_call.get("id") or _new_id("call"),
                "name": function.get("name") or "",
                "arguments": "",
                "output_index": self._next_output_index,
            }
            self._next_output_index += 1
            self._tools[index] = state
            item = _build_function_call_item(
                name=state["name"],
                arguments="",
                call_id=state["call_id"],
                item_id=state["item_id"],
                status="in_progress",
            )
            events.append(self._event(
                "response.output_item.added",
                output_index=state["output_index"],
                item=item,
            ))

        if function.get("name"):
            state["name"] = function["name"]

        arguments = function.get("arguments")
        if isinstance(arguments, str) and arguments:
            state["arguments"] += arguments
            events.append(self._event(
                "response.function_call_arguments.delta",
                item_id=state["item_id"],
                output_index=state["output_index"],
                delta=arguments,
            ))
        return events

    def _close_all_tools(self) -> List[Dict[str, Any]]:
        events: List[Dict[str, Any]] = []
        for index in sorted(self._tools):
            state = self._tools[index]
            if state is None:
                continue
            item = _build_function_call_item(
                name=state["name"],
                arguments=state["arguments"],
                call_id=state["call_id"],
                item_id=state["item_id"],
            )
            self._output_items.append(item)
            events.append(self._event(
                "response.function_call_arguments.done",
                item_id=state["item_id"],
                output_index=state["output_index"],
                arguments=state["arguments"],
            ))
            events.append(self._event(
                "response.output_item.done",
                output_index=state["output_index"],
                item=item,
            ))
        self._tools = {}
        return events
