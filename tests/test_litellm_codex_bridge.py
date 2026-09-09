import json
import pathlib
import sys
import unittest


sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "tools"))

from litellm_codex_bridge import ResponsesFramingRepair  # noqa: E402


def sse(event):
    return f"data: {json.dumps(event, separators=(',', ':'))}\n\n"


def decoded(chunks):
    return [json.loads(chunk.removeprefix("data: ").strip()) for chunk in chunks]


class ResponsesFramingRepairTests(unittest.TestCase):
    def test_reasoning_then_text_declares_message_before_delta(self):
        repair = ResponsesFramingRepair()
        reasoning_added = {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {"id": "rs_1", "type": "reasoning", "status": "in_progress"},
        }
        reasoning_done = {
            "type": "response.output_item.done",
            "output_index": 0,
            "item": {"id": "rs_1", "type": "reasoning", "status": "completed"},
        }
        delta = {
            "type": "response.output_text.delta",
            "item_id": "msg_1",
            "output_index": 0,
            "content_index": 0,
            "delta": "hello",
            "model": "local-model",
        }

        self.assertEqual(len(repair.process(sse(reasoning_added))), 1)
        self.assertEqual(len(repair.process(sse(reasoning_done))), 1)
        events = decoded(repair.process(sse(delta)))

        self.assertEqual(
            [event["type"] for event in events],
            [
                "response.output_item.added",
                "response.content_part.added",
                "response.output_text.delta",
            ],
        )
        self.assertTrue(all(event["output_index"] == 1 for event in events))
        self.assertEqual(events[0]["item"]["id"], "msg_1")
        self.assertEqual(events[1]["part"]["type"], "output_text")
        self.assertEqual(events[0]["model"], "local-model")
        self.assertEqual([event["sequence_number"] for event in events], [2, 3, 4])

    def test_reasoning_delta_declares_summary_part_first(self):
        repair = ResponsesFramingRepair()
        events = decoded(
            repair.process(
                sse(
                    {
                        "type": "response.reasoning_summary_text.delta",
                        "item_id": "rs_1",
                        "output_index": 0,
                        "delta": "thinking",
                    }
                )
            )
        )

        self.assertEqual(
            [event["type"] for event in events],
            [
                "response.output_item.added",
                "response.reasoning_summary_part.added",
                "response.reasoning_summary_text.delta",
            ],
        )
        self.assertEqual(events[0]["item"]["summary"], [])
        self.assertEqual(events[1]["part"]["type"], "summary_text")
        self.assertEqual([event["sequence_number"] for event in events], [0, 1, 2])

    def test_orphan_content_part_declares_message_item_first(self):
        repair = ResponsesFramingRepair()
        events = decoded(
            repair.process(
                sse(
                    {
                        "type": "response.content_part.added",
                        "item_id": "msg_1",
                        "output_index": 0,
                        "content_index": 0,
                        "part": {"type": "output_text", "text": ""},
                    }
                )
            )
        )

        self.assertEqual(
            [event["type"] for event in events],
            ["response.output_item.added", "response.content_part.added"],
        )
        self.assertEqual(events[0]["item"]["id"], "msg_1")

    def test_message_done_events_keep_index_and_output_text_part(self):
        repair = ResponsesFramingRepair()
        repair.process(
            sse(
                {
                    "type": "response.output_item.added",
                    "output_index": 0,
                    "item": {"id": "rs_1", "type": "reasoning"},
                }
            )
        )
        repair.process(
            sse(
                {
                    "type": "response.output_text.delta",
                    "item_id": "msg_1",
                    "output_index": 0,
                    "content_index": 0,
                    "delta": "hello",
                }
            )
        )

        text_done = decoded(
            repair.process(
                sse(
                    {
                        "type": "response.output_text.done",
                        "item_id": "msg_1",
                        "output_index": 0,
                        "content_index": 0,
                        "text": "hello world",
                    }
                )
            )
        )[-1]
        part_done = decoded(
            repair.process(
                sse(
                    {
                        "type": "response.content_part.done",
                        "item_id": "msg_1",
                        "output_index": 0,
                        "content_index": 0,
                        "part": {"type": "reasoning_text", "reasoning": "hidden"},
                    }
                )
            )
        )[-1]
        item_done = decoded(
            repair.process(
                sse(
                    {
                        "type": "response.output_item.done",
                        "output_index": 0,
                        "item": {"id": "msg_1", "type": "message", "content": []},
                    }
                )
            )
        )[-1]

        self.assertEqual(text_done["output_index"], 1)
        self.assertEqual(part_done["output_index"], 1)
        self.assertEqual(part_done["part"]["type"], "output_text")
        self.assertEqual(part_done["part"]["text"], "hello world")
        self.assertEqual(item_done["output_index"], 1)
        self.assertEqual(text_done["logprobs"], [])

    def test_text_only_response_uses_zero_without_duplicate_declarations(self):
        repair = ResponsesFramingRepair()
        first = decoded(
            repair.process(
                sse(
                    {
                        "type": "response.output_text.delta",
                        "item_id": "msg_1",
                        "output_index": 0,
                        "content_index": 0,
                        "delta": "a",
                    }
                )
            )
        )
        second = decoded(
            repair.process(
                sse(
                    {
                        "type": "response.output_text.delta",
                        "item_id": "msg_1",
                        "output_index": 0,
                        "content_index": 0,
                        "delta": "b",
                    }
                )
            )
        )

        self.assertEqual(len(first), 3)
        self.assertTrue(all(event["output_index"] == 0 for event in first))
        self.assertEqual(len(second), 1)
        self.assertEqual(second[0]["delta"], "b")

    def test_tool_index_is_remapped_after_reasoning_and_message(self):
        repair = ResponsesFramingRepair()
        repair.process(
            sse(
                {
                    "type": "response.output_item.added",
                    "output_index": 0,
                    "item": {"id": "rs_1", "type": "reasoning"},
                }
            )
        )
        repair.process(
            sse(
                {
                    "type": "response.output_text.delta",
                    "item_id": "msg_1",
                    "output_index": 0,
                    "content_index": 0,
                    "delta": "checking",
                }
            )
        )
        tool_added = decoded(
            repair.process(
                sse(
                    {
                        "type": "response.output_item.added",
                        "output_index": 1,
                        "item": {"id": "call_1", "type": "function_call", "name": "search"},
                    }
                )
            )
        )[0]
        args_delta = decoded(
            repair.process(
                sse(
                    {
                        "type": "response.function_call_arguments.delta",
                        "item_id": "call_1",
                        "output_index": 1,
                        "delta": "{}",
                    }
                )
            )
        )[0]

        self.assertEqual(tool_added["output_index"], 2)
        self.assertEqual(args_delta["output_index"], 2)

    def test_first_tool_item_is_compacted_to_index_zero(self):
        repair = ResponsesFramingRepair()
        tool_added = decoded(
            repair.process(
                sse(
                    {
                        "type": "response.output_item.added",
                        "output_index": 1,
                        "item": {
                            "id": "call_1",
                            "type": "function_call",
                            "name": "search",
                        },
                    }
                )
            )
        )[0]

        self.assertEqual(tool_added["output_index"], 0)

    def test_all_json_events_receive_monotonic_sequence_numbers(self):
        repair = ResponsesFramingRepair()
        created = decoded(
            repair.process(
                sse(
                    {
                        "type": "response.created",
                        "sequence_number": 99,
                        "response": {"id": "resp_1", "status": "in_progress"},
                    }
                )
            )
        )[0]
        heartbeat = decoded(
            repair.process(sse({"type": "response.in_progress"}))
        )[0]
        delta_events = decoded(
            repair.process(
                sse(
                    {
                        "type": "response.output_text.delta",
                        "item_id": "msg_1",
                        "output_index": 0,
                        "content_index": 0,
                        "delta": "hello",
                    }
                )
            )
        )

        events = [created, heartbeat, *delta_events]
        self.assertEqual(
            [event["sequence_number"] for event in events], list(range(len(events)))
        )
        self.assertEqual(heartbeat["response"]["id"], "resp_1")
        self.assertEqual(delta_events[-1]["logprobs"], [])

    def test_non_json_and_done_chunks_pass_through(self):
        repair = ResponsesFramingRepair()
        self.assertEqual(repair.process(": ping\n\n"), [": ping\n\n"])
        self.assertEqual(repair.process("data: [DONE]\n\n"), ["data: [DONE]\n\n"])


if __name__ == "__main__":
    unittest.main()
