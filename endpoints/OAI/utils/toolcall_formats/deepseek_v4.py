import re
import json
from common.logger import xlogger
from endpoints.OAI.types.tools import ToolCall, Tool

"""
DeepSeek-V4 - DSML invoke blocks with explicitly typed parameters

Raw format:
    <｜DSML｜tool_calls>
    <｜DSML｜invoke name="__FUNCTION_NAME__">
    <｜DSML｜parameter name="__PARAM_1__" string="true|false">__VALUE_1__</｜DSML｜parameter>
    <｜DSML｜parameter name="__PARAM_2__" string="true|false">__VALUE_2__</｜DSML｜parameter>
    </｜DSML｜invoke>
    <｜DSML｜invoke name="__FUNCTION_NAME_2__">
    ...
    </｜DSML｜invoke>
    </｜DSML｜tool_calls>

The string attribute makes parameter typing explicit: string="true" values
are taken verbatim, string="false" values are parsed as JSON. ｜DSML｜ is a
single added token; the rest of each tag is ordinary text.

Note that the model's chat template requires tool results to appear in the
same order as the corresponding tool calls; the server sorts tool messages
before templating to guarantee this.
"""

TOOLCALL_START = "<｜DSML｜tool_calls>"
TOOLCALL_END = "</｜DSML｜tool_calls>"

_OUTER = re.compile(
    r"\s*<｜DSML｜tool_calls>(.*?)</｜DSML｜tool_calls>", re.DOTALL
)
_INVOKE = re.compile(
    r'\s*<｜DSML｜invoke name="([^"]+)"\s*>(.*?)</｜DSML｜invoke>', re.DOTALL
)
_PARAM = re.compile(
    r'\s*<｜DSML｜parameter name="([^"]+)"\s+string="(true|false)"\s*>'
    r"(.*?)</｜DSML｜parameter>",
    re.DOTALL,
)


def _parse_parameters(func_body: str, func_name: str) -> dict[str, object]:
    args: dict[str, object] = {}
    cursor = 0

    while cursor < len(func_body):
        match = _PARAM.match(func_body, cursor)
        if match is None:
            if func_body[cursor:].strip():
                raise ValueError(
                    f"Malformed parameter markup in DeepSeek tool call {func_name!r}"
                )
            break

        param_name, is_string, value = match.groups()
        param_name = param_name.strip()
        if not param_name:
            raise ValueError(
                f"Blank parameter name in DeepSeek tool call {func_name!r}"
            )
        if param_name in args:
            raise ValueError(
                f"Duplicate parameter {param_name!r} in DeepSeek tool call {func_name!r}"
            )

        if is_string == "true":
            args[param_name] = value
        else:
            try:
                args[param_name] = json.loads(value)
            except (json.JSONDecodeError, ValueError):
                # Keep the format's existing tolerant typed-value behavior.
                args[param_name] = value
        cursor = match.end()

    return args


def _parse_invocations(text: str) -> list[ToolCall]:
    results: list[ToolCall] = []
    cursor = 0

    while cursor < len(text):
        match = _INVOKE.match(text, cursor)
        if match is None:
            if text[cursor:].strip():
                raise ValueError("Malformed or misordered DeepSeek invoke markup")
            break

        func_name = match.group(1).strip()
        if not func_name:
            raise ValueError("Blank function name in DeepSeek tool call")

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
        raise ValueError("DeepSeek tool output did not contain a complete invocation")
    return results


def parse_toolcalls(text: str) -> list[ToolCall]:
    if not text.strip():
        return []

    stripped = text.lstrip()
    is_wrapped = stripped.startswith(TOOLCALL_START) or stripped.startswith(
        TOOLCALL_END
    )

    if is_wrapped:
        results: list[ToolCall] = []
        cursor = 0
        while cursor < len(text):
            match = _OUTER.match(text, cursor)
            if match is None:
                if text[cursor:].strip():
                    raise ValueError("Malformed or unmatched DeepSeek tool_calls wrapper")
                break
            results.extend(_parse_invocations(match.group(1)))
            cursor = match.end()
    else:
        results = _parse_invocations(text)

    if not results:
        raise ValueError("DeepSeek tool output did not contain a complete tool call")

    xlogger.debug(
        f"deepseek_v4: Parsed {len(results)} tool calls",
        {"raw_text": text, "results": results, "is_wrapped": is_wrapped},
    )
    return results
