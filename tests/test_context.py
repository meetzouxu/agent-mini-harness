"""迭代 5 单测：维护上下文 —— 分段组装、确定性缓存、多轮会话上下文保持。

运行：.venv/bin/python -m unittest discover -s tests -v
全部使用假客户端与内存 registry，不依赖 API key。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mini_agent.agent import Agent  # noqa: E402
from mini_agent.prompt import (  # noqa: E402
    SYSTEM_PROMPT_BOUNDARY,
    SectionRegistry,
    build_system_prompt,
    default_registry,
)
from mini_agent.session import QueryEngine  # noqa: E402
from mini_agent.tools import default_tools  # noqa: E402

from tests.test_agent_loop import FakeClient, make_response, make_tool_call  # noqa: E402


# ---------------------------------------------------------------------------
# 分段组装与 BOUNDARY
# ---------------------------------------------------------------------------

class SectionRegistryTests(unittest.TestCase):
    def test_assemble_in_registration_order(self):
        registry = SectionRegistry()
        registry.register("a", lambda: "段A", is_static=True)
        registry.register("b", lambda: "段B")
        parts = registry.resolve()
        self.assertEqual(parts, ["段A", SYSTEM_PROMPT_BOUNDARY, "段B"])

    def test_boundary_between_static_and_dynamic(self):
        registry = SectionRegistry()
        registry.register("s1", lambda: "S1", is_static=True)
        registry.register("s2", lambda: "S2", is_static=True)
        registry.register("d1", lambda: "D1")
        registry.register("d2", lambda: "D2")
        parts = registry.resolve()
        # BOUNDARY 恰好插在静态区与动态区之间
        self.assertEqual(parts, ["S1", "S2", SYSTEM_PROMPT_BOUNDARY, "D1", "D2"])

    def test_build_removes_boundary(self):
        registry = SectionRegistry()
        registry.register("s", lambda: "静态", is_static=True)
        registry.register("d", lambda: "动态")
        system = build_system_prompt(registry.resolve())
        self.assertNotIn(SYSTEM_PROMPT_BOUNDARY, system)
        self.assertEqual(system, "静态\n\n动态")

    def test_duplicate_name_rejected(self):
        registry = SectionRegistry()
        registry.register("a", lambda: "x")
        with self.assertRaises(ValueError):
            registry.register("a", lambda: "y")

    def test_all_static_no_boundary(self):
        registry = SectionRegistry()
        registry.register("a", lambda: "A", is_static=True)
        self.assertEqual(registry.resolve(), ["A"])


# ---------------------------------------------------------------------------
# 确定性缓存
# ---------------------------------------------------------------------------

class DeterministicCacheTests(unittest.TestCase):
    def test_static_section_computed_once(self):
        registry = SectionRegistry()
        calls = []

        def build():
            calls.append(1)
            return "静态内容"

        registry.register("s", build, is_static=True)
        registry.resolve()
        registry.resolve()
        registry.resolve()
        self.assertEqual(len(calls), 1)  # 只计算一次
        self.assertEqual(registry.stats.hits, 2)
        self.assertEqual(registry.stats.misses, 1)

    def test_dynamic_section_content_unchanged_hits_cache(self):
        """动态段：输入不变 → 内容相同 → 命中（builder 仍执行，内容比对）。"""
        registry = SectionRegistry()
        content = {"v": "甲"}
        calls = []

        def build():
            calls.append(1)
            return f"动态内容: {content['v']}"

        registry.register("d", build)
        registry.resolve()  # miss（首次）
        registry.resolve()  # hit（内容相同）
        self.assertEqual(len(calls), 2)  # 动态段每轮重算 builder
        self.assertEqual(registry.stats.hits, 1)
        self.assertEqual(registry.stats.misses, 1)

    def test_dynamic_section_change_invalidates(self):
        registry = SectionRegistry()
        state = {"v": "旧"}
        registry.register("d", lambda: f"值: {state['v']}")
        first = build_system_prompt(registry.resolve())
        state["v"] = "新"
        second = build_system_prompt(registry.resolve())
        self.assertEqual(first, "值: 旧")
        self.assertEqual(second, "值: 新")
        self.assertEqual(registry.stats.misses, 2)

    def test_cache_break_forces_recompute(self):
        """cache_break=True：内容相同也不命中（对应 DANGEROUS_uncached）。"""
        registry = SectionRegistry()
        registry.register("mcp", lambda: "MCP 指令", cache_break=True)
        registry.resolve()
        registry.resolve()
        self.assertEqual(registry.stats.hits, 0)
        self.assertEqual(registry.stats.misses, 2)

    def test_reset_clears_cache(self):
        registry = SectionRegistry()
        calls = []

        def build():
            calls.append(1)
            return "静态"

        registry.register("s", build, is_static=True)
        registry.resolve()
        registry.reset()
        registry.resolve()
        self.assertEqual(len(calls), 2)  # reset 后重算
        self.assertEqual(registry.stats.hits, 0)


# ---------------------------------------------------------------------------
# 默认注册表（identity / tools / workspace）
# ---------------------------------------------------------------------------

class DefaultRegistryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_cwd = os.getcwd()
        os.chdir(self._tmp.name)

    def tearDown(self):
        os.chdir(self._old_cwd)
        self._tmp.cleanup()

    def test_default_sections_present(self):
        registry = default_registry(default_tools())
        parts = registry.resolve()
        system = build_system_prompt(parts)
        self.assertIn("mini-agent", system)          # identity
        self.assertIn("read_file", system)           # tools
        self.assertIn("工作目录", system)              # workspace
        self.assertIn(SYSTEM_PROMPT_BOUNDARY, parts)  # 静态/动态分界

    def test_tools_change_reflected_next_resolve(self):
        """工具集变化 → tools 段缓存失效 → system prompt 更新（每轮动态组装）。"""
        tools = default_tools()[:2]  # 只留 read/write
        registry = default_registry(tools)
        first = build_system_prompt(registry.resolve())
        self.assertNotIn("bash", first)
        # 运行中注册新工具（模拟 MCP 动态接入）
        tools.append(default_tools()[-1])  # bash
        second = build_system_prompt(registry.resolve())
        self.assertIn("bash", second)
        self.assertNotEqual(first, second)


# ---------------------------------------------------------------------------
# QueryEngine：多轮上下文保持
# ---------------------------------------------------------------------------

class QueryEngineTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_cwd = os.getcwd()
        os.chdir(self._tmp.name)
        (Path(self._tmp.name) / "hello.txt").write_text("hello mini-agent\n", encoding="utf-8")
        self.tools = default_tools()
        self.registry = default_registry(self.tools)

    def tearDown(self):
        os.chdir(self._old_cwd)
        self._tmp.cleanup()

    def _engine(self, client):
        agent = Agent(client=client, tools=self.tools, verbose=False)
        return QueryEngine(agent, self.registry)

    def test_context_carried_across_turns(self):
        """核心：第二轮请求包含第一轮的全部消息 + 新 user 消息。"""
        client = FakeClient(
            [
                make_response(tool_calls=[make_tool_call("c1", "read_file", '{"path": "hello.txt"}')]),
                make_response(content="文件内容是 hello mini-agent"),
                make_response(content="是的，正如我刚才读到的"),  # 第二轮
            ]
        )
        engine = self._engine(client)
        engine.submit_message("读一下 hello.txt")
        engine.submit_message("刚才读到了什么？")

        # 第二轮请求的消息 = 第一轮全部 + 新 user
        second_req = client.requests[2]
        roles = [m.get("role") for m in second_req["messages"]]
        self.assertEqual(roles[0], "system")
        self.assertIn("user", roles)
        self.assertIn("assistant", roles)
        self.assertIn("tool", roles)  # 第一轮的工具观察保留
        # 末尾两条：final assistant（第一轮）+ 新 user（本轮）
        self.assertEqual(roles[-2], "assistant")
        self.assertEqual(roles[-1], "user")
        users = [m for m in second_req["messages"] if m.get("role") == "user"]
        self.assertEqual(users[-1]["content"], "刚才读到了什么？")
        # 工具结果仍在上下文中
        tool_msgs = [m for m in second_req["messages"] if m.get("role") == "tool"]
        self.assertIn("hello mini-agent", tool_msgs[0]["content"])

    def test_usage_accumulates_across_turns(self):
        client = FakeClient(
            [
                make_response(content="第一轮回答", in_tok=100, out_tok=50),
                make_response(content="第二轮回答", in_tok=200, out_tok=80),
            ]
        )
        engine = self._engine(client)
        engine.submit_message("问题一")
        engine.submit_message("问题二")
        self.assertEqual(engine.stats.turns, 2)
        self.assertEqual(engine.stats.input_tokens, 300)
        self.assertEqual(engine.stats.output_tokens, 130)

    def test_system_prompt_replaced_on_change(self):
        """动态段变化 → messages[0] 被替换为新的 system prompt。"""
        client = FakeClient(
            [
                make_response(content="ok1"),
                make_response(content="ok2"),
            ]
        )
        engine = self._engine(client)
        engine.submit_message("第一轮")
        first_system = engine.messages[0]["content"]
        # 改变工具集（registry 捕获同一 list 引用）
        self.tools.append(self._make_extra_tool("extra_probe"))
        engine.submit_message("第二轮")
        second_system = engine.messages[0]["content"]
        self.assertNotEqual(first_system, second_system)
        self.assertIn("extra_probe", second_system)
        # 第二次请求确实带了新 system
        self.assertIn("extra_probe", client.requests[1]["messages"][0]["content"])

    def test_clear_resets_session(self):
        client = FakeClient(
            [
                make_response(content="第一轮"),
                make_response(content="清空后第一轮"),
            ]
        )
        engine = self._engine(client)
        engine.submit_message("第一轮")
        self.assertGreater(engine.message_count(), 0)
        engine.clear()
        self.assertEqual(engine.message_count(), 0)
        self.assertEqual(engine.stats.turns, 0)
        engine.submit_message("清空后第一轮")
        # 清空后的请求只有 system + user（模型响应的 assistant 在请求之后才 append）
        roles = [m.get("role") for m in client.requests[1]["messages"]]
        self.assertEqual(roles, ["system", "user"])

    def test_cache_hit_rate_grows_in_steady_state(self):
        """稳态会话（工具/目录不变）→ 第二轮起缓存命中。"""
        client = FakeClient(
            [
                make_response(content="ok1"),
                make_response(content="ok2"),
            ]
        )
        engine = self._engine(client)
        engine.submit_message("第一轮")
        rate_after_first = engine.cache_hit_rate()
        engine.submit_message("第二轮")
        self.assertLess(rate_after_first, 1.0)  # 首轮必有 miss
        self.assertGreater(engine.cache_hit_rate(), rate_after_first)
        self.assertGreaterEqual(engine.cache_hit_rate(), 0.5)

    @staticmethod
    def _make_extra_tool(name):
        from mini_agent.tools import Tool

        return Tool(
            name=name,
            description="测试注入的额外工具。",
            parameters={"type": "object", "properties": {}, "required": []},
            handler=lambda args: "ok",
            is_concurrency_safe=True,
            is_read_only=True,
        )

    def test_agent_run_backward_compatible(self):
        """Agent.run()（单次模式）行为不变：system + user + loop。"""
        client = FakeClient(
            [
                make_response(content="单次回答"),
            ]
        )
        agent = Agent(
            client=client,
            tools=self.tools,
            system_prompt="固定系统提示",
            verbose=False,
        )
        result = agent.run("单次任务")
        self.assertEqual(result.stop_reason, "completed")
        # 模型请求时只有 system + user；assistant 在响应后 append 进历史
        roles = [m.get("role") for m in client.requests[0]["messages"]]
        self.assertEqual(roles, ["system", "user"])
        # 循环结束后历史含 final assistant
        self.assertEqual(
            [m.get("role") for m in result.messages][-1], "assistant"
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
