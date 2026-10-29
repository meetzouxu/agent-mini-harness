"""迭代 4 单测：Hooks 扩展点 —— 注册表、匹配、结果合并、四触发点、Stop 续跑与防循环。

运行：.venv/bin/python -m unittest discover -s tests -v
全部使用假客户端与内存 hook，不依赖 API key。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mini_agent.agent import Agent  # noqa: E402
from mini_agent.hooks import (  # noqa: E402
    EVENT_POST_TOOL_USE,
    EVENT_PRE_TOOL_USE,
    EVENT_STOP,
    EVENT_USER_PROMPT_SUBMIT,
    Hook,
    HookRegistry,
    HookResult,
)
from mini_agent.tools import default_tools  # noqa: E402

from tests.test_agent_loop import FakeClient, make_response, make_tool_call  # noqa: E402


# ---------------------------------------------------------------------------
# 注册表与匹配
# ---------------------------------------------------------------------------

class RegistryTests(unittest.TestCase):
    def test_register_unknown_event_rejected(self):
        registry = HookRegistry()
        with self.assertRaises(ValueError):
            registry.register(
                Hook(event="NotAnEvent", name="x", handler=lambda p: HookResult())
            )

    def test_unregister(self):
        registry = HookRegistry()
        registry.register(
            Hook(event=EVENT_PRE_TOOL_USE, name="h1", handler=lambda p: HookResult())
        )
        self.assertTrue(registry.unregister("h1"))
        self.assertFalse(registry.unregister("h1"))

    def test_matcher_exact_pipe_wildcard(self):
        registry = HookRegistry()
        registry.register(
            Hook(event=EVENT_PRE_TOOL_USE, name="exact", handler=lambda p: HookResult(), matcher="bash")
        )
        registry.register(
            Hook(event=EVENT_PRE_TOOL_USE, name="pipe", handler=lambda p: HookResult(), matcher="write_file|edit_file")
        )
        registry.register(
            Hook(event=EVENT_PRE_TOOL_USE, name="wild", handler=lambda p: HookResult(), matcher="*")
        )
        self.assertEqual(
            [h.name for h in registry.get_matching(EVENT_PRE_TOOL_USE, "bash")],
            ["exact", "wild"],
        )
        self.assertEqual(
            [h.name for h in registry.get_matching(EVENT_PRE_TOOL_USE, "edit_file")],
            ["pipe", "wild"],
        )
        self.assertEqual(
            [h.name for h in registry.get_matching(EVENT_PRE_TOOL_USE, "grep")],
            ["wild"],
        )

    def test_matcher_ignored_for_non_tool_events(self):
        registry = HookRegistry()
        registry.register(
            Hook(event=EVENT_STOP, name="onstop", handler=lambda p: HookResult(), matcher="bash")
        )
        # Stop 事件不按工具过滤（matcher 不生效）
        self.assertEqual(len(registry.get_matching(EVENT_STOP)), 1)

    def test_hook_exception_does_not_break(self):
        """hook 抛异常 → system_message 警告，不抛出。"""
        registry = HookRegistry()

        def boom(payload):
            raise RuntimeError("hook 内部错误")

        registry.register(Hook(event=EVENT_PRE_TOOL_USE, name="boom", handler=boom))
        result = registry.trigger(EVENT_PRE_TOOL_USE, {"tool_name": "bash"})
        self.assertIn("hook boom 执行异常", result.system_message)
        self.assertTrue(result.continue_)  # 异常不打断


# ---------------------------------------------------------------------------
# 结果合并语义
# ---------------------------------------------------------------------------

class MergeTests(unittest.TestCase):
    def _registry_with(self, *results):
        registry = HookRegistry()
        for i, r in enumerate(results):
            registry.register(
                Hook(event=EVENT_PRE_TOOL_USE, name=f"h{i}", handler=lambda p, r=r: r)
            )
        return registry

    def test_strictest_behavior_wins(self):
        registry = self._registry_with(
            HookResult.allow(),
            HookResult.deny("bad"),
            HookResult.ask("?"),
        )
        merged = registry.trigger(EVENT_PRE_TOOL_USE, {"tool_name": "bash"})
        self.assertEqual(merged.permission_behavior, "deny")

    def test_last_updated_input_wins(self):
        registry = self._registry_with(
            HookResult(updated_input={"command": "first"}),
            HookResult(updated_input={"command": "second"}),
        )
        merged = registry.trigger(EVENT_PRE_TOOL_USE, {"tool_name": "bash"})
        self.assertEqual(merged.updated_input, {"command": "second"})

    def test_additional_context_concatenated(self):
        registry = self._registry_with(
            HookResult(additional_context="注意 A"),
            HookResult(additional_context="注意 B"),
        )
        merged = registry.trigger(EVENT_PRE_TOOL_USE, {"tool_name": "bash"})
        self.assertIn("注意 A", merged.additional_context)
        self.assertIn("注意 B", merged.additional_context)

    def test_any_continue_false_blocks(self):
        registry = self._registry_with(
            HookResult(continue_=True),
            HookResult(continue_=False, stop_reason="拦截理由"),
        )
        merged = registry.trigger(EVENT_PRE_TOOL_USE, {"tool_name": "bash"})
        self.assertFalse(merged.continue_)
        self.assertEqual(merged.stop_reason, "拦截理由")

    def test_first_stop_reason_wins(self):
        registry = self._registry_with(
            HookResult(continue_=False, stop_reason="第一个"),
            HookResult(continue_=False, stop_reason="第二个"),
        )
        merged = registry.trigger(EVENT_PRE_TOOL_USE, {"tool_name": "bash"})
        self.assertEqual(merged.stop_reason, "第一个")


# ---------------------------------------------------------------------------
# Agent 集成：四个触发点
# ---------------------------------------------------------------------------

class AgentHookIntegrationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_cwd = os.getcwd()
        os.chdir(self._tmp.name)
        (Path(self._tmp.name) / "hello.txt").write_text("hi\n", encoding="utf-8")
        self.registry = HookRegistry()

    def tearDown(self):
        os.chdir(self._old_cwd)
        self._tmp.cleanup()

    def _agent(self, client, **kw):
        return Agent(
            client=client,
            tools=default_tools(),
            verbose=False,
            hooks=self.registry,
            **kw,
        )

    def test_user_prompt_submit_injects_context(self):
        """UserPromptSubmit 注入上下文 → 模型请求里多一条 user 消息。"""
        calls = []

        def inject(payload):
            calls.append(payload["prompt"])
            return HookResult(additional_context="[系统提示] 当前目录下有 hello.txt")

        self.registry.register(
            Hook(event=EVENT_USER_PROMPT_SUBMIT, name="ctx", handler=inject)
        )
        client = FakeClient(
            [
                make_response(tool_calls=[make_tool_call("c1", "read_file", '{"path": "hello.txt"}')]),
                make_response(content="done"),
            ]
        )
        result = self._agent(client).run("读 hello.txt")
        self.assertEqual(result.stop_reason, "completed")
        self.assertEqual(calls, ["读 hello.txt"])
        # 第一次模型请求 = system + user 任务 + user 注入（3 条起手）
        first_req = client.requests[0]
        user_msgs = [m for m in first_req["messages"] if m.get("role") == "user"]
        self.assertEqual(len(user_msgs), 2)
        self.assertIn("hello.txt", user_msgs[1]["content"])

    def test_user_prompt_submit_can_block_task(self):
        """UserPromptSubmit continue_=False → 任务拦截（stop_reason=hook_blocked）。"""
        self.registry.register(
            Hook(
                event=EVENT_USER_PROMPT_SUBMIT,
                name="gate",
                handler=lambda p: HookResult(continue_=False, stop_reason="包含危险指令"),
            )
        )
        client = FakeClient([])
        result = self._agent(client).run("危险任务")
        self.assertEqual(result.stop_reason, "hook_blocked")
        self.assertIn("危险指令", result.final_text)
        self.assertEqual(len(client.requests), 0)  # 模型一次都没被调

    def test_pre_tool_use_blocks_tool(self):
        """PreToolUse continue_=False → 工具拦截，错误回填。"""
        self.registry.register(
            Hook(
                event=EVENT_PRE_TOOL_USE,
                name="blocker",
                handler=lambda p: HookResult(continue_=False, stop_reason="此工具被 hook 禁用"),
                matcher="bash",
            )
        )
        client = FakeClient(
            [
                make_response(tool_calls=[make_tool_call("c1", "bash", '{"command": "ls"}')]),
                make_response(content="被拦截，改用 list_dir"),
            ]
        )
        result = self._agent(client).run("跑命令")
        self.assertTrue(result.tool_events[0].is_error)
        self.assertIn("[PreToolUse 拦截]", result.tool_events[0].result)
        self.assertIn("此工具被 hook 禁用", result.tool_events[0].result)

    def test_pre_tool_use_updates_input(self):
        """PreToolUse updated_input → 参数被改写（加 --bail 的场景）。"""
        seen = []

        def rewrite(payload):
            seen.append(payload["arguments"]["command"])
            return HookResult(updated_input={"command": "echo rewritten"})

        self.registry.register(
            Hook(event=EVENT_PRE_TOOL_USE, name="rewriter", handler=rewrite, matcher="bash")
        )
        client = FakeClient(
            [
                make_response(tool_calls=[make_tool_call("c1", "bash", '{"command": "echo original"}')]),
                make_response(content="done"),
            ]
        )
        result = self._agent(client).run("跑命令")
        self.assertFalse(result.tool_events[0].is_error)
        self.assertEqual(seen, ["echo original"])
        self.assertIn("rewritten", result.tool_events[0].result)

    def test_post_tool_use_updates_output(self):
        """PostToolUse updated_output → 回填给模型的文本被改写。"""
        self.registry.register(
            Hook(
                event=EVENT_POST_TOOL_USE,
                name="sanitizer",
                handler=lambda p: HookResult(updated_output="[已脱敏] ***"),
                matcher="read_file",
            )
        )
        client = FakeClient(
            [
                make_response(tool_calls=[make_tool_call("c1", "read_file", '{"path": "hello.txt"}')]),
                make_response(content="done"),
            ]
        )
        result = self._agent(client).run("读文件")
        self.assertIn("[已脱敏] ***", result.tool_events[0].result)
        # 改写后的文本真的回填给了模型
        second_req = client.requests[1]
        tool_msgs = [m for m in second_req["messages"] if m.get("role") == "tool"]
        self.assertIn("[已脱敏] ***", tool_msgs[0]["content"])

    def test_post_tool_use_additional_context_appended(self):
        self.registry.register(
            Hook(
                event=EVENT_POST_TOOL_USE,
                name="advisor",
                handler=lambda p: HookResult(additional_context="建议检查返回码"),
            )
        )
        client = FakeClient(
            [
                make_response(tool_calls=[make_tool_call("c1", "read_file", '{"path": "hello.txt"}')]),
                make_response(content="done"),
            ]
        )
        result = self._agent(client).run("读文件")
        self.assertIn("[hook 上下文] 建议检查返回码", result.tool_events[0].result)

    def test_stop_hook_force_continue_once(self):
        """Stop hook force_continue → 注入消息续跑一次；模型再停 → 真正完成。"""
        stop_events = []

        def nudge(payload):
            stop_events.append(payload["final_text"])
            if len(stop_events) == 1:
                return HookResult(force_continue="请再检查一遍结果是否完整")
            return HookResult()

        self.registry.register(Hook(event=EVENT_STOP, name="nudge", handler=nudge))
        client = FakeClient(
            [
                make_response(content="第一版答案"),          # 停 → hook 续跑
                make_response(content="检查过了，最终答案"),   # 再停 → 完成
            ]
        )
        result = self._agent(client).run("做一件事")
        self.assertEqual(result.stop_reason, "completed")
        self.assertEqual(len(stop_events), 2)
        self.assertEqual(result.final_text, "检查过了，最终答案")
        # 续跑注入的消息真的进了对话
        second_req = client.requests[1]
        user_msgs = [m for m in second_req["messages"] if m.get("role") == "user"]
        self.assertIn("请再检查一遍", user_msgs[-1]["content"])

    def test_stop_hook_no_infinite_loop(self):
        """Stop hook 永远返回 force_continue → 只续跑一次（护栏），不死循环。"""
        self.registry.register(
            Hook(
                event=EVENT_STOP,
                name="always",
                handler=lambda p: HookResult(force_continue="再来一遍！"),
            )
        )
        client = FakeClient(
            [
                make_response(content="答案 1"),
                make_response(content="答案 2"),  # 护栏生效后这次 stop 不再续跑 → 完成
            ]
        )
        result = self._agent(client).run("任务")
        self.assertEqual(result.stop_reason, "completed")
        # 恰好 2 次模型调用（初始 + 1 次续跑），不是无限 —— 护栏生效
        self.assertEqual(len(client.requests), 2)
        self.assertEqual(result.final_text, "答案 2")


# ---------------------------------------------------------------------------
# hook 与权限的融合
# ---------------------------------------------------------------------------

class HookPermissionFusionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_cwd = os.getcwd()
        os.chdir(self._tmp.name)
        self.registry = HookRegistry()

    def tearDown(self):
        os.chdir(self._old_cwd)
        self._tmp.cleanup()

    def _agent(self, client, permissions):
        return Agent(
            client=client,
            tools=default_tools(),
            verbose=False,
            hooks=self.registry,
            permission_manager=permissions,
        )

    def test_hook_allow_cannot_override_deny_rule(self):
        """hook allow 不能凌驾 deny 规则（CC 同款：业务语义 vs 系统边界）。"""
        from mini_agent.permissions import ANSWER_NO, Behavior, PermissionManager, PermissionRule

        self.registry.register(
            Hook(
                event=EVENT_PRE_TOOL_USE,
                name="over",
                handler=lambda p: HookResult.allow(),
                matcher="bash",
            )
        )
        pm = PermissionManager(
            rules=[PermissionRule(tool="bash", behavior=Behavior.DENY, content="curl *")],
        )
        client = FakeClient(
            [
                make_response(tool_calls=[make_tool_call("c1", "bash", '{"command": "curl x"}')]),
                make_response(content="无法执行"),
            ]
        )
        result = self._agent(client, pm).run("curl")
        self.assertTrue(result.tool_events[0].is_error)
        self.assertIn("deny 规则", result.tool_events[0].result)

    def test_hook_deny_overrides_rule_allow(self):
        """hook deny 可以加严：压过 allow 规则。"""
        from mini_agent.permissions import Behavior, PermissionManager, PermissionRule

        self.registry.register(
            Hook(
                event=EVENT_PRE_TOOL_USE,
                name="guard",
                handler=lambda p: HookResult.deny("hook 禁止写 .env"),
                matcher="write_file",
            )
        )
        pm = PermissionManager(
            rules=[PermissionRule(tool="write_file", behavior=Behavior.ALLOW)],
        )
        client = FakeClient(
            [
                make_response(
                    tool_calls=[make_tool_call("c1", "write_file", '{"path": ".env", "content": "k=v"}')]
                ),
                make_response(content="被拒"),
            ]
        )
        result = self._agent(client, pm).run("写 env")
        self.assertTrue(result.tool_events[0].is_error)
        self.assertIn("hook 禁止写 .env", result.tool_events[0].result)
        # 文件未被写入
        self.assertFalse((Path.cwd() / ".env").exists())

    def test_hook_allow_skips_ask_interaction(self):
        """hook allow 免除询问：本应 ask 的操作不再触发交互。"""
        from mini_agent.permissions import PermissionManager

        ask_calls = []
        pm = PermissionManager(on_ask=lambda n, a, r: (ask_calls.append(n), "no")[1])
        self.registry.register(
            Hook(
                event=EVENT_PRE_TOOL_USE,
                name="fast",
                handler=lambda p: HookResult.allow(),
                matcher="write_file",
            )
        )
        client = FakeClient(
            [
                make_response(tool_calls=[make_tool_call("c1", "write_file", '{"path": "ok.txt", "content": "x"}')]),
                make_response(content="done"),
            ]
        )
        result = self._agent(client, pm).run("写文件")
        self.assertEqual(ask_calls, [])  # 免询问
        self.assertFalse(result.tool_events[0].is_error)
        self.assertTrue((Path.cwd() / "ok.txt").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
