"""Run LiteLLM with Codex-compatible Responses stream framing.

LiteLLM's normal streaming keepalive is an SSE comment. That is sufficient for
network intermediaries, but Codex applies its idle timeout after SSE decoding,
where comments have already been discarded. This wrapper changes only
keepalive comments on ``/responses`` into a harmless ``response.in_progress``
event.

LiteLLM's Chat-Completions-to-Responses adapter can also start a response with
a reasoning item and then emit message text without first announcing the
message item/content part. The per-stream repair below inserts the two required
declarations, gives every event a monotonic sequence number, and keeps output
indexes distinct across reasoning, message, and tool items. Every non-Responses
endpoint retains LiteLLM's standard behavior.
"""

from __future__ import annotations

from copy import deepcopy
from contextlib import aclosing
from functools import wraps
import json
from typing import Any


SSE_COMMENT_KEEPALIVE = ": ping\n\n"
CODEX_RESPONSES_KEEPALIVE = 'data: {"type":"response.in_progress"}\n\n'


def _is_responses_request(request: Any) -> bool:
    path = getattr(getattr(request, "url", None), "path", "")
    return isinstance(path, str) and path.rstrip("/").endswith("/responses")


def codex_compatible_keepalive(chunk: Any, request: Any = None) -> Any:
    """Return a decoded SSE event for Codex Responses keepalives only."""

    if chunk == SSE_COMMENT_KEEPALIVE and _is_responses_request(request):
        return CODEX_RESPONSES_KEEPALIVE
    return chunk


class ResponsesFramingRepair:
    """Repair item declarations and indexes in one Responses SSE stream."""

    def __init__(self) -> None:
        self._item_indexes: dict[str, int] = {}
        self._index_owners: dict[int, str] = {}
        self._item_types: dict[str, str] = {}
        self._announced_items: set[str] = set()
        self._announced_content_parts: set[tuple[str, int]] = set()
        self._announced_reasoning_parts: set[tuple[str, int]] = set()
        self._text_by_item: dict[str, str] = {}
        self._response_snapshot: dict[str, Any] | None = None
        self._sequence_number = 0

    @staticmethod
    def _decode(chunk: Any) -> dict[str, Any] | None:
        if not isinstance(chunk, str):
            return None
        lines = chunk.splitlines()
        data_lines = [line[5:].lstrip() for line in lines if line.startswith("data:")]
        if len(data_lines) != 1 or data_lines[0] == "[DONE]":
            return None
        try:
            value = json.loads(data_lines[0])
        except (TypeError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    def _encode(self, event: dict[str, Any]) -> str:
        # LiteLLM currently omits sequence numbers on most translated events
        # and reuses some of the numbers it does emit. Stamp the complete
        # downstream sequence so inserted events cannot collide with them.
        event["sequence_number"] = self._sequence_number
        self._sequence_number += 1
        return f"data: {json.dumps(event, ensure_ascii=False, separators=(',', ':'))}\n\n"

    @staticmethod
    def _event_item_id(event: dict[str, Any]) -> str | None:
        item_id = event.get("item_id")
        if isinstance(item_id, str) and item_id:
            return item_id
        item = event.get("item")
        if isinstance(item, dict):
            item_id = item.get("id")
            if isinstance(item_id, str) and item_id:
                return item_id
        return None

    def _claim_index(self, item_id: str) -> int:
        existing = self._item_indexes.get(item_id)
        if existing is not None:
            return existing

        # Allocate by first appearance. LiteLLM can emit a first tool item at
        # index 1 even when index 0 was never declared, as well as reuse index
        # 0 for a message after a reasoning item.
        candidate = 0
        while candidate in self._index_owners:
            candidate += 1
        self._item_indexes[item_id] = candidate
        self._index_owners[candidate] = item_id
        return candidate

    @staticmethod
    def _copy_stream_metadata(source: dict[str, Any], target: dict[str, Any]) -> None:
        if "model" in source:
            target["model"] = source["model"]

    def _message_item_declaration(
        self, event: dict[str, Any], item_id: str
    ) -> list[dict[str, Any]]:
        output_index = self._claim_index(item_id)
        self._item_types[item_id] = "message"

        events: list[dict[str, Any]] = []
        if item_id not in self._announced_items:
            added = {
                "type": "response.output_item.added",
                "output_index": output_index,
                "item": {
                    "id": item_id,
                    "type": "message",
                    "status": "in_progress",
                    "role": "assistant",
                    "content": [],
                },
            }
            self._copy_stream_metadata(event, added)
            events.append(added)
            self._announced_items.add(item_id)
        return events

    def _message_declarations(
        self, event: dict[str, Any], item_id: str
    ) -> list[dict[str, Any]]:
        output_index = self._claim_index(item_id)
        events = self._message_item_declaration(event, item_id)

        part_key = (item_id, 0)
        if part_key not in self._announced_content_parts:
            part_added = {
                "type": "response.content_part.added",
                "item_id": item_id,
                "output_index": output_index,
                "content_index": 0,
                "part": {"type": "output_text", "text": "", "annotations": []},
            }
            self._copy_stream_metadata(event, part_added)
            events.append(part_added)
            self._announced_content_parts.add(part_key)
        return events

    def _reasoning_declarations(
        self, event: dict[str, Any], item_id: str
    ) -> list[dict[str, Any]]:
        output_index = self._claim_index(item_id)
        self._item_types[item_id] = "reasoning"
        try:
            summary_index = int(event.get("summary_index", 0))
        except (TypeError, ValueError):
            summary_index = 0

        events: list[dict[str, Any]] = []
        if item_id not in self._announced_items:
            item_added = {
                "type": "response.output_item.added",
                "output_index": output_index,
                "item": {
                    "id": item_id,
                    "type": "reasoning",
                    "status": "in_progress",
                    "summary": [],
                },
            }
            self._copy_stream_metadata(event, item_added)
            events.append(item_added)
            self._announced_items.add(item_id)

        part_key = (item_id, summary_index)
        if part_key not in self._announced_reasoning_parts:
            part_added = {
                "type": "response.reasoning_summary_part.added",
                "item_id": item_id,
                "output_index": output_index,
                "summary_index": summary_index,
                "part": {"type": "summary_text", "text": ""},
            }
            self._copy_stream_metadata(event, part_added)
            events.append(part_added)
            self._announced_reasoning_parts.add(part_key)
        return events

    def _complete_required_fields(self, event: dict[str, Any]) -> None:
        """Fill small required fields omitted by LiteLLM's translation."""

        event_type = event.get("type")
        if event_type in {
            "response.reasoning_summary_part.added",
            "response.reasoning_summary_part.done",
            "response.reasoning_summary_text.delta",
            "response.reasoning_summary_text.done",
        }:
            event.setdefault("summary_index", 0)
        elif event_type in {"response.output_text.delta", "response.output_text.done"}:
            event.setdefault("logprobs", [])

        response = event.get("response")
        if isinstance(response, dict):
            self._response_snapshot = deepcopy(response)
        elif event_type == "response.in_progress" and self._response_snapshot is not None:
            heartbeat_response = deepcopy(self._response_snapshot)
            heartbeat_response["status"] = "in_progress"
            event["response"] = heartbeat_response

    def process(self, chunk: Any) -> list[Any]:
        """Return zero or more correctly framed chunks for one LiteLLM chunk."""

        event = self._decode(chunk)
        if event is None:
            return [chunk]

        event_type = event.get("type")
        item_id = self._event_item_id(event)
        prefix: list[dict[str, Any]] = []

        self._complete_required_fields(event)

        if event_type == "response.output_item.added" and item_id:
            item = event.get("item")
            item_type = item.get("type") if isinstance(item, dict) else None
            output_index = self._claim_index(item_id)
            event["output_index"] = output_index
            if isinstance(item_type, str):
                self._item_types[item_id] = item_type
            if item_type == "reasoning" and isinstance(item, dict):
                item.setdefault("summary", [])
            self._announced_items.add(item_id)

        elif event_type == "response.content_part.added" and item_id:
            prefix.extend(self._message_item_declaration(event, item_id))
            output_index = self._claim_index(item_id)
            event["output_index"] = output_index
            try:
                content_index = int(event.get("content_index", 0))
            except (TypeError, ValueError):
                content_index = 0
            self._announced_content_parts.add((item_id, content_index))

        elif event_type == "response.output_text.annotation.added" and item_id:
            prefix.extend(self._message_declarations(event, item_id))
            event["output_index"] = self._item_indexes[item_id]

        elif event_type in {
            "response.reasoning_summary_text.delta",
            "response.reasoning_summary_text.done",
            "response.reasoning_summary_part.done",
        } and item_id:
            prefix.extend(self._reasoning_declarations(event, item_id))
            event["output_index"] = self._item_indexes[item_id]
            if event_type == "response.reasoning_summary_part.done":
                try:
                    summary_index = int(event.get("summary_index", 0))
                except (TypeError, ValueError):
                    summary_index = 0
                self._announced_reasoning_parts.add((item_id, summary_index))

        elif event_type == "response.output_text.delta" and item_id:
            prefix.extend(self._message_declarations(event, item_id))
            event["output_index"] = self._item_indexes[item_id]
            delta = event.get("delta")
            if isinstance(delta, str):
                self._text_by_item[item_id] = self._text_by_item.get(item_id, "") + delta

        elif event_type == "response.output_text.done" and item_id:
            prefix.extend(self._message_declarations(event, item_id))
            event["output_index"] = self._item_indexes[item_id]
            text = event.get("text")
            if isinstance(text, str):
                self._text_by_item[item_id] = text

        elif item_id:
            output_index = self._claim_index(item_id)
            event["output_index"] = output_index

            if event_type == "response.output_item.done":
                item = event.get("item")
                item_type = item.get("type") if isinstance(item, dict) else None
                if isinstance(item_type, str):
                    self._item_types[item_id] = item_type

            if (
                event_type == "response.content_part.done"
                and self._item_types.get(item_id) == "message"
            ):
                part = event.get("part")
                if not isinstance(part, dict) or part.get("type") != "output_text":
                    event["part"] = {
                        "type": "output_text",
                        "text": self._text_by_item.get(item_id, ""),
                        "annotations": [],
                    }

        return [self._encode(item) for item in (*prefix, event)]


def install_codex_heartbeat() -> None:
    """Wrap LiteLLM's outgoing stream generator once per process."""

    from litellm.proxy import proxy_server

    original = proxy_server.async_data_generator
    if getattr(original, "_zeta_codex_heartbeat", False):
        return

    @wraps(original)
    async def wrapped_async_data_generator(
        response,
        user_api_key_dict,
        request_data,
        request=None,
    ):
        framing_repair = ResponsesFramingRepair() if _is_responses_request(request) else None
        stream = original(
            response=response,
            user_api_key_dict=user_api_key_dict,
            request_data=request_data,
            request=request,
        )
        async with aclosing(stream):
            async for chunk in stream:
                chunk = codex_compatible_keepalive(chunk, request)
                repaired_chunks = framing_repair.process(chunk) if framing_repair else [chunk]
                for repaired_chunk in repaired_chunks:
                    yield repaired_chunk

    wrapped_async_data_generator._zeta_codex_heartbeat = True
    proxy_server.async_data_generator = wrapped_async_data_generator


def main() -> Any:
    install_codex_heartbeat()
    from litellm import run_server

    return run_server()


if __name__ == "__main__":
    raise SystemExit(main())
