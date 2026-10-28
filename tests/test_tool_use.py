"""迭代 2 单测：工具编排 —— bash / edit_file / grep + 参数校验 + 并发分派。

运行：.venv/bin/python -m unittest discover -s tests -v
依然全部使用假客户端，不需要 API key。
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mini_agent.agent import Agent, ToolEvent  # noqa: E402
from mini_agent.tools import Tool, ToolError, default_tools  # noqa: E402

from tests.test_agent_loop import FakeClient, make_response, make_tool_call  # noqa: E402


# ---------------------------------------------------------------------------
# 参数校验（对应 CC validateInput）
# ---------------------------------------------------------------------------

class ValidationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_cwd = os.getcwd()
        os.chdir(self._tmp.name)

    def tearDown(self):
        os.chdir(self._old_cwd)
        self._tmp.cleanup()

    def _tools(self):
        return {t.name: t for t in default_tools()}

    def test_missing_required_parameter(self):
        with self.assertRaises(ToolError) as ctx:
            self._tools()["read_file"].run({})  # 缺 path
        self.assertIn("缺少必填参数", str(ctx.exception))

    def test_wrong_type_rejected(self):
        with self.assertRaises(ToolError) as ctx:
            self._tools()["read_file"].run({"path": 123})  # path 必须是 string
        self.assertIn("类型错误", str(ctx.exception))

    def test_bool_not_accepted_as_integer(self):
        # bool 是 int 子类，integer 校验必须排除它
        with self.assertRaises(ToolError):
            self._tools()["read_file"].run({"path": "x.txt", "limit": True})

    def test_validation_error_fed_back_to_model(self):
        """校验失败 → 错误文本回填 → 模型自纠（循环不崩）。"""
        client = FakeClient(
            [
                make_response(tool_calls=[make_tool_call("c1", "bash", '{"timeout": 30}')]),  # 缺 command
                make_response(content="参数补全后完成了"),
            ]
        )
        result = Agent(client=client, tools=default_tools(), verbose=False).run("跑个命令")
        self.assertEqual(result.stop_reason, "completed")
        self.assertTrue(result.tool_events[0].is_error)
        self.assertIn("缺少必填参数", result.tool_events[0].result)


# ---------------------------------------------------------------------------
# bash 工具
# ---------------------------------------------------------------------------

class BashToolTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_cwd = os.getcwd()
        os.chdir(self._tmp.name)
        self.bash = {t.name: t for t in default_tools()}["bash"]

    def tearDown(self):
        os.chdir(self._old_cwd)
        self._tmp.cleanup()

    def test_normal_command(self):
        out = self.bash.run({"command": "echo hello-bash"})
        self.assertIn("exit_code=0", out)
        self.assertIn("hello-bash", out)

    def test_nonzero_exit_code_reported(self):
        out = self.bash.run({"command": "exit 3"})
        self.assertIn("exit_code=3", out)

    def test_stderr_captured(self):
        out = self.bash.run({"command": "echo oops 1>&2"})
        self.assertIn("[stderr]", out)
        self.assertIn("oops", out)

    def test_timeout(self):
        with self.assertRaises(ToolError) as ctx:
            self.bash.run({"command": "sleep 5", "timeout": 1})
        self.assertIn("超时", str(ctx.exception))

    def test_dangerous_pattern_blocked(self):
        for cmd in ("sudo rm x", "rm -rf /", "mkfs.ext4 /dev/sda"):
            with self.assertRaises(ToolError) as ctx:
                self.bash.run({"command": cmd})
            self.assertIn("黑名单", str(ctx.exception))

    def test_runs_in_cwd(self):
        Path("marker.txt").write_text("from-bash", encoding="utf-8")
        out = self.bash.run({"command": "cat marker.txt"})
        self.assertIn("from-bash", out)


# ---------------------------------------------------------------------------
# edit_file 工具（CC Edit 的唯一性语义）
# ---------------------------------------------------------------------------

class EditFileToolTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_cwd = os.getcwd()
        os.chdir(self._tmp.name)
        tools = {t.name: t for t in default_tools()}
        self.edit, self.write, self.read = tools["edit_file"], tools["write_file"], tools["read_file"]

    def tearDown(self):
        os.chdir(self._old_cwd)
        self._tmp.cleanup()

    def test_unique_replace(self):
        self.write.run({"path": "a.txt", "content": "alpha\nbeta\ngamma\n"})
        out = self.edit.run({"path": "a.txt", "old_string": "beta", "new_string": "BETA"})
        self.assertIn("已替换 1 处", out)
        self.assertIn("BETA", self.read.run({"path": "a.txt"}))

    def test_not_unique_rejected(self):
        self.write.run({"path": "b.txt", "content": "same\nsame\n"})
        with self.assertRaises(ToolError) as ctx:
            self.edit.run({"path": "b.txt", "old_string": "same", "new_string": "x"})
        self.assertIn("2 次，不唯一", str(ctx.exception))
        self.assertIn("replace_all", str(ctx.exception))

    def test_replace_all(self):
        self.write.run({"path": "c.txt", "content": "same\nsame\nsame\n"})
        out = self.edit.run(
            {"path": "c.txt", "old_string": "same", "new_string": "uniq", "replace_all": True}
        )
        self.assertIn("已替换 3 处", out)

    def test_not_found(self):
        self.write.run({"path": "d.txt", "content": "hello\n"})
        with self.assertRaises(ToolError) as ctx:
            self.edit.run({"path": "d.txt", "old_string": "world", "new_string": "x"})
        self.assertIn("未找到", str(ctx.exception))

    def test_edit_missing_file(self):
        with self.assertRaises(ToolError):
            self.edit.run({"path": "ghost.txt", "old_string": "a", "new_string": "b"})


# ---------------------------------------------------------------------------
# grep 工具
# ---------------------------------------------------------------------------

class GrepToolTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_cwd = os.getcwd()
        os.chdir(self._tmp.name)
        tools = {t.name: t for t in default_tools()}
        self.grep, self.write = tools["grep"], tools["write_file"]
        self.write.run({"path": "src/a.py", "content": "def hello():\n    return 'world'\n"})
        self.write.run({"path": "src/b.py", "content": "hello = 1\n"})
        self.write.run({"path": "src/notes.txt", "content": "hello in text\n"})

    def tearDown(self):
        os.chdir(self._old_cwd)
        self._tmp.cleanup()

    def test_regex_search(self):
        out = self.grep.run({"pattern": r"hello\s*="})
        self.assertIn("src/b.py:1:hello = 1", out)
        self.assertNotIn("a.py", out)  # 'hello():' 不匹配 hello\s*=

    def test_glob_filter(self):
        out = self.grep.run({"pattern": "hello", "glob": "*.py"})
        self.assertIn("src/a.py", out)
        self.assertIn("src/b.py", out)
        self.assertNotIn("notes.txt", out)

    def test_no_match(self):
        out = self.grep.run({"pattern": "zzz_nothing"})
        self.assertIn("没有匹配", out)

    def test_invalid_regex(self):
        with self.assertRaises(ToolError) as ctx:
            self.grep.run({"pattern": "([bad"})
        self.assertIn("无效正则", str(ctx.exception))


# ---------------------------------------------------------------------------
# 并发分派（迭代 2 核心）
# ---------------------------------------------------------------------------

def _slow_tool(name: str, sleep_s: float) -> Tool:
    """一个慢的只读工具：sleep 后返回 done-{name}（用于并发计时验证）。"""
    return Tool(
        name=name,
        description=f"slow test tool {name}",
        parameters={"type": "object", "properties": {}, "required": []},
        handler=lambda args: (time.sleep(sleep_s) or f"done-{name}"),
        is_concurrency_safe=True,
        is_read_only=True,
    )


def _slow_unsafe_tool(name: str, sleep_s: float) -> Tool:
    return Tool(
        name=name,
        description=f"slow unsafe tool {name}",
        parameters={"type": "object", "properties": {}, "required": []},
        handler=lambda args: (time.sleep(sleep_s) or f"done-{name}"),
        is_concurrency_safe=False,
    )


class ConcurrencyDispatchTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_cwd = os.getcwd()
        os.chdir(self._tmp.name)

    def tearDown(self):
        os.chdir(self._old_cwd)
        self._tmp.cleanup()

    def test_all_safe_tools_run_concurrently(self):
        """2 个各 sleep 0.3s 的只读工具 → 并发后总耗时应 ≈0.3s 而非 0.6s。"""
        tools = [_slow_tool("slow_a", 0.3), _slow_tool("slow_b", 0.3)]
        client = FakeClient(
            [
                make_response(
                    tool_calls=[
                        make_tool_call("c1", "slow_a", "{}"),
                        make_tool_call("c2", "slow_b", "{}"),
                    ]
                ),
                make_response(content="done"),
            ]
        )
        started = time.perf_counter()
        result = Agent(client=client, tools=tools, verbose=False).run("并发")
        elapsed = time.perf_counter() - started
        self.assertEqual(result.stop_reason, "completed")
        # 串行至少 0.6s；并发 ≈0.3s。阈值取 0.5s 留出调度开销余量
        self.assertLess(elapsed, 0.5, f"耗时 {elapsed:.2f}s，未并发执行")

    def test_mixed_safe_unsafe_run_sequentially(self):
        """只读 + 有副作用混在同一轮 → 全部串行（保序语义）。"""
        tools = [_slow_tool("slow_safe", 0.3), _slow_unsafe_tool("slow_unsafe", 0.3)]
        client = FakeClient(
            [
                make_response(
                    tool_calls=[
                        make_tool_call("c1", "slow_safe", "{}"),
                        make_tool_call("c2", "slow_unsafe", "{}"),
                    ]
                ),
                make_response(content="done"),
            ]
        )
        started = time.perf_counter()
        Agent(client=client, tools=tools, verbose=False).run("混合")
        elapsed = time.perf_counter() - started
        self.assertGreaterEqual(elapsed, 0.58, f"耗时 {elapsed:.2f}s，不应并发")

    def test_builtin_flags(self):
        """内置工具的并发安全标记正确：只读四件套 True，写改命令三件 False。"""
        flags = {t.name: t.is_concurrency_safe for t in default_tools()}
        self.assertTrue(all(flags[n] for n in ("read_file", "list_dir", "glob_files", "grep")))
        self.assertFalse(any(flags[n] for n in ("write_file", "edit_file", "bash")))

    def test_parallel_results_keep_original_order(self):
        """并发执行后，事件与 tool 消息仍按 tool_calls 原始顺序回填。"""
        tools = [_slow_tool("slow_a", 0.2), _slow_tool("slow_b", 0.05)]
        client = FakeClient(
            [
                make_response(
                    tool_calls=[
                        make_tool_call("c1", "slow_a", "{}"),  # 慢的先提交
                        make_tool_call("c2", "slow_b", "{}"),  # 快的后提交（先完成）
                    ]
                ),
                make_response(content="done"),
            ]
        )
        result = Agent(client=client, tools=tools, verbose=False).run("保序")
        # 结果映射不串线：c1 → slow_a 的产出，c2 → slow_b 的产出
        self.assertEqual(result.tool_events[0].result, "done-slow_a")
        self.assertEqual(result.tool_events[1].result, "done-slow_b")
        second_req = client.requests[1]
        tool_msgs = [m for m in second_req["messages"] if m.get("role") == "tool"]
        self.assertEqual([m["tool_call_id"] for m in tool_msgs], ["c1", "c2"])
        self.assertEqual(tool_msgs[0]["content"], "done-slow_a")
        self.assertEqual(tool_msgs[1]["content"], "done-slow_b")

    def test_unknown_tool_in_batch_stays_sequential(self):
        """批次含未知工具 → 不并发（安全降级）。"""
        tools = [_slow_tool("slow_a", 0.05)]
        client = FakeClient(
            [
                make_response(
                    tool_calls=[
                        make_tool_call("c1", "slow_a", "{}"),
                        make_tool_call("c2", "no_such", "{}"),
                    ]
                ),
                make_response(content="done"),
            ]
        )
        result = Agent(client=client, tools=tools, verbose=False).run("未知工具")
        self.assertEqual(result.stop_reason, "completed")
        self.assertTrue(result.tool_events[1].is_error)


if __name__ == "__main__":
    unittest.main(verbosity=2)
