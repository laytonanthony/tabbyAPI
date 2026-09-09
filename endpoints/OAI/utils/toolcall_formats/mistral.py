import re
import json
from common.logger import xlogger
from endpoints.OAI.types.tools import ToolCall, Tool

"""
Mistral family, v11+ - Name/Args tokens

Raw format (single call):
    [TOOL_CALLS]get_weather[ARGS]{"location": "Paris", "format": "celsius"}

Raw format (parallel calls):
    [TOOL_CALLS]read[ARGS]{"filePath": "/path/x.jpg"}
    [TOOL_CALLS]read[ARGS]{"filePath": "/path/y.jpg"}

The model emits [TOOL_CALLS] followed by the function name as plain text,
then [ARGS] followed by a JSON object of arguments. For parallel calls the
pattern repeats with no separator. There is no id field in the raw output;
IDs should be assigned by the API server.

There is no end token; the sequence ends at EOS.
"""

TOOLCALL_START = "[TOOL_CALLS]"
TOOLCALL_END = None

_FUNCTION_NAME = re.compile(r"\S+")


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"mistral: Duplicate argument name {key!r}")
        result[key] = value
    return result


def _reject_nonstandard_constant(value: str):
    raise ValueError(f"mistral: Non-standard JSON constant {value!r}")


_JSON_DECODER = json.JSONDecoder(
    object_pairs_hook=_reject_duplicate_keys,
    parse_constant=_reject_nonstandard_constant,
)


def parse_toolcalls(text: str) -> list[ToolCall]:
    marker_pos = text.find(TOOLCALL_START)
    if marker_pos == -1:
        return []

    results = []
    while marker_pos != -1:
        name_start = marker_pos + len(TOOLCALL_START)
        args_pos = text.find("[ARGS]", name_start)
        next_marker = text.find(TOOLCALL_START, name_start)
        if args_pos == -1 or (next_marker != -1 and next_marker < args_pos):
            raise ValueError("mistral: Tool call is missing its [ARGS] marker")

        func_name = text[name_start:args_pos].strip()
        if not func_name or _FUNCTION_NAME.fullmatch(func_name) is None:
            raise ValueError("mistral: Tool call has an invalid function name")

        json_start = args_pos + len("[ARGS]")
        while json_start < len(text) and text[json_start].isspace():
            json_start += 1
        if json_start >= len(text):
            raise ValueError("mistral: Tool call has no arguments object")

        try:
            arguments, json_end = _JSON_DECODER.raw_decode(text, json_start)
        except (json.JSONDecodeError, ValueError) as exc:
            xlogger.warning(
                "mistral: Failed to parse tool call arguments",
                {
                    "exception": str(exc),
                    "function": func_name,
                    "raw_args": text[json_start:],
                },
            )
            raise ValueError("mistral: Tool call arguments are malformed") from exc

        if not isinstance(arguments, dict):
            raise ValueError("mistral: Tool call arguments must be a JSON object")

        args_json = json.dumps(arguments, ensure_ascii=False)
        results.append(ToolCall(function=Tool(name=func_name, arguments=args_json)))

        marker_pos = json_end
        while marker_pos < len(text) and text[marker_pos].isspace():
            marker_pos += 1
        if marker_pos == len(text):
            marker_pos = -1
        elif not text.startswith(TOOLCALL_START, marker_pos):
            raise ValueError("mistral: Unexpected text after tool call arguments")

    xlogger.debug(
        f"mistral: Parsed {len(results)} tool calls",
        {"raw_text": text, "results": results},
    )
    return results
