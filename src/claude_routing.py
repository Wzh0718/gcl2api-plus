"""Request-scoped account routing for managed Antigravity API keys.

The legacy Claude names remain compatible with existing stored key bindings.
"""
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
from inspect import signature


@dataclass(frozen=True)
class ClaudeRoute:
    api_key_id: str
    credential_name: str | None
    prompt_fusion_mode: str = "inherit"
    allowed_model: str = ""


_route: ContextVar[ClaudeRoute | None] = ContextVar('claude_account_route', default=None)


def current_api_key_id() -> str:
    route = _route.get()
    return route.api_key_id if route else 'env'


def bound_claude_credential() -> str | None:
    route = _route.get()
    return route.credential_name if route else None


def key_prompt_fusion_mode() -> str:
    route = _route.get()
    return route.prompt_fusion_mode if route else 'inherit'


def key_allowed_model() -> str:
    route = _route.get()
    return route.allowed_model if route else ''


def _check_model(route, model):
    if route and route.allowed_model and model != route.allowed_model:
        from fastapi import HTTPException
        raise HTTPException(403, f'该密钥仅允许执行模型：{route.allowed_model}')


def enforce_key_model(model: str) -> None:
    _check_model(_route.get(), model)


async def _load_route(api_key_id, model):
    existing = _route.get()
    if existing is not None and existing.api_key_id == api_key_id:
        return existing
    credential = None
    record = {}
    if api_key_id != 'env':
        from src.storage_adapter import get_storage_adapter
        from fastapi import HTTPException
        record = await (await get_storage_adapter()).get_api_key_record(api_key_id)
        if record is None:
            raise HTTPException(503, '调用方密钥路由配置不可用')
        credential = record.get('claude_credential_name') or None
    return ClaudeRoute(api_key_id, credential, record.get('prompt_fusion_mode', 'inherit'), record.get('allowed_model') or '')


@asynccontextmanager
async def claude_route_scope(api_key_id, model):
    token = _route.set(await _load_route(api_key_id, model))
    try:
        yield
    finally:
        _route.reset(token)


def bind_claude_request(function):
    sig = signature(function)
    @wraps(function)
    async def wrapped(*args, **kwargs):
        arguments = sig.bind(*args, **kwargs)
        arguments.apply_defaults()
        async with claude_route_scope(arguments.arguments['api_key_id'], arguments.arguments.get('request_model') or arguments.arguments['body'].get('model', '')):
            enforce_key_model(arguments.arguments['body'].get('model', ''))
            return await function(*args, **kwargs)
    return wrapped


def bind_claude_stream(function):
    sig = signature(function)
    @wraps(function)
    async def wrapped(*args, **kwargs):
        arguments = sig.bind(*args, **kwargs)
        arguments.apply_defaults()
        route = await _load_route(arguments.arguments['api_key_id'], arguments.arguments.get('request_model') or arguments.arguments['body'].get('model', ''))
        _check_model(route, arguments.arguments['body'].get('model', ''))
        inner = function(*args, **kwargs)
        try:
            while True:
                # The first item and subsequent ASGI iteration may run in different tasks.
                # Bind only around advancement; never leave a ContextVar set across yield.
                token = _route.set(route)
                try:
                    item = await anext(inner)
                except StopAsyncIteration:
                    break
                finally:
                    _route.reset(token)
                yield item
        finally:
            token = _route.set(route)
            try:
                await inner.aclose()
            finally:
                _route.reset(token)
    return wrapped


def claude_routing_options(request_model: str, execution_model: str) -> dict:
    """Retain Claude intent when a legacy alias normalizes to a non-Claude model."""
    if 'claude' in request_model.lower() and 'claude' not in execution_model.lower():
        return {'request_model': request_model}
    return {}
