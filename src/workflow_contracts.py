"""Server-owned response contracts selected by authenticated Antigravity key ID."""

import json
import math
import os
from functools import lru_cache
from pathlib import Path

from fastapi import HTTPException
from jsonschema import Draft202012Validator, SchemaError, ValidationError
from pydantic import BaseModel, ConfigDict, Field, model_validator
from referencing import Registry
from referencing.exceptions import Unresolvable
from starlette.responses import JSONResponse, Response

WORKFLOW_JSON_GUIDANCE = (
    'Return only the JSON value specified by the server response schema. '
    'Omit Markdown fences and text delimiters. The gateway adds any required delimiters after validation.'
)
_MARKDOWN_JSON_GUIDANCE = (
    'Return exactly one JSON object. Put the requested human-readable Markdown in analysis_markdown '
    'and the final structured result in result. Do not put transport delimiters inside either value. '
    'The gateway renders the Markdown and final JSON block after validation.'
)


class WorkflowContract(BaseModel):
    model_config = ConfigDict(extra='forbid')

    response_schema: dict = Field(min_length=1)
    # Accept existing config files. Format protection no longer uses a paragraph list.
    prose_paragraphs: frozenset[str] = Field(default_factory=frozenset, exclude=True)
    legacy_markers: tuple[str, str] | None = None
    legacy_markdown_prefix: bool = False

    @model_validator(mode='after')
    def check_config(self):
        Draft202012Validator.check_schema(self.response_schema)
        if self.legacy_markers:
            start, end = self.legacy_markers
            if start == end or any(not marker.strip() or marker.splitlines() != [marker] for marker in (start, end)):
                raise ValueError('legacy_markers must be two distinct nonempty lines')
        elif self.legacy_markdown_prefix:
            raise ValueError('legacy_markdown_prefix requires legacy_markers')
        return self

    @property
    def json_guidance(self):
        return _MARKDOWN_JSON_GUIDANCE if self.legacy_markdown_prefix else WORKFLOW_JSON_GUIDANCE

    def apply_generation_config(self, request):
        from src.converter.openai2gemini import _clean_schema_for_gemini

        generation = dict(request.get('generationConfig') or {})
        generation.pop('responseJsonSchema', None)
        generation.pop('responseFormat', None)
        generation.pop('response_format', None)
        schema = _clean_schema_for_gemini(self.response_schema)
        if self.legacy_markdown_prefix:
            schema = {'type': 'OBJECT', 'required': ['analysis_markdown', 'result'], 'properties': {
                'analysis_markdown': {'type': 'STRING', 'minLength': 1}, 'result': schema,
            }}
        generation.update(responseMimeType='application/json', responseSchema=schema, candidateCount=1)
        request['generationConfig'] = generation


class _WorkflowConfig(BaseModel):
    model_config = ConfigDict(extra='forbid')

    bindings: dict[str, str]
    contracts: dict[str, WorkflowContract]

    @model_validator(mode='after')
    def check_bindings(self):
        if 'env' in self.bindings or any(
            not key.strip() or version not in self.contracts for key, version in self.bindings.items()
        ):
            raise ValueError('bindings require dedicated key IDs and defined contract versions')
        return self


@lru_cache(maxsize=4)
def _load_config(path: str) -> _WorkflowConfig:
    return _WorkflowConfig.model_validate(_parse_json(Path(path).read_bytes()))


def get_workflow_contract(api_key_id: str | None) -> WorkflowContract | None:
    path = os.getenv('ANTIGRAVITY_WORKFLOW_CONTRACTS_FILE')
    if not path or not api_key_id or api_key_id == 'env':
        return None
    try:
        config = _load_config(path)
    except (OSError, ValueError, SchemaError) as exc:
        raise HTTPException(503, 'Workflow contract configuration is unavailable') from exc
    return config.contracts.get(config.bindings.get(api_key_id))


def _unique_fields(pairs):
    fields = dict(pairs)
    if len(fields) != len(pairs):
        raise ValueError('duplicate JSON fields')
    return fields


def _reject_non_json_number(_):
    raise ValueError('non-JSON number')


def _finite_float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError('JSON number is outside the supported range')
    return number


def _parse_json(text):
    return json.loads(text, object_pairs_hook=_unique_fields, parse_constant=_reject_non_json_number,
                      parse_float=_finite_float)


def validate_workflow_response(response: Response, contract: WorkflowContract) -> Response:
    if response.status_code != 200:
        return response
    try:
        payload = _parse_json(response.body)
        result = payload.get('response', payload)
        candidate, = result['candidates']
        if candidate['finishReason'] != 'STOP':
            raise ValueError('incomplete response')
        parts = candidate['content']['parts']
        answer_parts = [part for part in parts if not part.get('thought')]
        text = ''.join(part['text'] for part in answer_parts).strip()
        value = _parse_json(text)
        prefix = ''
        if contract.legacy_markdown_prefix:
            if not isinstance(value, dict) or value.keys() != {'analysis_markdown', 'result'}:
                raise ValueError('missing Markdown or result')
            markdown = value['analysis_markdown']
            if not isinstance(markdown, str) or not markdown.strip() or any(
                marker in markdown for marker in contract.legacy_markers
            ):
                raise ValueError('invalid Markdown prefix')
            prefix = markdown.rstrip('\r\n') + '\n\n'
            value = value['result']
            text = json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
        Draft202012Validator(contract.response_schema, registry=Registry()).validate(value)
        if contract.legacy_markers:
            if any(marker in text for marker in contract.legacy_markers):
                raise ValueError('result contains a reserved transport marker')
            start, end = contract.legacy_markers
            answer_parts[0]['text'] = f'{prefix}{start}\n{text}\n{end}'
            for part in answer_parts[1:]:
                part['text'] = ''
            headers = {key: value for key, value in response.headers.items()
                       if key not in ('content-type', 'content-length', 'content-encoding')}
            return JSONResponse(payload, headers=headers)
    except (AttributeError, KeyError, TypeError, ValueError, RecursionError, ValidationError, Unresolvable):
        return JSONResponse({'error': {
            'code': 'invalid_workflow_output',
            'message': 'Upstream output did not satisfy the workflow response contract',
        }}, status_code=502)
    return response
