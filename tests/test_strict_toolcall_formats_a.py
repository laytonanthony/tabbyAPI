import unittest

from endpoints.OAI.utils.toolcall_formats.deepseek_v4 import (
    parse_toolcalls as parse_deepseek,
)
from endpoints.OAI.utils.toolcall_formats.minimax_m2 import (
    parse_toolcalls as parse_minimax,
)
from endpoints.OAI.utils.toolcall_formats.qwen3_coder import (
    parse_toolcalls as parse_qwen,
)


class QwenStrictToolcallTests(unittest.TestCase):
    def test_valid_wrapped_parallel_and_no_args(self):
        calls = parse_qwen(
            "<tool_call>"
            "<function=write_file><parameter=path>/tmp/a</parameter>"
            "<parameter=lines>[1, 2]</parameter></function>"
            "</tool_call>"
            "<tool_call><function=list_files></function></tool_call>"
        )
        self.assertEqual([call.function.name for call in calls], ["write_file", "list_files"])
        self.assertEqual(
            calls[0].function.arguments, '{"path": "/tmp/a", "lines": [1, 2]}'
        )
        self.assertEqual(calls[1].function.arguments, "{}")

    def test_valid_bare_no_arg_call(self):
        calls = parse_qwen("\n<function=list_files>\n</function>\n")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].function.arguments, "{}")

    def test_incomplete_parameter_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_qwen(
                "<tool_call><function=f>"
                "<parameter=cmd>echo hi"
                "</function></tool_call>"
            )

    def test_misordered_parameter_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_qwen(
                "<tool_call><function=f>"
                "</parameter><parameter=cmd>echo hi</parameter>"
                "</function></tool_call>"
            )

    def test_duplicate_parameter_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_qwen(
                "<tool_call><function=f>"
                "<parameter=cmd>one</parameter><parameter=cmd>two</parameter>"
                "</function></tool_call>"
            )

    def test_partial_parallel_batch_is_rejected_atomically(self):
        with self.assertRaises(ValueError):
            parse_qwen(
                "<tool_call><function=good></function></tool_call>"
                "<tool_call><function=broken><parameter=x>1</parameter></tool_call>"
            )

    def test_mixed_wrapped_and_bare_batch_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_qwen(
                "<tool_call><function=good></function></tool_call>"
                "<function=unwrapped></function>"
            )


class MiniMaxStrictToolcallTests(unittest.TestCase):
    def test_valid_parallel_and_no_args(self):
        calls = parse_minimax(
            "<minimax:tool_call>"
            '<invoke name="write"><parameter name="count">2</parameter></invoke>'
            '<invoke name="list_files"></invoke>'
            "</minimax:tool_call>"
        )
        self.assertEqual([call.function.name for call in calls], ["write", "list_files"])
        self.assertEqual(calls[0].function.arguments, '{"count": 2}')
        self.assertEqual(calls[1].function.arguments, "{}")

    def test_incomplete_parameter_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_minimax(
                "<minimax:tool_call>"
                '<invoke name="f"><parameter name="cmd">echo hi</invoke>'
                "</minimax:tool_call>"
            )

    def test_unmatched_parameter_close_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_minimax(
                "<minimax:tool_call>"
                '<invoke name="f"></parameter></invoke>'
                "</minimax:tool_call>"
            )

    def test_duplicate_parameter_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_minimax(
                "<minimax:tool_call>"
                '<invoke name="f"><parameter name="x">1</parameter>'
                '<parameter name="x">2</parameter></invoke>'
                "</minimax:tool_call>"
            )

    def test_partial_parallel_invocations_are_rejected_atomically(self):
        with self.assertRaises(ValueError):
            parse_minimax(
                "<minimax:tool_call>"
                '<invoke name="good"></invoke>'
                '<invoke name="broken"><parameter name="x">1</parameter>'
                "</minimax:tool_call>"
            )

    def test_partial_parallel_wrappers_are_rejected_atomically(self):
        with self.assertRaises(ValueError):
            parse_minimax(
                '<minimax:tool_call><invoke name="good"></invoke>'
                "</minimax:tool_call>"
                '<minimax:tool_call><invoke name="broken"></invoke>'
            )


class DeepSeekStrictToolcallTests(unittest.TestCase):
    def test_valid_wrapped_parallel_and_no_args(self):
        calls = parse_deepseek(
            "<｜DSML｜tool_calls>"
            '<｜DSML｜invoke name="write">'
            '<｜DSML｜parameter name="count" string="false">2'
            "</｜DSML｜parameter></｜DSML｜invoke>"
            '<｜DSML｜invoke name="list_files"></｜DSML｜invoke>'
            "</｜DSML｜tool_calls>"
        )
        self.assertEqual([call.function.name for call in calls], ["write", "list_files"])
        self.assertEqual(calls[0].function.arguments, '{"count": 2}')
        self.assertEqual(calls[1].function.arguments, "{}")

    def test_valid_bare_no_arg_call(self):
        calls = parse_deepseek(
            '\n<｜DSML｜invoke name="list_files">\n</｜DSML｜invoke>\n'
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].function.arguments, "{}")

    def test_incomplete_parameter_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_deepseek(
                '<｜DSML｜invoke name="f">'
                '<｜DSML｜parameter name="cmd" string="true">echo hi'
                "</｜DSML｜invoke>"
            )

    def test_misordered_parameter_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_deepseek(
                '<｜DSML｜invoke name="f">'
                "</｜DSML｜parameter>"
                '<｜DSML｜parameter name="x" string="false">1'
                "</｜DSML｜parameter></｜DSML｜invoke>"
            )

    def test_duplicate_parameter_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_deepseek(
                '<｜DSML｜invoke name="f">'
                '<｜DSML｜parameter name="x" string="false">1'
                "</｜DSML｜parameter>"
                '<｜DSML｜parameter name="x" string="false">2'
                "</｜DSML｜parameter></｜DSML｜invoke>"
            )

    def test_partial_parallel_batch_is_rejected_atomically(self):
        with self.assertRaises(ValueError):
            parse_deepseek(
                '<｜DSML｜invoke name="good"></｜DSML｜invoke>'
                '<｜DSML｜invoke name="broken">'
                '<｜DSML｜parameter name="x" string="false">1'
                "</｜DSML｜invoke>"
            )

    def test_partial_parallel_wrappers_are_rejected_atomically(self):
        with self.assertRaises(ValueError):
            parse_deepseek(
                "<｜DSML｜tool_calls>"
                '<｜DSML｜invoke name="good"></｜DSML｜invoke>'
                "</｜DSML｜tool_calls>"
                "<｜DSML｜tool_calls>"
                '<｜DSML｜invoke name="broken"></｜DSML｜invoke>'
            )


if __name__ == "__main__":
    unittest.main()
