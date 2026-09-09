import json
import unittest

from endpoints.OAI.types.tools import Function, ToolSpec
from endpoints.OAI.utils.tools import (
    ToolCallParseError,
    parse_toolcalls,
    parse_zeta_search_pseudo_toolcall,
)


def tool_spec(name="f", required=None, additional_properties=False):
    properties = {key: {"type": "string"} for key in (required or [])}
    return ToolSpec(
        type="function",
        function=Function(
            name=name,
            description="test tool",
            parameters={
                "type": "object",
                "properties": properties,
                "required": required or [],
                "additionalProperties": additional_properties,
            },
        ),
    )


def search_tool_spec():
    return ToolSpec(
        type="function",
        function=Function(
            name="zeta_search__web_search",
            description="search the web",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "count": {"type": "integer", "minimum": 1, "maximum": 10},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        ),
    )


class SharedToolCallValidationTests(unittest.TestCase):
    def test_blank_tool_channel_is_not_an_error(self):
        self.assertEqual(parse_toolcalls("  \n", "glm4_5"), [])

    def test_nonblank_output_with_no_calls_is_rejected(self):
        with self.assertRaises(ToolCallParseError):
            parse_toolcalls("<tool_call>broken", "glm4_5")

    def test_parser_exception_is_rejected(self):
        with self.assertRaises(ToolCallParseError):
            parse_toolcalls(
                "<tool_call>f<arg_key>cmd</arg_key></tool_call>",
                "glm4_5",
            )

    def test_invalid_json_arguments_are_rejected(self):
        malformed_harmony = (
            "<|channel|>commentary to=functions.f <|constrain|>json"
            '<|message|>{"cmd":<|call|>'
        )
        with self.assertRaises(ToolCallParseError):
            parse_toolcalls(malformed_harmony, "harmony")

    def test_non_object_arguments_are_rejected(self):
        malformed_mistral = '[TOOL_CALLS][{"name":"f","arguments":["x"]}]'
        with self.assertRaises(ToolCallParseError):
            parse_toolcalls(malformed_mistral, "mistral_old")

    def test_unknown_function_is_rejected_against_request_tools(self):
        raw = "<tool_call>other</tool_call>"
        with self.assertRaises(ToolCallParseError):
            parse_toolcalls(raw, "glm4_5", [tool_spec("f")])

    def test_missing_required_argument_is_rejected(self):
        raw = "<tool_call>f</tool_call>"
        with self.assertRaises(ToolCallParseError):
            parse_toolcalls(raw, "glm4_5", [tool_spec("f", ["cmd"])])

    def test_unexpected_argument_is_rejected_for_strict_schema(self):
        raw = (
            "<tool_call>f"
            "<arg_key>extra</arg_key><arg_value>x</arg_value>"
            "</tool_call>"
        )
        with self.assertRaises(ToolCallParseError):
            parse_toolcalls(raw, "glm4_5", [tool_spec("f")])

    def test_valid_no_argument_call_is_allowed(self):
        raw = "<tool_call>f</tool_call>"
        calls = parse_toolcalls(raw, "glm4_5", [tool_spec("f")])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].function.arguments, "{}")

    def test_valid_required_argument_is_allowed(self):
        raw = (
            "<tool_call>f"
            "<arg_key>cmd</arg_key><arg_value>dir</arg_value>"
            "</tool_call>"
        )
        calls = parse_toolcalls(raw, "glm4_5", [tool_spec("f", ["cmd"])])
        self.assertEqual(calls[0].function.arguments, '{"cmd": "dir"}')

    def test_parallel_batch_is_rejected_atomically(self):
        raw = (
            "<tool_call>f<arg_key>cmd</arg_key><arg_value>dir</arg_value></tool_call>"
            "<tool_call>f<arg_key>cmd</arg_key></tool_call>"
        )
        with self.assertRaises(ToolCallParseError):
            parse_toolcalls(raw, "glm4_5", [tool_spec("f", ["cmd"])])

    def test_promotes_zeta_search_pseudo_call_and_maps_count_alias(self):
        raw = (
            "On it — searching now.\n\n"
            'To: zeta_search/web_search\n{"query":"GPU Spain","num_results":8}'
        )
        calls = parse_zeta_search_pseudo_toolcall(raw, [search_tool_spec()])

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].function.name, "zeta_search__web_search")
        self.assertEqual(
            json.loads(calls[0].function.arguments),
            {"query": "GPU Spain", "count": 8},
        )

    def test_pseudo_call_requires_exact_delivered_search_tool(self):
        raw = 'To: zeta_search/web_search\n{"query":"GPU Spain"}'
        with self.assertRaises(ToolCallParseError):
            parse_zeta_search_pseudo_toolcall(raw, [tool_spec("web_search")])

    def test_pseudo_call_rejects_duplicate_or_ambiguous_count(self):
        duplicate = (
            'To: zeta_search/web_search\n'
            '{"query":"GPU","query":"CPU"}'
        )
        both_counts = (
            'To: zeta_search/web_search\n'
            '{"query":"GPU","count":5,"num_results":8}'
        )
        with self.assertRaises(ToolCallParseError):
            parse_zeta_search_pseudo_toolcall(duplicate, [search_tool_spec()])
        with self.assertRaises(ToolCallParseError):
            parse_zeta_search_pseudo_toolcall(both_counts, [search_tool_spec()])

    def test_pseudo_call_enforces_search_argument_types_and_range(self):
        wrong_type = (
            'To: zeta_search/web_search\n{"query":"GPU","count":"eight"}'
        )
        out_of_range = 'To: zeta_search/web_search\n{"query":"GPU","count":11}'

        with self.assertRaises(ToolCallParseError):
            parse_zeta_search_pseudo_toolcall(wrong_type, [search_tool_spec()])
        with self.assertRaises(ToolCallParseError):
            parse_zeta_search_pseudo_toolcall(out_of_range, [search_tool_spec()])

    def test_pseudo_call_enforces_required_and_additional_properties(self):
        missing_query = 'To: zeta_search/web_search\n{"count":8}'
        extra_argument = (
            'To: zeta_search/web_search\n{"query":"GPU","country":"Spain"}'
        )

        with self.assertRaises(ToolCallParseError):
            parse_zeta_search_pseudo_toolcall(missing_query, [search_tool_spec()])
        with self.assertRaises(ToolCallParseError):
            parse_zeta_search_pseudo_toolcall(extra_argument, [search_tool_spec()])

    def test_embedded_or_fenced_pseudo_call_remains_content(self):
        embedded = (
            'Here is an example: To: zeta_search/web_search\n{"query":"GPU"}'
        )
        newline_example = (
            "Here is an example:\n"
            'To: zeta_search/web_search\n{"query":"GPU"}'
        )
        search_example = (
            "Example of searching:\n"
            'To: zeta_search/web_search\n{"query":"GPU"}'
        )
        fenced = '```\nTo: zeta_search/web_search\n{"query":"GPU"}\n```'

        self.assertIsNone(
            parse_zeta_search_pseudo_toolcall(embedded, [search_tool_spec()])
        )
        self.assertIsNone(
            parse_zeta_search_pseudo_toolcall(newline_example, [search_tool_spec()])
        )
        self.assertIsNone(
            parse_zeta_search_pseudo_toolcall(search_example, [search_tool_spec()])
        )
        self.assertIsNone(
            parse_zeta_search_pseudo_toolcall(fenced, [search_tool_spec()])
        )


if __name__ == "__main__":
    unittest.main()
