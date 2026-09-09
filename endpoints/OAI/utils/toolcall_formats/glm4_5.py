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
_ARG_PAIR = re.compile(
    r"\s*<arg_key>(.*?)</arg_key>\s*<arg_value>(.*?)</arg_value>",
    re.DOTALL,
)


def parse_toolcalls(text: str) -> list[ToolCall]:
    outer_matches = list(_OUTER.finditer(text))

    # A tool batch is atomic. Never accept a valid prefix while silently
    # ignoring prose, an empty/nested block, or a malformed trailing call.
    # Only whitespace may appear between complete top-level envelopes.
    position = 0
    for match in outer_matches:
        if text[position : match.start()].strip():
            raise ValueError("unexpected text outside GLM tool-call envelope")
        if TOOLCALL_START in match.group(1) or TOOLCALL_END in match.group(1):
            raise ValueError("nested GLM tool-call envelope")
        position = match.end()
    if text[position:].strip():
        raise ValueError("incomplete or trailing GLM tool-call envelope")

    results = []
    for om in outer_matches:
        inner = om.group(1)

        # Extract function name: everything before the first <arg_key>
        name_match = _FUNC_NAME.match(inner)
        if not name_match:
            raise ValueError("missing GLM function name")
        func_name = name_match.group(1).strip()
        if not func_name:
            raise ValueError("blank GLM function name")

        # Parse a contiguous sequence of complete key/value pairs. Searching
        # for tags independently can silently ignore junk or shift a later
        # value onto the wrong key, changing the call's meaning.
        args: dict[str, any] = {}
        position = name_match.end()
        pair_count = 0
        while position < len(inner):
            if not inner[position:].strip():
                position = len(inner)
                break
            pair = _ARG_PAIR.match(inner, position)
            if pair is None:
                raise ValueError("incomplete or trailing GLM argument markup")
            raw_key, raw_value = pair.groups()
            key = raw_key.strip()
            if not key:
                raise ValueError("blank GLM argument key")
            if key in args:
                raise ValueError(f"duplicate GLM argument key {key!r}")
            args[key] = coerce_param_value(raw_value)
            position = pair.end()
            pair_count += 1

        marker_counts = {
            inner.count("<arg_key>"),
            inner.count("</arg_key>"),
            inner.count("<arg_value>"),
            inner.count("</arg_value>"),
        }
        if marker_counts != {pair_count}:
            raise ValueError("unmatched GLM argument key/value tag")

        args_json = json.dumps(args, ensure_ascii=False)
        results.append(ToolCall(function=Tool(name=func_name, arguments=args_json)))

    xlogger.debug(
        f"glm4: Parsed {len(results)} tool calls",
        {"raw_text": text, "results": results},
    )
    return results
