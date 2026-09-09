import asyncio
import json
import pathlib
import unittest
from unittest.mock import patch

from endpoints.OAI.types.chat_completion import ChatCompletionRequest
from endpoints.OAI.types.tools import Function, ToolSpec
from endpoints.OAI.utils import chat_completion


MALFORMED = "<tool_call>run<arg_key>cmd</arg_key></tool_call>"
VALID = (
    "<tool_call>run"
    "<arg_key>cmd</arg_key><arg_value>dir</arg_value>"
    "</tool_call>"
)
PSEUDO_SEARCH = (
    "On it — searching now.\n\n"
    'To: zeta_search/web_search\n{"query":"GPU Spain","num_results":8}'
)


def request_with_tool(max_tokens=None):
    return ChatCompletionRequest(
        messages=[{"role": "user", "content": "run dir"}],
        max_tokens=max_tokens,
        tool_choice="auto",
        tools=[
            ToolSpec(
                type="function",
                function=Function(
                    name="run",
                    description="run a command",
                    parameters={
                        "type": "object",
                        "properties": {"cmd": {"type": "string"}},
                        "required": ["cmd"],
                        "additionalProperties": False,
                    },
                ),
            )
        ],
    )


def request_with_search_tool(tool_choice="auto"):
    return ChatCompletionRequest(
        messages=[{"role": "user", "content": "search for a GPU"}],
        tool_choice=tool_choice,
        tools=[
            ToolSpec(
                type="function",
                function=Function(
                    name="zeta_search__web_search",
                    description="search the web",
                    parameters={
                        "type": "object",
                        "properties": {
                            "query": {"type": "string"},
                            "count": {"type": "integer"},
                        },
                        "required": ["query"],
                        "additionalProperties": False,
                    },
                ),
            )
        ],
    )


class DummyDisconnectHandler:
    def __init__(self):
        self.polls = 0

    async def poll(self):
        self.polls += 1


class DummyContainer:
    harmony = False
    muse_glimmer = False
    tool_format = "glm4_5"
    reasoning = False
    reasoning_start_token = None
    reasoning_end_token = None
    tool_calls_in_reasoning = True
    reasoning_budget_tokens = None
    reasoning_budget_message = ""
    model_dir = pathlib.Path("dummy-model")

    def __init__(self, attempts):
        self.attempts = list(attempts)
        self.request_ids = []
        self.max_tokens = []
        self.closed = []

    def stream_generate(
        self,
        request_id,
        _prompt,
        _params,
        _disconnect_handler,
        _mm_embeddings,
        filter_trigger=None,
        label=None,
    ):
        self.request_ids.append(request_id)
        self.max_tokens.append(_params.max_tokens)
        chunks = self.attempts[len(self.request_ids) - 1]
        closed = self.closed

        async def generate():
            try:
                for chunk in chunks:
                    yield dict(chunk)
            finally:
                closed.append(request_id)

        return generate()

    def constrain_generation_output(self, _request_id, _text):
        return True


def terminal(text, finish_reason="stop"):
    return {
        "text": text,
        "finish_reason": finish_reason,
        "token_ids": [1],
        "prompt_tokens": 5,
        "generated_tokens": 1,
    }


class ToolCallRetryTests(unittest.IsolatedAsyncioTestCase):
    async def collect(self, container, *, streaming):
        queue = asyncio.Queue() if streaming else None
        disconnect = DummyDisconnectHandler()
        with (
            patch.object(chat_completion.model, "container", container),
            patch.object(chat_completion.zeta_metrics, "request_started"),
            patch.object(chat_completion.zeta_metrics, "observe_generation"),
            patch.object(chat_completion.zeta_metrics, "request_finished"),
        ):
            result = await chat_completion._chat_stream_collector(
                0,
                queue,
                "request-1",
                "prompt",
                request_with_tool(),
                False,
                streaming_mode=streaming,
                disconnect_handler=disconnect,
            )

        queued = []
        if queue is not None:
            while not queue.empty():
                queued.append(queue.get_nowait())
        return result, queued, disconnect

    async def test_nonstream_retries_malformed_then_returns_valid_call(self):
        container = DummyContainer([[terminal(MALFORMED)], [terminal(VALID)]])
        result, queued, disconnect = await self.collect(container, streaming=False)

        self.assertEqual(queued, [])
        self.assertEqual(result["finish_reason"], "tool_calls")
        self.assertEqual(result["tool_calls"][0]["function"]["name"], "run")
        self.assertEqual(
            container.request_ids, ["request-1", "request-1-toolretry1"]
        )
        self.assertEqual(
            container.max_tokens, [None, chat_completion.TOOL_CALL_RETRY_MAX_TOKENS]
        )
        self.assertEqual(container.closed, container.request_ids)
        self.assertEqual(disconnect.polls, 1)

    async def test_retry_preserves_smaller_client_token_limit(self):
        container = DummyContainer([[terminal(MALFORMED)], [terminal(VALID)]])
        queue = asyncio.Queue()
        disconnect = DummyDisconnectHandler()
        with (
            patch.object(chat_completion.model, "container", container),
            patch.object(chat_completion.zeta_metrics, "request_started"),
            patch.object(chat_completion.zeta_metrics, "observe_generation"),
            patch.object(chat_completion.zeta_metrics, "request_finished"),
        ):
            await chat_completion._chat_stream_collector(
                0,
                queue,
                "request-1",
                "prompt",
                request_with_tool(max_tokens=512),
                False,
                streaming_mode=True,
                disconnect_handler=disconnect,
            )

        self.assertEqual(container.max_tokens, [512, 512])

    async def test_stream_retries_before_false_terminal_is_emitted(self):
        container = DummyContainer([[terminal(MALFORMED)], [terminal(VALID)]])
        result, queued, disconnect = await self.collect(container, streaming=True)

        self.assertIsNone(result)
        self.assertEqual(len(queued), 1)
        self.assertNotIsInstance(queued[0], Exception)
        self.assertEqual(queued[0]["finish_reason"], "tool_calls")
        self.assertEqual(queued[0]["delta_tool_calls"][0]["function"]["name"], "run")
        self.assertEqual(
            container.request_ids, ["request-1", "request-1-toolretry1"]
        )
        self.assertEqual(container.closed, container.request_ids)
        self.assertEqual(disconnect.polls, 1)

    async def test_repeated_malformed_output_is_bounded_and_closes_normally(self):
        container = DummyContainer([[terminal(MALFORMED)], [terminal(MALFORMED)]])
        _result, queued, _disconnect = await self.collect(container, streaming=True)

        self.assertEqual(len(container.request_ids), 2)
        self.assertEqual(len(queued), 1)
        self.assertNotIsInstance(queued[0], Exception)
        self.assertIn("couldn't complete", queued[0]["delta_content"])
        self.assertEqual(queued[0]["finish_reason"], "stop")
        self.assertEqual(container.closed, container.request_ids)

    async def test_content_before_malformed_call_retries_without_aborting_stream(self):
        container = DummyContainer(
            [
                [terminal("answer in progress", None), terminal(MALFORMED)],
                [terminal(VALID)],
            ]
        )
        _result, queued, _disconnect = await self.collect(container, streaming=True)

        self.assertEqual(
            container.request_ids, ["request-1", "request-1-toolretry1"]
        )
        self.assertEqual(queued[0]["delta_content"], "answer in progress")
        self.assertFalse(any(isinstance(item, Exception) for item in queued))
        self.assertEqual(queued[-1]["finish_reason"], "tool_calls")
        self.assertEqual(
            queued[-1]["delta_tool_calls"][0]["function"]["name"], "run"
        )
        self.assertEqual(container.closed, container.request_ids)

    async def test_retry_that_abandons_tool_call_closes_stream_normally(self):
        container = DummyContainer(
            [[terminal(MALFORMED)], [terminal("unrelated answer")]]
        )
        _result, queued, _disconnect = await self.collect(container, streaming=True)

        self.assertEqual(len(queued), 1)
        self.assertNotIsInstance(queued[0], Exception)
        self.assertEqual(queued[0]["delta_content"], "unrelated answer")
        self.assertEqual(queued[0]["finish_reason"], "stop")

    async def test_repeated_malformed_output_after_content_closes_normally(self):
        container = DummyContainer(
            [
                [terminal("answer in progress", None), terminal(MALFORMED)],
                [terminal(MALFORMED)],
            ]
        )
        _result, queued, _disconnect = await self.collect(container, streaming=True)

        self.assertEqual(len(container.request_ids), 2)
        self.assertFalse(any(isinstance(item, Exception) for item in queued))
        self.assertEqual(queued[0]["delta_content"], "answer in progress")
        self.assertEqual(queued[-1]["delta_content"], "")
        self.assertEqual(queued[-1]["finish_reason"], "stop")

    async def test_stream_promotes_valid_zeta_search_pseudo_call(self):
        container = DummyContainer([[terminal(PSEUDO_SEARCH)]])
        queue = asyncio.Queue()
        with (
            patch.object(chat_completion.model, "container", container),
            patch.object(chat_completion.zeta_metrics, "request_started"),
            patch.object(chat_completion.zeta_metrics, "observe_generation"),
            patch.object(chat_completion.zeta_metrics, "request_finished"),
        ):
            await chat_completion._chat_stream_collector(
                0,
                queue,
                "request-1",
                "prompt",
                request_with_search_tool(),
                False,
                streaming_mode=True,
            )

        queued = []
        while not queue.empty():
            queued.append(queue.get_nowait())
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0]["delta_content"], "On it — searching now.")
        self.assertEqual(queued[0]["finish_reason"], "tool_calls")
        call = queued[0]["delta_tool_calls"][0]
        self.assertEqual(call["function"]["name"], "zeta_search__web_search")
        self.assertEqual(
            json.loads(call["function"]["arguments"]),
            {"query": "GPU Spain", "count": 8},
        )

    async def test_stream_promotes_pseudo_call_split_across_chunks(self):
        container = DummyContainer(
            [
                [
                    terminal("On it — searching now.\n\nTo: zeta_", None),
                    terminal(
                        'search/web_search\n{"query":"GPU Spain","num_results":8}'
                    ),
                ]
            ]
        )
        queue = asyncio.Queue()
        with (
            patch.object(chat_completion.model, "container", container),
            patch.object(chat_completion.zeta_metrics, "request_started"),
            patch.object(chat_completion.zeta_metrics, "observe_generation"),
            patch.object(chat_completion.zeta_metrics, "request_finished"),
        ):
            await chat_completion._chat_stream_collector(
                0,
                queue,
                "request-1",
                "prompt",
                request_with_search_tool(),
                False,
                streaming_mode=True,
            )

        queued = []
        while not queue.empty():
            queued.append(queue.get_nowait())
        self.assertNotIn(
            "To: zeta_search/web_search",
            "".join(item["delta_content"] for item in queued),
        )
        self.assertEqual(queued[-1]["finish_reason"], "tool_calls")
        self.assertEqual(
            queued[-1]["delta_tool_calls"][0]["function"]["name"],
            "zeta_search__web_search",
        )

    async def test_stream_buffer_preserves_non_candidate_content(self):
        original = "Send this\nTo: Anthony\nwhen you are ready."
        container = DummyContainer(
            [[terminal("Send this\nTo:", None), terminal(" Anthony\nwhen you are ready.")]]
        )
        queue = asyncio.Queue()
        with (
            patch.object(chat_completion.model, "container", container),
            patch.object(chat_completion.zeta_metrics, "request_started"),
            patch.object(chat_completion.zeta_metrics, "observe_generation"),
            patch.object(chat_completion.zeta_metrics, "request_finished"),
        ):
            await chat_completion._chat_stream_collector(
                0,
                queue,
                "request-1",
                "prompt",
                request_with_search_tool(),
                False,
                streaming_mode=True,
            )

        queued = []
        while not queue.empty():
            queued.append(queue.get_nowait())
        self.assertEqual("".join(item["delta_content"] for item in queued), original)
        self.assertEqual(queued[-1]["finish_reason"], "stop")

    async def test_nonstream_promotes_valid_zeta_search_pseudo_call(self):
        container = DummyContainer([[terminal(PSEUDO_SEARCH)]])
        with (
            patch.object(chat_completion.model, "container", container),
            patch.object(chat_completion.zeta_metrics, "request_started"),
            patch.object(chat_completion.zeta_metrics, "observe_generation"),
            patch.object(chat_completion.zeta_metrics, "request_finished"),
        ):
            result = await chat_completion._chat_stream_collector(
                0,
                None,
                "request-1",
                "prompt",
                request_with_search_tool(),
                False,
                streaming_mode=False,
            )

        self.assertEqual(result["finish_reason"], "tool_calls")
        self.assertEqual(
            result["tool_calls"][0]["function"]["name"],
            "zeta_search__web_search",
        )

    async def test_tool_choice_none_leaves_pseudo_call_as_content(self):
        container = DummyContainer([[terminal(PSEUDO_SEARCH)]])
        queue = asyncio.Queue()
        with (
            patch.object(chat_completion.model, "container", container),
            patch.object(chat_completion.zeta_metrics, "request_started"),
            patch.object(chat_completion.zeta_metrics, "observe_generation"),
            patch.object(chat_completion.zeta_metrics, "request_finished"),
        ):
            await chat_completion._chat_stream_collector(
                0,
                queue,
                "request-1",
                "prompt",
                request_with_search_tool(tool_choice="none"),
                False,
                streaming_mode=True,
            )

        queued = []
        while not queue.empty():
            queued.append(queue.get_nowait())
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0]["delta_content"], PSEUDO_SEARCH)
        self.assertEqual(queued[0]["delta_tool_calls"], [])
        self.assertEqual(queued[0]["finish_reason"], "stop")


if __name__ == "__main__":
    unittest.main()
