import json
import unittest

from endpoints.OAI.types.tools import Function, ToolSpec
from endpoints.OAI.utils.tools import (
    ToolCallParseError,
    is_zeta_search_glm_intent,
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
                    "query": {"type": "string", "minLength": 1},
                    "count": {"type": "integer", "minimum": 1, "maximum": 10},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        ),
    )


class SharedToolCallValidationTests(unittest.TestCase):
    def test_zeta_search_intent_recognizes_malformed_first_argument_tag(self):
        malformed = (
            "<tool_call>zeta_search__web_search" "<arg_value>GPU Spain</arg_value>"
        )

        self.assertTrue(
            is_zeta_search_glm_intent(
                malformed,
                "glm4_5",
                [search_tool_spec()],
            )
        )

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

    def test_recovers_unclosed_native_zeta_search_call(self):
        raw = (
            "<tool_call>zeta_search__web_search"
            "<arg_key>query</arg_key><arg_value>GPU Spain</arg_value>"
            "<arg_key>count</arg_key><arg_value>8</arg_value>"
        )

        calls = parse_toolcalls(raw, "glm4_5", [search_tool_spec()])

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].function.name, "zeta_search__web_search")
        self.assertEqual(
            json.loads(calls[0].function.arguments),
            {"query": "GPU Spain", "count": 8},
        )

    def test_recovers_unclosed_slash_recipient_and_count_alias(self):
        raw = (
            "<tool_call>zeta_search/web_search"
            "<arg_key>query</arg_key><arg_value>GPU Spain</arg_value>"
            "<arg_key>num_results</arg_key><arg_value>8</arg_value>"
        )

        calls = parse_toolcalls(raw, "glm4_5", [search_tool_spec()])

        self.assertEqual(calls[0].function.name, "zeta_search__web_search")
        self.assertEqual(
            json.loads(calls[0].function.arguments),
            {"query": "GPU Spain", "count": 8},
        )

    def test_recovers_unclosed_search_without_optional_count(self):
        raw = (
            "<tool_call>zeta_search__web_search"
            "<arg_key>query</arg_key><arg_value>GPU Spain</arg_value>"
        )

        calls = parse_toolcalls(raw, "glm4_5", [search_tool_spec()])

        self.assertEqual(
            json.loads(calls[0].function.arguments),
            {"query": "GPU Spain"},
        )

    def test_balanced_native_zeta_call_is_canonicalized_and_strictly_validated(self):
        alias = (
            "<tool_call>zeta_search/web_search"
            "<arg_key>query</arg_key><arg_value>GPU Spain</arg_value>"
            "<arg_key>num_results</arg_key><arg_value>10</arg_value>"
            "</tool_call>"
        )
        invalid_count = (
            "<tool_call>zeta_search__web_search"
            "<arg_key>query</arg_key><arg_value>GPU Spain</arg_value>"
            "<arg_key>count</arg_key><arg_value>true</arg_value>"
            "</tool_call>"
        )

        calls = parse_toolcalls(alias, "glm4_5", [search_tool_spec()])

        self.assertEqual(calls[0].function.name, "zeta_search__web_search")
        self.assertEqual(
            json.loads(calls[0].function.arguments),
            {"query": "GPU Spain", "count": 10},
        )
        with self.assertRaises(ToolCallParseError):
            parse_toolcalls(invalid_count, "glm4_5", [search_tool_spec()])

    def test_unclosed_search_recovery_rejects_incomplete_or_extra_markup(self):
        incomplete = (
            "<tool_call>zeta_search__web_search"
            "<arg_key>query</arg_key><arg_value>GPU Spain"
        )
        trailing = (
            "<tool_call>zeta_search__web_search"
            "<arg_key>query</arg_key><arg_value>GPU Spain</arg_value>oops"
        )
        parallel = (
            "<tool_call>zeta_search__web_search"
            "<arg_key>query</arg_key><arg_value>GPU Spain</arg_value>"
            "<tool_call>zeta_search__web_search"
        )

        for raw in (incomplete, trailing, parallel):
            with self.subTest(raw=raw), self.assertRaises(ToolCallParseError):
                parse_toolcalls(raw, "glm4_5", [search_tool_spec()])

    def test_glm_parser_never_accepts_a_valid_prefix_of_unclosed_batch(self):
        valid_search = (
            "<tool_call>zeta_search__web_search"
            "<arg_key>query</arg_key><arg_value>GPU Spain</arg_value>"
            "</tool_call>"
        )
        unclosed_search = (
            "<tool_call>zeta_search__web_search"
            "<arg_key>query</arg_key><arg_value>CPU Spain</arg_value>"
        )
        valid_other = "<tool_call>f</tool_call>"

        with self.assertRaises(ToolCallParseError):
            parse_toolcalls(
                valid_search + unclosed_search,
                "glm4_5",
                [search_tool_spec()],
            )
        with self.assertRaises(ToolCallParseError):
            parse_toolcalls(
                valid_other + unclosed_search,
                "glm4_5",
                [tool_spec("f"), search_tool_spec()],
            )

    def test_glm_parser_requires_full_non_nested_batch_consumption(self):
        valid = "<tool_call>f</tool_call>"
        malformed_suffixes = (
            "</tool_call><tool_call>",
            "<tool_call></tool_call>",
            "trailing prose",
        )
        nested = "<tool_call>f<tool_call>other</tool_call></tool_call>"

        for suffix in malformed_suffixes:
            with self.subTest(suffix=suffix), self.assertRaises(ToolCallParseError):
                parse_toolcalls(valid + suffix, "glm4_5", [tool_spec("f")])
        with self.assertRaises(ToolCallParseError):
            parse_toolcalls(nested, "glm4_5", [tool_spec("f"), tool_spec("other")])

    def test_glm_parser_rejects_text_or_tags_between_argument_pairs(self):
        prefix = "<tool_call>f" "<arg_key>cmd</arg_key><arg_value>dir</arg_value>"
        malformed = (
            prefix + "oops</tool_call>",
            prefix + "</arg_value></tool_call>",
            prefix
            + "junk"
            + "<arg_key>other</arg_key><arg_value>x</arg_value>"
            + "</tool_call>",
        )
        spec = ToolSpec(
            type="function",
            function=Function(
                name="f",
                description="test tool",
                parameters={
                    "type": "object",
                    "properties": {
                        "cmd": {"type": "string"},
                        "other": {"type": "string"},
                    },
                    "required": ["cmd"],
                    "additionalProperties": False,
                },
            ),
        )

        for raw in malformed:
            with self.subTest(raw=raw), self.assertRaises(ToolCallParseError):
                parse_toolcalls(raw, "glm4_5", [spec])

    def test_unclosed_search_recovery_rejects_invalid_arguments(self):
        cases = {
            "blank query": ("<arg_key>query</arg_key><arg_value>   </arg_value>"),
            "missing query": ("<arg_key>count</arg_key><arg_value>5</arg_value>"),
            "boolean count": (
                "<arg_key>query</arg_key><arg_value>GPU</arg_value>"
                "<arg_key>count</arg_key><arg_value>true</arg_value>"
            ),
            "count too small": (
                "<arg_key>query</arg_key><arg_value>GPU</arg_value>"
                "<arg_key>count</arg_key><arg_value>0</arg_value>"
            ),
            "count too large": (
                "<arg_key>query</arg_key><arg_value>GPU</arg_value>"
                "<arg_key>count</arg_key><arg_value>11</arg_value>"
            ),
            "unexpected argument": (
                "<arg_key>query</arg_key><arg_value>GPU</arg_value>"
                "<arg_key>country</arg_key><arg_value>Spain</arg_value>"
            ),
            "duplicate argument": (
                "<arg_key>query</arg_key><arg_value>GPU</arg_value>"
                "<arg_key>query</arg_key><arg_value>CPU</arg_value>"
            ),
            "both count spellings": (
                "<arg_key>query</arg_key><arg_value>GPU</arg_value>"
                "<arg_key>count</arg_key><arg_value>5</arg_value>"
                "<arg_key>num_results</arg_key><arg_value>8</arg_value>"
            ),
        }

        for label, arguments in cases.items():
            raw = "<tool_call>zeta_search__web_search" + arguments
            with self.subTest(label=label), self.assertRaises(ToolCallParseError):
                parse_toolcalls(raw, "glm4_5", [search_tool_spec()])

    def test_unclosed_search_recovery_requires_exact_schema_and_function(self):
        raw = (
            "<tool_call>zeta_search__web_search"
            "<arg_key>query</arg_key><arg_value>GPU Spain</arg_value>"
        )
        weak_schema = tool_spec("zeta_search__web_search", ["query"])
        duplicate_specs = [search_tool_spec(), search_tool_spec()]
        destructive = (
            "<tool_call>run" "<arg_key>cmd</arg_key><arg_value>dir</arg_value>"
        )

        with self.assertRaises(ToolCallParseError):
            parse_toolcalls(raw, "glm4_5", [weak_schema])
        with self.assertRaises(ToolCallParseError):
            parse_toolcalls(raw, "glm4_5", duplicate_specs)
        with self.assertRaises(ToolCallParseError):
            parse_toolcalls(destructive, "glm4_5", [tool_spec("run", ["cmd"])])

    def test_parallel_native_zeta_calls_all_receive_strict_validation(self):
        valid = (
            "<tool_call>zeta_search__web_search"
            "<arg_key>query</arg_key><arg_value>GPU Spain</arg_value>"
            "<arg_key>count</arg_key><arg_value>1</arg_value>"
            "</tool_call>"
        )
        invalid = (
            "<tool_call>zeta_search__web_search"
            "<arg_key>query</arg_key><arg_value>CPU Spain</arg_value>"
            "<arg_key>count</arg_key><arg_value>true</arg_value>"
            "</tool_call>"
        )

        with self.assertRaises(ToolCallParseError):
            parse_toolcalls(valid + invalid, "glm4_5", [search_tool_spec()])

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
        duplicate = "To: zeta_search/web_search\n" '{"query":"GPU","query":"CPU"}'
        both_counts = (
            "To: zeta_search/web_search\n" '{"query":"GPU","count":5,"num_results":8}'
        )
        with self.assertRaises(ToolCallParseError):
            parse_zeta_search_pseudo_toolcall(duplicate, [search_tool_spec()])
        with self.assertRaises(ToolCallParseError):
            parse_zeta_search_pseudo_toolcall(both_counts, [search_tool_spec()])

    def test_pseudo_call_enforces_search_argument_types_and_range(self):
        wrong_type = 'To: zeta_search/web_search\n{"query":"GPU","count":"eight"}'
        out_of_range = 'To: zeta_search/web_search\n{"query":"GPU","count":11}'

        with self.assertRaises(ToolCallParseError):
            parse_zeta_search_pseudo_toolcall(wrong_type, [search_tool_spec()])
        with self.assertRaises(ToolCallParseError):
            parse_zeta_search_pseudo_toolcall(out_of_range, [search_tool_spec()])

    def test_pseudo_call_rejects_blank_or_unbounded_query(self):
        blank = 'To: zeta_search/web_search\n{"query":"   "}'
        too_long = 'To: zeta_search/web_search\n{"query":"' + ("x" * 2049) + '"}'

        with self.assertRaises(ToolCallParseError):
            parse_zeta_search_pseudo_toolcall(blank, [search_tool_spec()])
        with self.assertRaises(ToolCallParseError):
            parse_zeta_search_pseudo_toolcall(too_long, [search_tool_spec()])

    def test_pseudo_call_enforces_required_and_additional_properties(self):
        missing_query = 'To: zeta_search/web_search\n{"count":8}'
        extra_argument = 'To: zeta_search/web_search\n{"query":"GPU","country":"Spain"}'

        with self.assertRaises(ToolCallParseError):
            parse_zeta_search_pseudo_toolcall(missing_query, [search_tool_spec()])
        with self.assertRaises(ToolCallParseError):
            parse_zeta_search_pseudo_toolcall(extra_argument, [search_tool_spec()])

    def test_embedded_or_fenced_pseudo_call_remains_content(self):
        embedded = 'Here is an example: To: zeta_search/web_search\n{"query":"GPU"}'
        newline_example = (
            "Here is an example:\n" 'To: zeta_search/web_search\n{"query":"GPU"}'
        )
        search_example = (
            "Example of searching:\n" 'To: zeta_search/web_search\n{"query":"GPU"}'
        )
        negated = (
            "Do not search for this:\n" 'To: zeta_search/web_search\n{"query":"GPU"}'
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
            parse_zeta_search_pseudo_toolcall(negated, [search_tool_spec()])
        )
        self.assertIsNone(
            parse_zeta_search_pseudo_toolcall(fenced, [search_tool_spec()])
        )


if __name__ == "__main__":
    unittest.main()
