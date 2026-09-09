import re
import json
from common.logger import xlogger
from endpoints.OAI.types.tools import ToolCall, Tool
from endpoints.OAI.utils.toolcall_formats.common import coerce_param_value

"""
Hy3 (Tencent Hunyuan) - tokenized XML with interleaved key/value pairs

Raw format:
    <tool_calls:opensource>
    <tool_call:opensource>__FUNCTION_NAME__<tool_sep:opensource>
    <arg_key:opensource>__PARAMETER_NAME_1__</arg_key:opensource>
    <arg_value:opensource>__PARAMETER_VALUE_1__</arg_value:opensource>
    <arg_key:opensource>__PARAMETER_NAME_2__</arg_key:opensource>
    <arg_value:opensource>__PARAMETER_VALUE_2__</arg_value:opensource>
    ...
    </tool_call:opensource>
    ...
    </tool_calls:opensource>

Every tag is a single added token; the ':opensource' suffix is part of the
token string. Parallel calls appear as multiple <tool_call:opensource> blocks
inside a single <tool_calls:opensource> wrapper, which the model closes before
emitting EOS. String argument values are rendered raw; other types are
rendered as JSON.
"""

TOOLCALL_START = "<tool_calls:opensource>"
TOOLCALL_END = "</tool_calls:opensource>"

_OUTER = re.compile(r"<tool_call:opensource>(.*?)</tool_call:opensource>", re.DOTALL)
_FUNC_NAME = re.compile(r"^(.*?)(?=<tool_sep:opensource>|<arg_key:opensource>|$)", re.DOTALL)
_ARG_TOKEN = re.compile(
    r"<arg_(key|value):opensource>(.*?)</arg_\1:opensource>", re.DOTALL
)


def parse_toolcalls(text: str) -> list[ToolCall]:
    outer_matches = list(_OUTER.finditer(text))

    results = []
    for om in outer_matches:
        inner = om.group(1)

        # Extract function name: everything before <tool_sep:opensource>
        name_match = _FUNC_NAME.match(inner)
        if not name_match:
            continue
        func_name = name_match.group(1).strip()
        if not func_name:
            continue

        # Preserve the emitted order so a missing value cannot shift every
        # later argument onto the wrong key.
        tokens = [(m.group(1), m.group(2)) for m in _ARG_TOKEN.finditer(inner)]
        key_markers = inner.count("<arg_key:opensource>")
        value_markers = inner.count("<arg_value:opensource>")
        if key_markers != value_markers or len(tokens) != key_markers + value_markers:
            raise ValueError("unmatched Hy3 argument key/value tag")
        if len(tokens) % 2:
            raise ValueError("incomplete Hy3 argument key/value pair")

        args: dict[str, any] = {}
        for index in range(0, len(tokens), 2):
            key_type, raw_key = tokens[index]
            value_type, raw_value = tokens[index + 1]
            if key_type != "key" or value_type != "value":
                raise ValueError("misordered Hy3 argument key/value pair")
            key = raw_key.strip()
            if not key:
                raise ValueError("blank Hy3 argument key")
            if key in args:
                raise ValueError(f"duplicate Hy3 argument key {key!r}")
            args[key] = coerce_param_value(raw_value)

        args_json = json.dumps(args, ensure_ascii=False)
        results.append(ToolCall(function=Tool(name=func_name, arguments=args_json)))

    xlogger.debug(
        f"hy3: Parsed {len(results)} tool calls",
        {"raw_text": text, "results": results},
    )
    return results
