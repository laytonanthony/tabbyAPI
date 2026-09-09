import unittest

from endpoints.OAI.utils.toolcall_formats import gemma4
from endpoints.OAI.utils.toolcall_formats import harmony
from endpoints.OAI.utils.toolcall_formats import mistral
from endpoints.OAI.utils.toolcall_formats import mistral_old
from endpoints.OAI.utils.toolcall_formats import muse_glimmer


class Gemma4StrictParsingTests(unittest.TestCase):
    def test_valid_call_and_no_argument_call(self):
        calls = gemma4.parse_toolcalls(
            '<|tool_call>call:write{path:<|"|>/tmp/a<|"|>}<tool_call|>'
            '<|tool_call>call:list_files{}<tool_call|>'
        )
        self.assertEqual([call.function.name for call in calls], ["write", "list_files"])
        self.assertEqual(calls[0].function.arguments, '{"path": "/tmp/a"}')
        self.assertEqual(calls[1].function.arguments, "{}")

    def test_invalid_json_is_rejected_instead_of_becoming_no_args(self):
        with self.assertRaises(ValueError):
            gemma4.parse_toolcalls(
                '<|tool_call>call:write{path:<|"|>/tmp/a<|"|>,}<tool_call|>'
            )

    def test_parallel_batch_with_incomplete_second_call_is_atomic(self):
        with self.assertRaises(ValueError):
            gemma4.parse_toolcalls(
                '<|tool_call>call:list_files{}<tool_call|>'
                '<|tool_call>call:write{path:<|"|>/tmp/a<|"|>}'
            )

    def test_duplicate_argument_is_rejected(self):
        with self.assertRaises(ValueError):
            gemma4.parse_toolcalls(
                '<|tool_call>call:f{a:1,a:2}<tool_call|>'
            )


class MistralStrictParsingTests(unittest.TestCase):
    def test_valid_parallel_calls_and_marker_text_inside_value(self):
        calls = mistral.parse_toolcalls(
            '[TOOL_CALLS]write[ARGS]{"text":"literal [TOOL_CALLS] marker"}\n'
            '[TOOL_CALLS]list_files[ARGS]{}'
        )
        self.assertEqual([call.function.name for call in calls], ["write", "list_files"])
        self.assertEqual(
            calls[0].function.arguments,
            '{"text": "literal [TOOL_CALLS] marker"}',
        )
        self.assertEqual(calls[1].function.arguments, "{}")

    def test_invalid_json_is_rejected(self):
        with self.assertRaises(ValueError):
            mistral.parse_toolcalls('[TOOL_CALLS]f[ARGS]{"a":')

    def test_partial_parallel_batch_is_rejected_atomically(self):
        with self.assertRaises(ValueError):
            mistral.parse_toolcalls(
                '[TOOL_CALLS]ok[ARGS]{}[TOOL_CALLS]broken-without-args'
            )

    def test_non_object_arguments_are_rejected(self):
        with self.assertRaises(ValueError):
            mistral.parse_toolcalls('[TOOL_CALLS]f[ARGS][1, 2]')

    def test_unparsed_trailing_text_is_rejected(self):
        with self.assertRaises(ValueError):
            mistral.parse_toolcalls('[TOOL_CALLS]f[ARGS]{} garbage')


class MistralOldStrictParsingTests(unittest.TestCase):
    def test_valid_parallel_calls_and_no_arguments(self):
        calls = mistral_old.parse_toolcalls(
            '[TOOL_CALLS][{"name":"f","arguments":{"a":1}},'
            '{"name":"list_files"}]'
        )
        self.assertEqual([call.function.name for call in calls], ["f", "list_files"])
        self.assertEqual(calls[0].function.arguments, '{"a": 1}')
        self.assertEqual(calls[1].function.arguments, "{}")

    def test_marker_text_inside_argument_value_is_valid(self):
        calls = mistral_old.parse_toolcalls(
            '[TOOL_CALLS][{"name":"f","arguments":{"text":"[TOOL_CALLS]"}}]'
        )
        self.assertEqual(calls[0].function.arguments, '{"text": "[TOOL_CALLS]"}')

    def test_invalid_batch_json_is_rejected(self):
        with self.assertRaises(ValueError):
            mistral_old.parse_toolcalls('[TOOL_CALLS][{"name":"f"}')

    def test_bad_parallel_entry_is_not_skipped(self):
        with self.assertRaises(ValueError):
            mistral_old.parse_toolcalls(
                '[TOOL_CALLS][{"name":"ok"},{"arguments":{}}]'
            )

    def test_string_arguments_must_decode_to_an_object(self):
        with self.assertRaises(ValueError):
            mistral_old.parse_toolcalls(
                '[TOOL_CALLS][{"name":"f","arguments":"not json"}]'
            )


class HarmonyStrictParsingTests(unittest.TestCase):
    def test_valid_call_and_no_argument_call(self):
        calls = harmony.parse_toolcalls(
            'assistant to=functions.f<|channel|>commentary json<|message|>'
            '{"a":1}<|call|>'
            'assistant to=functions.list_files<|channel|>commentary json<|message|>'
            '{}<|call|>'
        )
        self.assertEqual([call.function.name for call in calls], ["f", "list_files"])
        self.assertEqual(calls[0].function.arguments, '{"a": 1}')
        self.assertEqual(calls[1].function.arguments, "{}")

    def test_invalid_json_is_rejected(self):
        with self.assertRaises(ValueError):
            harmony.parse_toolcalls(
                'to=functions.f<|message|>{"a":<|call|>'
            )

    def test_non_object_arguments_are_rejected(self):
        with self.assertRaises(ValueError):
            harmony.parse_toolcalls('to=functions.f<|message|>[]<|call|>')

    def test_partial_parallel_batch_is_rejected_atomically(self):
        with self.assertRaises(ValueError):
            harmony.parse_toolcalls(
                'to=functions.ok<|message|>{}<|call|>'
                'to=functions.broken<|message|>{"a":1}'
            )

    def test_missing_recipient_is_rejected(self):
        with self.assertRaises(ValueError):
            harmony.parse_toolcalls('<|channel|>commentary<|message|>{}<|call|>')


class MuseGlimmerStrictParsingTests(unittest.TestCase):
    def test_valid_call_and_no_argument_call(self):
        calls = muse_glimmer.parse_toolcalls(
            '<atem:invoke name="f">'
            '<atem:parameter name="a">1</atem:parameter>'
            '</atem:invoke>'
            '<atem:invoke name="list_files"></atem:invoke>'
        )
        self.assertEqual([call.function.name for call in calls], ["f", "list_files"])
        self.assertEqual(calls[0].function.arguments, '{"a": 1}')
        self.assertEqual(calls[1].function.arguments, "{}")

    def test_missing_closing_invoke_remains_supported(self):
        calls = muse_glimmer.parse_toolcalls(
            '<atem:invoke name="f">'
            '<atem:parameter name="a">hello</atem:parameter>'
        )
        self.assertEqual(calls[0].function.arguments, '{"a": "hello"}')

    def test_missing_closing_invoke_before_next_message_is_supported(self):
        calls = muse_glimmer.parse_toolcalls(
            'to=f<|message|><atem:function_calls>'
            '<atem:invoke name="f">'
            '<atem:parameter name="a">1</atem:parameter>'
            '</atem:function_calls><|eom|>'
            'to=g<|message|><atem:function_calls>'
            '<atem:invoke name="g"></atem:invoke>'
            '</atem:function_calls><|eom|>'
        )
        self.assertEqual([call.function.name for call in calls], ["f", "g"])
        self.assertEqual(calls[0].function.arguments, '{"a": 1}')
        self.assertEqual(calls[1].function.arguments, "{}")

    def test_dangling_parameter_is_rejected(self):
        with self.assertRaises(ValueError):
            muse_glimmer.parse_toolcalls(
                '<atem:invoke name="f">'
                '<atem:parameter name="a">hello</atem:invoke>'
            )

    def test_malformed_structured_value_is_rejected(self):
        with self.assertRaises(ValueError):
            muse_glimmer.parse_toolcalls(
                '<atem:invoke name="f">'
                '<atem:parameter name="a">{"bad":</atem:parameter>'
                '</atem:invoke>'
            )

    def test_duplicate_parameter_is_rejected(self):
        with self.assertRaises(ValueError):
            muse_glimmer.parse_toolcalls(
                '<atem:invoke name="f">'
                '<atem:parameter name="a">1</atem:parameter>'
                '<atem:parameter name="a">2</atem:parameter>'
                '</atem:invoke>'
            )

    def test_partial_parallel_batch_is_rejected_atomically(self):
        with self.assertRaises(ValueError):
            muse_glimmer.parse_toolcalls(
                '<atem:invoke name="ok"></atem:invoke>'
                '<atem:invoke name="broken">'
                '<atem:parameter name="a">1'
            )

    def test_parameter_outside_invoke_is_rejected(self):
        with self.assertRaises(ValueError):
            muse_glimmer.parse_toolcalls(
                '<atem:invoke name="ok"></atem:invoke>'
                '<atem:parameter name="orphan">1</atem:parameter>'
            )


if __name__ == "__main__":
    unittest.main()
