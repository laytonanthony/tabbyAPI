import json
import re

from common.logger import xlogger
from endpoints.OAI.types.tools import ToolCall, Tool

"""
Muse Glimmer - structural message format with ATEM tool call syntax

Tool calls are assistant messages addressed to a tool recipient. The Glimmer
stream parser emits each one on the tool channel as:

    <|start|>assistant to=__NAME__<|message|><atem:function_calls>
    <atem:invoke name="__NAME__">
    <atem:parameter name="__PARAMETER_NAME__">__PARAMETER_VALUE__</atem:parameter>
    ...
    </atem:invoke>
    </atem:function_calls><|eom|>

The invoke name inside the body is authoritative; the recipient duplicates
it. Scalar parameter values are written as-is, lists and objects as JSON.
Per the format's own system prompt, the output is not expected to be valid
XML and is parsed with regular expressions.

This format requires the Glimmer stream parser and is selected automatically
for Muse Glimmer models; TOOLCALL_START/END are None since there are no tags
for TagStreamParser to scan for.
"""

TOOLCALL_START = None
TOOLCALL_END = None

_INVOKE_OPEN = re.compile(r'<atem:invoke name="([^"]+)">')
_INVOKE_TOKEN = re.compile(r'<atem:invoke name="([^"]+)">|</atem:invoke>')
_PARAM_TOKEN = re.compile(r'<atem:parameter name="([^"]*)">|</atem:parameter>')

_ALLOWED_OUTSIDE_PARAMETERS = (
    "</atem:function_calls>",
    "<|eom|>",
    "<|eot|>",
)


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"muse_glimmer: Duplicate JSON key {key!r}")
        result[key] = value
    return result


def _reject_nonstandard_constant(value: str):
    raise ValueError(f"muse_glimmer: Non-standard JSON constant {value!r}")


def _coerce_param_value(raw: str) -> any:
    """
    JSON-decode lists, objects, numbers, booleans and null; keep anything
    else as a string. String values are not stripped: the template renders
    them verbatim, with no whitespace around the parameter tags.
    """

    try:
        return json.loads(
            raw,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonstandard_constant,
        )
    except (json.JSONDecodeError, ValueError):
        if raw.lstrip().startswith(("{", "[")):
            raise ValueError("muse_glimmer: Structured parameter contains malformed JSON")
        return raw


def _validate_outside_parameter_text(text: str):
    for token in _ALLOWED_OUTSIDE_PARAMETERS:
        text = text.replace(token, "")
    if text.strip():
        raise ValueError("muse_glimmer: Unexpected text outside parameter tags")


def _parse_parameters(body: str) -> dict:
    marker_count = body.count("<atem:parameter")
    tokens = list(_PARAM_TOKEN.finditer(body))
    open_count = sum(match.group(1) is not None for match in tokens)
    if marker_count != open_count:
        raise ValueError("muse_glimmer: Malformed parameter opening tag")

    args = {}
    outside_start = 0
    pending_name = None
    value_start = None
    for token in tokens:
        name = token.group(1)
        if name is not None:
            if pending_name is not None:
                raise ValueError("muse_glimmer: Nested or unclosed parameter tag")
            _validate_outside_parameter_text(body[outside_start : token.start()])
            name = name.strip()
            if not name:
                raise ValueError("muse_glimmer: Parameter has no name")
            if name in args:
                raise ValueError(f"muse_glimmer: Duplicate parameter name {name!r}")
            pending_name = name
            value_start = token.end()
            continue

        if pending_name is None or value_start is None:
            raise ValueError("muse_glimmer: Parameter closing tag has no opening tag")
        args[pending_name] = _coerce_param_value(body[value_start : token.start()])
        pending_name = None
        value_start = None
        outside_start = token.end()

    if pending_name is not None:
        raise ValueError(f"muse_glimmer: Parameter {pending_name!r} has no closing tag")
    _validate_outside_parameter_text(body[outside_start:])
    return args


def parse_toolcalls(text: str) -> list[ToolCall]:
    # Scan for invoke blocks directly: every message on the tool channel is a
    # tool call, and each may hold one or more invokes. A block runs until
    # its closing tag or, if the model omitted it, the next invoke or the end
    # of the text.
    opens = list(_INVOKE_OPEN.finditer(text))

    raw_open_count = text.count("<atem:invoke")
    if raw_open_count != len(opens):
        raise ValueError("muse_glimmer: Malformed invoke opening tag")
    if text.count("</atem:invoke") != text.count("</atem:invoke>"):
        raise ValueError("muse_glimmer: Malformed invoke closing tag")
    if text.count("</atem:parameter") != text.count("</atem:parameter>"):
        raise ValueError("muse_glimmer: Malformed parameter closing tag")

    invoke_is_open = False
    for token in _INVOKE_TOKEN.finditer(text):
        if token.group(1) is not None:
            # A new invoke implicitly terminates the previous one. Some
            # Glimmer generations intentionally omit </atem:invoke>.
            invoke_is_open = True
        elif not invoke_is_open:
            raise ValueError("muse_glimmer: Invoke closing tag has no opening tag")
        else:
            invoke_is_open = False

    if text.count("</atem:invoke>") > len(opens):
        raise ValueError("muse_glimmer: Too many invoke closing tags")

    if "<|message|>" in text:
        for message_body in text.split("<|message|>")[1:]:
            next_message = message_body.find("<|message|>")
            if next_message != -1:
                message_body = message_body[:next_message]
            if _INVOKE_OPEN.search(message_body) is None:
                raise ValueError("muse_glimmer: Tool message contains no invoke block")

    results = []
    parsed_parameter_count = 0
    for idx, open_match in enumerate(opens):
        func_name = open_match.group(1).strip()
        if not func_name:
            raise ValueError("muse_glimmer: Invoke has no function name")
        body_start = open_match.end()
        body_end = opens[idx + 1].start() if idx + 1 < len(opens) else len(text)
        close = text.find("</atem:invoke>", body_start, body_end)
        if close != -1:
            body_end = close
        else:
            # A missing invoke closer is a documented Glimmer quirk. When a
            # later message follows, stop at its structural boundary rather
            # than treating the next message header as invoke-body garbage.
            implicit_ends = [
                pos
                for token in ("</atem:function_calls>", "<|eom|>", "<|eot|>")
                if (pos := text.find(token, body_start, body_end)) != -1
            ]
            if implicit_ends:
                body_end = min(implicit_ends)
        body = text[body_start:body_end]

        args = _parse_parameters(body)
        parsed_parameter_count += len(args)

        args_json = json.dumps(args, ensure_ascii=False)
        results.append(ToolCall(function=Tool(name=func_name, arguments=args_json)))

    raw_parameter_count = text.count("<atem:parameter")
    if raw_parameter_count != parsed_parameter_count:
        raise ValueError("muse_glimmer: Parameter tag appears outside an invoke block")
    if text.count("</atem:parameter>") != parsed_parameter_count:
        raise ValueError("muse_glimmer: Unmatched parameter closing tag")

    if not opens and "<|message|>" in text:
        xlogger.warning(
            "muse_glimmer: Tool message contains no parseable <atem:invoke> block",
            {"raw_text": text},
        )

    xlogger.debug(
        f"muse_glimmer: Parsed {len(results)} tool calls",
        {"raw_text": text, "results": results},
    )
    return results
