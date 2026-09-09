import re
import json
from common.logger import xlogger
from endpoints.OAI.types.tools import ToolCall, Tool
from endpoints.OAI.utils.toolcall_formats.common import coerce_param_value

"""
GLM-4.5 / GLM-4.6 / GLM-4.7 family - XML with interleaved key/value pairs

Raw format:
    <tool_call>__FUNCTION_NAME__
    <arg_key>__PARAMETER_NAME_1__</arg_key>
    <arg_value>__PARAMETER_VALUE_1__</arg_value>
    <arg_key>__PARAMETER_NAME_2__</arg_key>
    <arg_value>__PARAMETER_VALUE_2__</arg_value>
    ...
    </tool_call>

The function name appears as bare text immediately after <tool_call> (on the
same line or the next). Arguments are interleaved <arg_key>/<arg_value> pairs,
NOT nested inside a function/invoke wrapper. Multiple <tool_call> blocks may
appear for parallel tool calls.

Note: This format does NOT apply to GLM-4 or earlier models, which use a
different tool call mechanism.
"""

TOOLCALL_START = "<tool_call>"
TOOLCALL_END = "</tool_call>"

_OUTER = re.compile(r"<tool_call>(.*?)</tool_call>", re.DOTALL)
_FUNC_NAME = re.compile(r"^(.*?)(?=<arg_key>|$)", re.DOTALL)
_ARG_TOKEN = re.compile(r"<arg_(key|value)>(.*?)</arg_\1>", re.DOTALL)


def parse_toolcalls(text: str) -> list[ToolCall]:
    outer_matches = list(_OUTER.finditer(text))

    results = []
    for om in outer_matches:
        inner = om.group(1)

        # Extract function name: everything before the first <arg_key>
        name_match = _FUNC_NAME.match(inner)
        if not name_match:
            continue
        func_name = name_match.group(1).strip()
        if not func_name:
            continue

        # Preserve the emitted order. Extracting keys and values separately
        # can shift later values onto the wrong keys when one tag is missing.
        # Reject the entire call instead of risking a partially reconstructed
        # command with different semantics.
        tokens = [(m.group(1), m.group(2)) for m in _ARG_TOKEN.finditer(inner)]
        key_markers = inner.count("<arg_key>")
        value_markers = inner.count("<arg_value>")
        if key_markers != value_markers or len(tokens) != key_markers + value_markers:
            raise ValueError("unmatched GLM argument key/value tag")
        if len(tokens) % 2:
            raise ValueError("incomplete GLM argument key/value pair")

        args: dict[str, any] = {}
        for index in range(0, len(tokens), 2):
            key_type, raw_key = tokens[index]
            value_type, raw_value = tokens[index + 1]
            if key_type != "key" or value_type != "value":
                raise ValueError("misordered GLM argument key/value pair")
            key = raw_key.strip()
            if not key:
                raise ValueError("blank GLM argument key")
            if key in args:
                raise ValueError(f"duplicate GLM argument key {key!r}")
            args[key] = coerce_param_value(raw_value)

        args_json = json.dumps(args, ensure_ascii=False)
        results.append(ToolCall(function=Tool(name=func_name, arguments=args_json)))

    xlogger.debug(
        f"glm4: Parsed {len(results)} tool calls",
        {"raw_text": text, "results": results},
    )
    return results
