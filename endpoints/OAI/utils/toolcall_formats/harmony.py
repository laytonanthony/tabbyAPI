import json
import re

from common.logger import xlogger
from endpoints.OAI.types.tools import ToolCall, Tool

"""
Harmony (gpt-oss) - structural message format

Tool calls are not delimited by tags in plain text; they are commentary
messages addressed to a recipient. The Harmony stream parser emits each one
on the tool channel as:

    <|channel|>commentary to=functions.__NAME__ <|constrain|>json<|message|>__JSON_ARGS__<|call|>

The recipient may also precede the channel (`assistant
to=functions.x<|channel|>commentary json<|message|>...`), as the chat
template renders it. One call per message; the model stops at <|call|>, so
in practice there is at most one call per generation.

This format requires the Harmony stream parser and is selected automatically
for Harmony models; TOOLCALL_START/END are None since there are no tags for
TagStreamParser to scan for.
"""

TOOLCALL_START = None
TOOLCALL_END = None

_RECIPIENT = re.compile(r"\bto=([^\s<]+)")


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"harmony: Duplicate argument name {key!r}")
        result[key] = value
    return result


def _reject_nonstandard_constant(value: str):
    raise ValueError(f"harmony: Non-standard JSON constant {value!r}")


def parse_toolcalls(text: str) -> list[ToolCall]:
    if "<|message|>" not in text and "<|call|>" not in text:
        return []

    results = []
    cursor = 0
    while cursor < len(text):
        message_pos = text.find("<|message|>", cursor)
        if message_pos == -1:
            trailing = text[cursor:]
            if trailing.strip():
                raise ValueError("harmony: Unexpected trailing tool-call text")
            break

        header = text[cursor:message_pos]
        if "<|call|>" in header:
            raise ValueError("harmony: Tool call terminator appears before its message")

        body_start = message_pos + len("<|message|>")
        call_end = text.find("<|call|>", body_start)
        next_message = text.find("<|message|>", body_start)
        if call_end == -1 or (next_message != -1 and next_message < call_end):
            raise ValueError("harmony: Tool call is missing its <|call|> terminator")
        body = text[body_start:call_end]

        recipients = _RECIPIENT.findall(header)
        if len(recipients) != 1:
            raise ValueError("harmony: Tool call message must have exactly one recipient")
        func_name = recipients[0].removeprefix("functions.")
        if not func_name.strip():
            raise ValueError("harmony: Tool call has no function name")

        raw_args = body.strip()
        if not raw_args:
            raise ValueError("harmony: Tool call has no JSON arguments object")
        try:
            arguments = json.loads(
                raw_args,
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_nonstandard_constant,
            )
        except (json.JSONDecodeError, ValueError) as exc:
            xlogger.warning(
                "harmony: Tool call arguments are not valid JSON",
                {"func_name": func_name, "args": raw_args, "exception": str(exc)},
            )
            raise ValueError("harmony: Tool call arguments are malformed") from exc
        if not isinstance(arguments, dict):
            raise ValueError("harmony: Tool call arguments must be a JSON object")

        args_json = json.dumps(arguments, ensure_ascii=False)
        results.append(ToolCall(function=Tool(name=func_name, arguments=args_json)))
        cursor = call_end + len("<|call|>")

    xlogger.debug(
        f"harmony: Parsed {len(results)} tool calls",
        {"raw_text": text, "results": results},
    )
    return results
