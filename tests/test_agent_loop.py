"""Agent Loop 冒烟测试：不需要 API key，用「假模型客户端」脚本化响应。

验证的正是 Agentic Loop 的机制本身：
- 有 tool_calls → 执行工具 → 回填 tool 消息 → 继续下一轮
- 无 tool_calls → completed 终止
- 轮次超限 → max_turns 终止
- 工具出错 → 错误文本回填（循环不崩）

运行：.venv/bin/python -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mini_agent.agent import Agent  # noqa: E402
from mini_agent.tools import ToolError, default_tools, safe_path  # noqa: E402


# ---------------------------------------------------------------------------
# 假模型客户端：按脚本依次吐出响应
# ---------------------------------------------------------------------------

class FakeClient:
    """模拟 openai.OpenAI 的 chat.completions.create 接口。"""

    def __init__(self, responses: list):
        self.responses = list(responses)
        self.requests: list[dict] = []  # 记录每次调用的入参，供断言
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.requests.append(kwargs)
        if not self.responses:
            raise AssertionError("脚本响应已耗尽，模型被调用了多余次数")
        return self.responses.pop(0)


def make_response(content=None, tool_calls=None, in_tok=10, out_tok=5):
    """构造一个形如 openai ChatCompletion 的响应对象。"""
    msg = SimpleNamespace(content=content, tool_calls=tool_calls)
    msg.model_dump = lambda **_: {
        "role": "assistant",
        **({"content": content} if content is not None else {}),
        **({"tool_calls": tool_calls} if tool_calls else {}),
    }
    choice = SimpleNamespace(message=msg)
    usage = SimpleNamespace(prompt_tokens=in_tok, completion_tokens=out_tok)
    return SimpleNamespace(choices=[choice], usage=usage)


def make_tool_call(call_id: str, name: str, arguments: str):
    fn = SimpleNamespace(name=name, arguments=arguments)
    return SimpleNamespace(id=call_id, function=fn)


# ---------------------------------------------------------------------------
# 测试用例
# ---------------------------------------------------------------------------

class AgentLoopTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_cwd = os.getcwd()
        os.chdir(self._tmp.name)  # 工具的「工作目录围栏」以 cwd 为根
        (Path(self._tmp.name) / "hello.txt").write_text("hello mini-agent\n", encoding="utf-8")

    def tearDown(self):
        os.chdir(self._old_cwd)
        self._tmp.cleanup()

    def _agent(self, client, **kw):
        return Agent(client=client, tools=default_tools(), verbose=False, max_turns=kw.pop("max_turns", 10), **kw)

    def test_loop_completes_after_tool_use(self):
        """读文件 → 回填结果 → 第二轮给出文本答案。"""
        client = FakeClient(
            [
                make_response(tool_calls=[make_tool_call("c1", "read_file", '{"path": "hello.txt"}')]),
                make_response(content="文件内容是 hello mini-agent"),
            ]
        )
        result = self._agent(client).run("读一下 hello.txt")
        self.assertEqual(result.stop_reason, "completed")
        self.assertEqual(result.turn_count, 1)
        self.assertEqual(result.tool_calls_count, 1)
        self.assertEqual(result.final_text, "文件内容是 hello mini-agent")
        # 第二次请求必须包含 tool 结果消息（观察回填）
        second_req = client.requests[1]
        tool_msgs = [m for m in second_req["messages"] if m.get("role") == "tool"]
        self.assertEqual(len(tool_msgs), 1)
        self.assertEqual(tool_msgs[0]["tool_call_id"], "c1")
        self.assertIn("hello mini-agent", tool_msgs[0]["content"])
        self.assertFalse(result.tool_events[0].is_error)
        # usage 累计
        self.assertEqual(result.input_tokens, 20)
        self.assertEqual(result.output_tokens, 10)

    def test_max_turns_termination(self):
        """模型永远发工具调用 → 达到 max_turns 强制终止。"""
        always_tool = [
            make_response(tool_calls=[make_tool_call(f"c{i}", "list_dir", '{"path": "."}')])
            for i in range(100)
        ]
        client = FakeClient(always_tool)
        result = self._agent(client, max_turns=3).run("无限列目录")
        self.assertEqual(result.stop_reason, "max_turns")
        self.assertEqual(result.turn_count, 3)

    def test_tool_error_is_fed_back_not_crash(self):
        """工具报错 → 错误文本回填，循环继续并正常完成。"""
        client = FakeClient(
            [
                make_response(tool_calls=[make_tool_call("c1", "read_file", '{"path": "no-such.txt"}')]),
                make_response(content="文件不存在，我换个方式"),
            ]
        )
        result = self._agent(client).run("读一个不存在的文件")
        self.assertEqual(result.stop_reason, "completed")
        self.assertTrue(result.tool_events[0].is_error)
        self.assertIn("文件不存在", result.tool_events[0].result)
        second_req = client.requests[1]
        tool_msgs = [m for m in second_req["messages"] if m.get("role") == "tool"]
        self.assertIn("[工具错误]", tool_msgs[0]["content"])

    def test_unknown_tool_and_bad_json(self):
        """未知工具名 / 参数不是合法 JSON → 都转成错误观察，不抛异常。"""
        client = FakeClient(
            [
                make_response(tool_calls=[make_tool_call("c1", "no_such_tool", '{}')]),
                make_response(tool_calls=[make_tool_call("c2", "read_file", "not-json{")]),
                make_response(content="工具都失败但我活下来了"),
            ]
        )
        result = self._agent(client).run("乱调工具")
        self.assertEqual(result.stop_reason, "completed")
        self.assertTrue(result.tool_events[0].is_error)
        self.assertIn("未知工具", result.tool_events[0].result)
        self.assertTrue(result.tool_events[1].is_error)
        self.assertIn("[参数解析失败]", result.tool_events[1].result)

    def test_multi_tool_calls_in_one_turn(self):
        """一轮内并行下发多个 tool_calls → 逐个回填，顺序保持。"""
        client = FakeClient(
            [
                make_response(
                    tool_calls=[
                        make_tool_call("c1", "read_file", '{"path": "hello.txt"}'),
                        make_tool_call("c2", "list_dir", '{"path": "."}'),
                    ]
                ),
                make_response(content="两件事都做完了"),
            ]
        )
        result = self._agent(client).run("读文件并列表")
        self.assertEqual(result.tool_calls_count, 2)
        second_req = client.requests[1]
        tool_msgs = [m for m in second_req["messages"] if m.get("role") == "tool"]
        self.assertEqual([m["tool_call_id"] for m in tool_msgs], ["c1", "c2"])

    def test_tools_schema_serializable(self):
        """工具 schema 能被 json 序列化（发给 API 的前提）。"""
        import json

        for t in default_tools():
            dumped = json.dumps(t.to_api_schema())
            self.assertIn(t.name, dumped)


class ToolLayerTests(unittest.TestCase):
    """工具层单测（同样在临时目录内验证围栏）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_cwd = os.getcwd()
        os.chdir(self._tmp.name)

    def tearDown(self):
        os.chdir(self._old_cwd)
        self._tmp.cleanup()

    def test_read_write_flow(self):
        tools = {t.name: t for t in default_tools()}
        out = tools["write_file"].run({"path": "a/b.txt", "content": "第一行\n第二行"})
        self.assertIn("已写入", out)
        content = tools["read_file"].run({"path": "a/b.txt"})
        self.assertIn("1\t第一行", content)

    def test_path_traversal_blocked(self):
        with self.assertRaises(ToolError):
            safe_path("../../etc/passwd")
        with self.assertRaises(ToolError):
            safe_path("/etc/passwd")

    def test_read_missing_file_is_tool_error(self):
        tools = {t.name: t for t in default_tools()}
        with self.assertRaises(ToolError):
            tools["read_file"].run({"path": "ghost.txt"})

    def test_result_truncation(self):
        tools = {t.name: t for t in default_tools()}
        long_content = "x" * 20000
        tools["write_file"].run({"path": "big.txt", "content": long_content})
        out = tools["read_file"].run({"path": "big.txt", "limit": 20000})
        self.assertLess(len(out), 20000)
        self.assertIn("已截断", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
