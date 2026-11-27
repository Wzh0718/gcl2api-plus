"""把客户端工具声明对齐到官方 agy 2.35.2 的线上形态（顺序 + 大小写）。

只做两件事，且可逆、幂等：

1. 顺序：按官方 `functionDeclarations` 的声明顺序稳定排序；官方把全部声明放在
   一个 `tools` entry 里，这里同样合并成一个 entry（仅当每个 entry 只含
   `functionDeclarations` 时）。
2. 大小写/取值形态：`type` 改为大写枚举（`OBJECT`/`STRING`/`INTEGER`/`BOOLEAN`/
   `ARRAY`），数值约束改为字符串（`"minLength": "1"`），与 protojson `Struct`
   承载 JSON Schema 的官方写法一致。

不重命名工具、不改参数名、不改枚举值——调用语义保持不变，因此上游与客户端之间
无需额外映射。未在官方顺序里的工具按原相对顺序排在末尾。
"""
from __future__ import annotations

import copy
from typing import Any, Dict, Iterable, List, Optional, Tuple

# 官方 2.35.2 hub 实抓顺序（reports/agy-full-prompt-tools-2026-11-26）。
OFFICIAL_TOOL_ORDER_2_35_2: Tuple[str, ...] = (
    'read_file',
    'run_command',
    'manage_task',
    'send_message',
    'schedule',
    'write_file',
    'edit_file',
    'generate_image',
    'search_web',
    'ask_question',
)

# 各家 agent 工具集的规范顺序。对齐只改“声明顺序 + schema 形态”，不改工具名，
# 因此客户端调用与结果映射不受影响；检测不到时保持客户端原始顺序。
CLIENT_TOOL_ORDERS: Dict[str, Tuple[str, ...]] = {
    'agy': OFFICIAL_TOOL_ORDER_2_35_2,
    'dsh': (
        'bash', 'read', 'write', 'edit', 'grep', 'glob', 'working_directory',
        'present', 'ask_user_question', 'todo_write', 'skill', 'subagent',
        'subagent_fork', 'workflow', 'send_message', 'wait_agent', 'list_agents',
        'job_output', 'job_list', 'job_kill',
        'schedule_create', 'schedule_edit', 'schedule_list', 'schedule_view',
        'create_goal', 'update_goal', 'get_goal',
        'web_search', 'web_fetch', 'list_mcp_resources', 'read_mcp_resource',
        'plugin_manager',
    ),
    'claude_code': (
        'Task', 'Read', 'Write', 'Edit', 'MultiEdit', 'NotebookEdit', 'NotebookRead',
        'Bash', 'BashOutput', 'KillShell', 'Grep', 'Glob', 'LS',
        'WebFetch', 'WebSearch', 'TodoWrite', 'ExitPlanMode', 'SlashCommand',
    ),
    'codex': (
        'shell', 'apply_patch', 'update_plan', 'view_image', 'web_search',
    ),
}

_TYPE_ALIASES = {
    'object': 'OBJECT',
    'array': 'ARRAY',
    'string': 'STRING',
    'integer': 'INTEGER',
    'number': 'NUMBER',
    'boolean': 'BOOLEAN',
    'null': 'NULL',
}

# 官方把数值约束写成字符串；这些键统一转成 str。
_BOUND_KEYS = frozenset({
    'minLength', 'maxLength', 'minItems', 'maxItems',
    'minimum', 'maximum', 'multipleOf',
})

_SCHEMA_LIST_KEYS = ('anyOf', 'oneOf', 'allOf')
_SCHEMA_MAP_KEYS = ('properties', '$defs', 'definitions')


def _normalize_schema(schema: Any) -> Any:
    if isinstance(schema, list):
        return [_normalize_schema(item) for item in schema]
    if not isinstance(schema, dict):
        return schema

    result: Dict[str, Any] = {}
    for key, value in schema.items():
        if key == 'type' and isinstance(value, str):
            result[key] = _TYPE_ALIASES.get(value.strip().lower(), value)
        elif key == 'type' and isinstance(value, list):
            result[key] = [
                _TYPE_ALIASES.get(str(item).strip().lower(), item) if isinstance(item, str) else item
                for item in value
            ]
        elif key in _BOUND_KEYS and isinstance(value, (int, float)) and not isinstance(value, bool):
            result[key] = str(value)
        elif key in _SCHEMA_MAP_KEYS and isinstance(value, dict):
            result[key] = {name: _normalize_schema(sub) for name, sub in value.items()}
        elif key in ('items', 'additionalProperties', 'not', 'if', 'then', 'else') and isinstance(value, (dict, list)):
            result[key] = _normalize_schema(value)
        elif key in _SCHEMA_LIST_KEYS and isinstance(value, list):
            result[key] = [_normalize_schema(item) for item in value]
        else:
            result[key] = value
    return result


def normalize_declaration(declaration: Dict[str, Any]) -> Dict[str, Any]:
    """复制一份声明并把 schema 形态对齐官方写法。"""
    result = copy.deepcopy(declaration)
    parameters = result.get('parameters')
    if isinstance(parameters, dict):
        result['parameters'] = _normalize_schema(parameters)
    return result


def detect_client_tools(names: Iterable[Any]) -> Optional[str]:
    """按工具名交集猜客户端；命中不足两个名字时返回 None（保持原顺序）。"""
    declared = {str(name) for name in names if name}
    best_client, best_hits = None, 0
    for client, order in CLIENT_TOOL_ORDERS.items():
        hits = len(declared & set(order))
        if hits > best_hits:
            best_client, best_hits = client, hits
    return best_client if best_hits >= 2 else None


def _order_for(names: Iterable[Any], client: Optional[str] = None) -> Tuple[str, ...]:
    detected = client or detect_client_tools(names)
    return CLIENT_TOOL_ORDERS.get(detected or '', ())


def _ordered(declarations: Iterable[Dict[str, Any]], order: Tuple[str, ...] = OFFICIAL_TOOL_ORDER_2_35_2) -> List[Dict[str, Any]]:
    def index_of(name: Any) -> int:
        try:
            return order.index(str(name))
        except ValueError:
            return len(order)

    indexed = list(enumerate(declarations))
    return [
        decl for _, decl in sorted(indexed, key=lambda pair: (index_of(pair[1].get('name')), pair[0]))
    ]


def align_tool_declarations(tools: Any, client: Optional[str] = None) -> Any:
    """返回对齐后的 tools（不修改入参）。结构不可判定时只做 schema 归一。"""
    if not isinstance(tools, list) or not tools:
        return tools
    if not all(isinstance(entry, dict) for entry in tools):
        return tools

    declarations: List[Dict[str, Any]] = []
    mergeable = True
    for entry in tools:
        keys = set(entry.keys())
        if keys - {'functionDeclarations'}:
            mergeable = False
            break
        part = entry.get('functionDeclarations')
        if not isinstance(part, list) or not all(isinstance(d, dict) and d.get('name') for d in part):
            mergeable = False
            break
        declarations.extend(part)

    order = _order_for([d.get('name') for d in declarations], client)

    if not mergeable:
        normalized: List[Any] = []
        for entry in tools:
            part = entry.get('functionDeclarations')
            if isinstance(part, list) and all(isinstance(d, dict) and d.get('name') for d in part):
                normalized.append({
                    **entry,
                    'functionDeclarations': [normalize_declaration(d) for d in _ordered(part, order)],
                })
            else:
                normalized.append(copy.deepcopy(entry))
        return normalized

    return [{'functionDeclarations': [normalize_declaration(d) for d in _ordered(declarations, order)]}]
