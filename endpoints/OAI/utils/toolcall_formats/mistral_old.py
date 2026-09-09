import json
from common.logger import xlogger
from endpoints.OAI.types.tools import ToolCall, Tool

"""
Mistral family, v2-v7 - JSON list

Raw format:
    [TOOL_CALLS]
    [
        {"name": "__FUNCTION_NAME_1__", "arguments": {"key": "value"}, "id": "__CALL_ID_1__"},
        {"name": "__FUNCTION_NAME_2__", "arguments": {"key": "value"}, "id": "__CALL_ID_2__"}
    ]

The model emits the [TOOL_CALLS] control token followed by a JSON array of
tool call objects. Each object contains "name", "arguments" (already a dict),
and optionally "id" (v3+). Multiple tool calls for parallel invocation appear as
multiple entries in the array.

There is no end token; tool calls simply appear at the end of the response stream.
"""

TOOLCALL_START = "[TOOL_CALLS]"
TOOLCALL_END = None

def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"mistral_old: Duplicate JSON key {key!r}")
        result[key] = value
    return result


def _reject_nonstandard_constant(value: str):
    raise ValueError(f"mistral_old: Non-standard JSON constant {value!r}")


def _load_json(raw_json: str):
    return json.loads(
        raw_json,
        object_pairs_hook=_reject_duplicate_keys,
        parse_constant=_reject_nonstandard_constant,
    )


def parse_toolcalls(text: str) -> list[ToolCall]:
    marker_pos = text.find(TOOLCALL_START)
    if marker_pos == -1:
        return []

    raw_json = text[marker_pos + len(TOOLCALL_START) :].strip()
    if not raw_json:
        raise ValueError("mistral_old: Tool-call batch has no JSON payload")

    try:
        calls = _load_json(raw_json)
    except (json.JSONDecodeError, ValueError) as exc:
        xlogger.warning(
            "mistral_old: Failed to parse tool call JSON",
            {"exception": str(exc), "raw_text": text, "raw_json": raw_json},
        )
        raise ValueError("mistral_old: Tool-call batch JSON is malformed") from exc

    if not isinstance(calls, list):
        raise ValueError("mistral_old: Tool-call batch must be a JSON array")
    if not calls:
        raise ValueError("mistral_old: Tool-call batch must contain at least one call")

    results = []
    for call in calls:
        if not isinstance(call, dict):
            raise ValueError("mistral_old: Every tool call must be a JSON object")
        func_name = call.get("name")
        if not isinstance(func_name, str) or not func_name.strip():
            raise ValueError("mistral_old: Tool call has no valid function name")
        func_name = func_name.strip()
        arguments = call.get("arguments", {})
        if isinstance(arguments, str):
            try:
                arguments = _load_json(arguments)
            except (json.JSONDecodeError, ValueError) as exc:
                raise ValueError("mistral_old: String arguments contain invalid JSON") from exc
        if not isinstance(arguments, dict):
            raise ValueError("mistral_old: Tool call arguments must be a JSON object")
        args_json = json.dumps(arguments, ensure_ascii=False)
        func = Tool(name=func_name, arguments=args_json)
        if "id" in call:
            call_id = call["id"]
            if not isinstance(call_id, str) or not call_id.strip():
                raise ValueError("mistral_old: Tool call id must be a non-empty string")
            results.append(ToolCall(id=call_id, function=func))
        else:
            results.append(ToolCall(function=func))

    xlogger.debug(
        f"mistral_old: Parsed {len(results)} tool calls",
        {"raw_text": text, "results": results},
    )
    return results
