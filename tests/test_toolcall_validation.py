import unittest

from endpoints.OAI.types.tools import Function, ToolSpec
from endpoints.OAI.utils.tools import ToolCallParseError, parse_toolcalls


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


if __name__ == "__main__":
    unittest.main()
