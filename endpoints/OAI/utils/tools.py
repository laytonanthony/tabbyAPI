"""Tool call processing utilities for OAI server."""

import json
from typing import List, Optional, Sequence

from common.logger import xlogger
from endpoints.OAI.types.tools import ToolCall, ToolSpec
from endpoints.OAI.utils.toolcall_formats import (
    qwen3_coder,
    minimax_m2,
    glm4_5,
    harmony,
    hy3,
    muse_glimmer,
    deepseek_v4,
    mistral_old,
    mistral,
    gemma4,
)

ALL_TOOLCALL_FORMATS = {
    "deepseek_v4": deepseek_v4,
    "dsv4": deepseek_v4,
    "gemma4": gemma4,
    "glm4_5": glm4_5,
    "glm4_6": glm4_5,
    "glm4_7": glm4_5,
    "harmony": harmony,
    "hy3": hy3,
    "hy_v3": hy3,
    "laguna": glm4_5,
    "poolside_v1": glm4_5,
    "minimax_m2": minimax_m2,
    "minimax_m2_1": minimax_m2,
    "minimax_m2_5": minimax_m2,
    "mistral_old": mistral_old,
    "mistral": mistral,
    "muse_glimmer": muse_glimmer,
    "glimmer": muse_glimmer,
    "qwen3_coder": qwen3_coder,
    "qwen3_5": qwen3_coder,
    "step3_5": qwen3_coder,
    "step3_7": qwen3_coder,
}


class ToolCallParseError(ValueError):
    """Raised when model-emitted tool syntax cannot be handled safely."""


def _get_parser(tool_format: str):
    if not tool_format:
        return None
    parser = ALL_TOOLCALL_FORMATS.get(tool_format)
    if not parser:
        xlogger.error(f"Unknown tool format given: {tool_format}")
    return parser


def get_toolcall_tags(tool_format: str):
    parser = _get_parser(tool_format)
    if not parser:
        return None, None
    return parser.TOOLCALL_START, parser.TOOLCALL_END


def is_supported_format(tool_format: str) -> bool:
    return tool_format in ALL_TOOLCALL_FORMATS


def _validate_toolcalls(
    parsed: Sequence[ToolCall],
    tool_specs: Optional[Sequence[ToolSpec]],
) -> None:
    """Validate the common invariants required before a tool can be executed.

    Parsers are deliberately format-specific, but the API must never emit an
    empty, unknown, or structurally invalid call as a successful tool turn.
    Validate the full batch atomically so retrying cannot duplicate a valid
    subset of parallel calls.
    """

    specs_by_name = (
        {spec.function.name: spec.function.parameters for spec in tool_specs}
        if tool_specs is not None
        else None
    )
    call_ids = set()

    for index, call in enumerate(parsed):
        call_id = call.id.strip() if isinstance(call.id, str) else ""
        if not call_id:
            raise ToolCallParseError(f"tool call {index} has no call id")
        if call_id in call_ids:
            raise ToolCallParseError(f"tool call {index} repeats call id {call_id!r}")
        call_ids.add(call_id)

        name = call.function.name.strip() if isinstance(call.function.name, str) else ""
        if not name:
            raise ToolCallParseError(f"tool call {index} has no function name")

        try:
            arguments = json.loads(call.function.arguments)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ToolCallParseError(
                f"tool call {index} ({name}) has invalid JSON arguments"
            ) from exc
        if not isinstance(arguments, dict):
            raise ToolCallParseError(
                f"tool call {index} ({name}) arguments must be a JSON object"
            )

        if specs_by_name is None:
            continue
        if name not in specs_by_name:
            raise ToolCallParseError(f"tool call {index} names unknown function {name!r}")

        schema = specs_by_name[name]
        if not isinstance(schema, dict):
            continue

        required = schema.get("required", [])
        if isinstance(required, list):
            missing = [key for key in required if key not in arguments]
            if missing:
                raise ToolCallParseError(
                    f"tool call {index} ({name}) is missing required arguments: "
                    + ", ".join(map(str, missing))
                )

        properties = schema.get("properties")
        if schema.get("additionalProperties") is False and isinstance(properties, dict):
            unexpected = [key for key in arguments if key not in properties]
            if unexpected:
                raise ToolCallParseError(
                    f"tool call {index} ({name}) has unexpected arguments: "
                    + ", ".join(map(str, unexpected))
                )


def parse_toolcalls(
    tool_calls_str: str,
    tool_format: str,
    tool_specs: Optional[Sequence[ToolSpec]] = None,
) -> List[ToolCall]:
    """
    Dispatch tool call parsing to the appropriate format handler.

    Args:
        tool_calls_str: Raw tool call text from model generation.
        tool_format: See below

    Returns:
        A validated list of parsed ToolCall objects. Blank input returns an
        empty list. Nonblank malformed input raises ToolCallParseError so the
        caller can retry instead of reporting a false successful completion.
    """

    if not tool_calls_str or not tool_calls_str.strip():
        return []

    try:
        parser = _get_parser(tool_format)
        if not parser:
            raise ToolCallParseError(f"unknown tool format {tool_format!r}")

        parsed = parser.parse_toolcalls(tool_calls_str)
        if not parsed:
            raise ToolCallParseError(
                f"nonblank {tool_format!r} tool output produced no tool calls"
            )
        _validate_toolcalls(parsed, tool_specs)
        return list(parsed)

    except Exception as e:
        error = e if isinstance(e, ToolCallParseError) else ToolCallParseError(str(e))
        xlogger.error(
            "ToolCallProcessor.parse: Failed to parse tool calls",
            {"tool_format": tool_format, "e": str(e)},
            details=f"(format={tool_format}): {e}",
        )
        if error is e:
            raise
        raise error from e
