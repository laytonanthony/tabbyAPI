import re
import json
from common.logger import xlogger
from endpoints.OAI.types.tools import ToolCall, Tool
from endpoints.OAI.utils.toolcall_formats.common import coerce_param_value

"""
Qwen3.5 / Qwen3-Coder - pseudo-XML syntax

Raw format:
    <tool_call>
        <function=__FUNCTION_NAME__>
            <parameter=__PARAMETER_NAME_1__>
                __PARAMETER_1__
            </parameter>
            <parameter=__PARAMETER_NAME_2__>
                __PARAMETER_2__
            </parameter>
            ...
        </function>
    </tool_call>
"""

# TODO: the outer <tool_call> wrapper is supposedly optional in some deployments; the parser
#   handles both, but detecting tool calls in the stream currently relies on <tool_call> being
#   emitted by the model.

TOOLCALL_START = "<tool_call>"
TOOLCALL_END = "</tool_call>"

_OUTER = re.compile(r"\s*<tool_call>(.*?)</tool_call>", re.DOTALL)
_FUNC = re.compile(r"\s*<function=([^>\s]+)\s*>(.*?)</function>", re.DOTALL)
_PARAM = re.compile(r"\s*<parameter=([^>\s]+)\s*>(.*?)</parameter>", re.DOTALL)


def _parse_parameters(func_body: str, func_name: str) -> dict[str, any]:
    """Parse a complete, ordered parameter sequence for one function."""
    args: dict[str, any] = {}
    cursor = 0

    while cursor < len(func_body):
        match = _PARAM.match(func_body, cursor)
        if match is None:
            if func_body[cursor:].strip():
                raise ValueError(
                    f"Malformed parameter markup in Qwen tool call {func_name!r}"
                )
            break

        key = match.group(1).strip()
        if not key:
            raise ValueError(f"Blank parameter name in Qwen tool call {func_name!r}")
        if key in args:
            raise ValueError(
                f"Duplicate parameter {key!r} in Qwen tool call {func_name!r}"
            )

        args[key] = coerce_param_value(match.group(2))
        cursor = match.end()

    return args


def _parse_functions(text: str) -> list[ToolCall]:
    """Parse all function blocks, rejecting a partial parallel batch."""
    results: list[ToolCall] = []
    cursor = 0

    while cursor < len(text):
        match = _FUNC.match(text, cursor)
        if match is None:
            if text[cursor:].strip():
                raise ValueError("Malformed or misordered Qwen function markup")
            break

        func_name = match.group(1).strip()
        if not func_name:
            raise ValueError("Blank function name in Qwen tool call")

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
        raise ValueError("Qwen tool output did not contain a complete function call")
    return results


def parse_toolcalls(text: str) -> list[ToolCall]:
    if not text.strip():
        return []

    # The outer wrapper is optional, but a batch must use one form consistently.
    stripped = text.lstrip()
    is_wrapped = stripped.startswith("<tool_call") or stripped.startswith(
        "</tool_call>"
    )

    results: list[ToolCall] = []
    if is_wrapped:
        cursor = 0
        while cursor < len(text):
            match = _OUTER.match(text, cursor)
            if match is None:
                if text[cursor:].strip():
                    raise ValueError("Malformed or unmatched Qwen tool_call wrapper")
                break
            results.extend(_parse_functions(match.group(1)))
            cursor = match.end()
    else:
        results = _parse_functions(text)

    if not results:
        raise ValueError("Qwen tool output did not contain a complete tool call")

    xlogger.debug(
        f"qwen3_coder: Parsed {len(results)} tool calls",
        {"raw_text": text, "results": results, "is_wrapped": is_wrapped},
    )
    return results
