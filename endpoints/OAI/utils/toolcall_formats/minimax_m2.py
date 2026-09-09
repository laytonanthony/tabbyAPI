import re
import json
from common.logger import xlogger
from endpoints.OAI.types.tools import ToolCall, Tool
from endpoints.OAI.utils.toolcall_formats.common import coerce_param_value

"""
MiniMax M2 family (M2, M2.1, M2.5) - structured XML syntax

Raw format:
    <minimax:tool_call>
        <invoke name="__FUNCTION_NAME__">
            <parameter name="__PARAMETER_NAME_1__">__PARAMETER_1__</parameter>
            <parameter name="__PARAMETER_NAME_2__">__PARAMETER_2__</parameter>
            ...
        </invoke>
    </minimax:tool_call>

Multiple <invoke> blocks may appear within a single <minimax:tool_call> wrapper
for parallel tool calls. The input text may contain multiple <minimax:tool_call>
blocks.

Note: This format does NOT apply to MiniMax-M1, which uses a different
JSON-based tool call format.
"""

TOOLCALL_START = "<minimax:tool_call>"
TOOLCALL_END = "</minimax:tool_call>"

_OUTER = re.compile(
    r"\s*<minimax:tool_call>(.*?)</minimax:tool_call>", re.DOTALL
)
_INVOKE = re.compile(r'\s*<invoke\s+name="([^"]+)"\s*>(.*?)</invoke>', re.DOTALL)
_PARAM = re.compile(
    r'\s*<parameter\s+name="([^"]+)"\s*>(.*?)</parameter>', re.DOTALL
)


def _parse_parameters(func_body: str, func_name: str) -> dict[str, any]:
    args: dict[str, any] = {}
    cursor = 0

    while cursor < len(func_body):
        match = _PARAM.match(func_body, cursor)
        if match is None:
            if func_body[cursor:].strip():
                raise ValueError(
                    f"Malformed parameter markup in MiniMax tool call {func_name!r}"
                )
            break

        key = match.group(1).strip()
        if not key:
            raise ValueError(f"Blank parameter name in MiniMax tool call {func_name!r}")
        if key in args:
            raise ValueError(
                f"Duplicate parameter {key!r} in MiniMax tool call {func_name!r}"
            )

        args[key] = coerce_param_value(match.group(2))
        cursor = match.end()

    return args


def _parse_invocations(text: str) -> list[ToolCall]:
    results: list[ToolCall] = []
    cursor = 0

    while cursor < len(text):
        match = _INVOKE.match(text, cursor)
        if match is None:
            if text[cursor:].strip():
                raise ValueError("Malformed or misordered MiniMax invoke markup")
            break

        func_name = match.group(1).strip()
        if not func_name:
            raise ValueError("Blank function name in MiniMax tool call")

        args = _parse_parameters(match.group(2), func_name)
        results.append(
            ToolCall(
                function=Tool(
                    name=func_name,
                    arguments=json.dumps(args, ensure_ascii=False),
                )
            )
        )
        cursor = match.end()

    if not results:
        raise ValueError("MiniMax wrapper did not contain a complete invocation")
    return results


def parse_toolcalls(text: str) -> list[ToolCall]:
    if not text.strip():
        return []

    results: list[ToolCall] = []
    cursor = 0
    while cursor < len(text):
        match = _OUTER.match(text, cursor)
        if match is None:
            if text[cursor:].strip():
                raise ValueError("Malformed or unmatched MiniMax tool_call wrapper")
            break
        results.extend(_parse_invocations(match.group(1)))
        cursor = match.end()

    if not results:
        raise ValueError("MiniMax tool output did not contain a complete tool call")

    xlogger.debug(
        f"minimax_m2: Parsed {len(results)} tool calls",
        {"raw_text": text, "results": results},
    )
    return results
