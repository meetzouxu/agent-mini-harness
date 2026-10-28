"""迭代 3 单测：权限管线 —— 四态裁决、三道闸门、规则匹配、Denial Tracking、模式表、settings 加载。

运行：.venv/bin/python -m unittest discover -s tests -v
全部使用假客户端与内存规则，不依赖 API key 与真实交互。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mini_agent.agent import Agent  # noqa: E402
from mini_agent.permissions import (  # noqa: E402
    ANSWER_ALWAYS,
    ANSWER_NO,
    ANSWER_YES,
    Behavior,
    PermissionManager,
    PermissionRule,
    load_settings_rules,
)
from mini_agent.tools import default_tools  # noqa: E402

from tests.test_agent_loop import FakeClient, make_response, make_tool_call  # noqa: E402


def _tool_by_name(name: str):
    return {t.name: t for t in default_tools()}[name]


# ---------------------------------------------------------------------------
# 闸门顺序与四态裁决
# ---------------------------------------------------------------------------

class GateOrderTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_cwd = os.getcwd()
        os.chdir(self._tmp.name)

    def tearDown(self):
        os.chdir(self._old_cwd)
        self._tmp.cleanup()

    def test_gate0_blacklist_deny(self):
        """闸门 0：bash 黑名单直接 deny（优先于一切规则）。"""
        pm = PermissionManager(mode="bypass")  # 连 bypass 都拦不住黑名单
        result = pm.check(_tool_by_name("bash"), {"command": "sudo rm x"})
        self.assertIs(result.behavior, Behavior.DENY)
        self.assertIn("黑名单", result.reason)

    def test_gate0_path_fence_deny(self):
        """闸门 0：路径越界 deny（优先于 allow 规则）。"""
        pm = PermissionManager(
            mode="bypass",
            rules=[PermissionRule(tool="write_file", behavior=Behavior.ALLOW)],
        )
        result = pm.check(_tool_by_name("write_file"), {"path": "/etc/passwd", "content": "x"})
        self.assertIs(result.behavior, Behavior.DENY)
        self.assertIn("越界", result.reason)

    def test_deny_rule_beats_allow_rule(self):
        """deny 规则压过 allow 规则（闸门 1 在闸门 2 之前）。"""
        pm = PermissionManager(
            rules=[
                PermissionRule(tool="bash", behavior=Behavior.ALLOW),
                PermissionRule(tool="bash", behavior=Behavior.DENY),
            ]
        )
        result = pm.check(_tool_by_name("bash"), {"command": "ls"})
        self.assertIs(result.behavior, Behavior.DENY)

    def test_allow_rule_short_circuits(self):
        """allow 规则命中 → 放行（不再走默认 ask）。"""
        pm = PermissionManager(
            rules=[PermissionRule(tool="bash", behavior=Behavior.ALLOW, content="git *")]
        )
        result = pm.check(_tool_by_name("bash"), {"command": "git status"})
        self.assertIs(result.behavior, Behavior.ALLOW)
        # 未命中前缀 → 走默认行为（ask）
        result2 = pm.check(_tool_by_name("bash"), {"command": "curl example.com"})
        self.assertIs(result2.behavior, Behavior.ASK)

    def test_ask_rule_hits(self):
        pm = PermissionManager(
            rules=[PermissionRule(tool="bash", behavior=Behavior.ASK, content="rm *")]
        )
        result = pm.check(_tool_by_name("bash"), {"command": "rm -r build"})
        self.assertIs(result.behavior, Behavior.ASK)


# ---------------------------------------------------------------------------
# 规则匹配三维度
# ---------------------------------------------------------------------------

class RuleMatchTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_cwd = os.getcwd()
        os.chdir(self._tmp.name)
        (Path(self._tmp.name) / "src").mkdir()
        (Path(self._tmp.name) / "src" / "a.py").write_text("x=1\n", encoding="utf-8")

    def tearDown(self):
        os.chdir(self._old_cwd)
        self._tmp.cleanup()

    def test_tool_name_match(self):
        pm = PermissionManager(rules=[PermissionRule(tool="grep", behavior=Behavior.ALLOW)])
        self.assertIs(pm.check(_tool_by_name("grep"), {"pattern": "x"}).behavior, Behavior.ALLOW)
        self.assertIs(pm.check(_tool_by_name("bash"), {"command": "ls"}).behavior, Behavior.ASK)

    def test_tool_name_wildcard(self):
        pm = PermissionManager(rules=[PermissionRule(tool="*", behavior=Behavior.ALLOW, source="cli")])
        for name in ("read_file", "bash", "grep"):
            self.assertIs(pm.check(_tool_by_name(name), {"command": "ls", "path": "src"}).behavior, Behavior.ALLOW)

    def test_bash_command_prefix_match(self):
        pm = PermissionManager(
            rules=[PermissionRule(tool="bash", behavior=Behavior.ALLOW, content="git *")]
        )
        self.assertIs(
            pm.check(_tool_by_name("bash"), {"command": "git commit -m fix"}).behavior,
            Behavior.ALLOW,
        )
        self.assertIs(
            pm.check(_tool_by_name("bash"), {"command": "github-cli"}).behavior,
            Behavior.ASK,  # 前缀是 "git " 带空格，github-cli 不匹配
        )

    def test_bash_exact_match(self):
        pm = PermissionManager(
            rules=[PermissionRule(tool="bash", behavior=Behavior.ALLOW, content="git status")]
        )
        self.assertIs(pm.check(_tool_by_name("bash"), {"command": "git status"}).behavior, Behavior.ALLOW)
        self.assertIs(pm.check(_tool_by_name("bash"), {"command": "git status -s"}).behavior, Behavior.ASK)

    def test_path_glob_match(self):
        pm = PermissionManager(
            rules=[PermissionRule(tool="write_file", behavior=Behavior.ALLOW, content="src/**")]
        )
        self.assertIs(
            pm.check(_tool_by_name("write_file"), {"path": "src/a.py", "content": "y"}).behavior,
            Behavior.ALLOW,
        )
        self.assertIs(
            pm.check(_tool_by_name("write_file"), {"path": "docs/a.md", "content": "y"}).behavior,
            Behavior.ASK,
        )


# ---------------------------------------------------------------------------
# 权限模式表
# ---------------------------------------------------------------------------

class ModeTableTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_cwd = os.getcwd()
        os.chdir(self._tmp.name)

    def tearDown(self):
        os.chdir(self._old_cwd)
        self._tmp.cleanup()

    def test_default_mode(self):
        pm = PermissionManager(mode="default")
        self.assertIs(pm.check(_tool_by_name("read_file"), {"path": "x"}).behavior, Behavior.ALLOW)
        self.assertIs(pm.check(_tool_by_name("bash"), {"command": "ls"}).behavior, Behavior.ASK)
        self.assertIs(pm.check(_tool_by_name("write_file"), {"path": "x", "content": ""}).behavior, Behavior.ASK)

    def test_plan_mode(self):
        pm = PermissionManager(mode="plan")
        self.assertIs(pm.check(_tool_by_name("grep"), {"pattern": "x"}).behavior, Behavior.ALLOW)
        self.assertIs(pm.check(_tool_by_name("write_file"), {"path": "x", "content": ""}).behavior, Behavior.DENY)
        self.assertIs(pm.check(_tool_by_name("bash"), {"command": "ls"}).behavior, Behavior.DENY)

    def test_accept_edits_mode(self):
        pm = PermissionManager(mode="acceptEdits")
        self.assertIs(pm.check(_tool_by_name("edit_file"), {"path": "x", "old_string": "a", "new_string": "b"}).behavior, Behavior.ALLOW)
        self.assertIs(pm.check(_tool_by_name("write_file"), {"path": "x", "content": ""}).behavior, Behavior.ALLOW)
        self.assertIs(pm.check(_tool_by_name("bash"), {"command": "ls"}).behavior, Behavior.ASK)

    def test_bypass_mode(self):
        pm = PermissionManager(mode="bypass")
        self.assertIs(pm.check(_tool_by_name("bash"), {"command": "ls"}).behavior, Behavior.ALLOW)
        # 但黑名单仍然生效（闸门 0 是硬边界）
        self.assertIs(pm.check(_tool_by_name("bash"), {"command": "sudo x"}).behavior, Behavior.DENY)


# ---------------------------------------------------------------------------
# decide()：交互与 Always allow
# ---------------------------------------------------------------------------

class DecideTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_cwd = os.getcwd()
        os.chdir(self._tmp.name)

    def tearDown(self):
        os.chdir(self._old_cwd)
        self._tmp.cleanup()

    def test_ask_yes_allows_once(self):
        answers = []
        pm = PermissionManager(on_ask=lambda n, a, r: (answers.append(n), ANSWER_YES)[1])
        result = pm.decide(_tool_by_name("bash"), {"command": "ls"})
        self.assertIs(result.behavior, Behavior.ALLOW)
        self.assertEqual(answers, ["bash"])

    def test_ask_always_persists_session_rule(self):
        pm = PermissionManager(on_ask=lambda n, a, r: ANSWER_ALWAYS)
        result = pm.decide(_tool_by_name("bash"), {"command": "ls"})
        self.assertIs(result.behavior, Behavior.ALLOW)
        # 第二次同工具调用 → 不再询问（会话级规则命中）
        result2 = pm.decide(_tool_by_name("bash"), {"command": "ls -la"})
        self.assertIs(result2.behavior, Behavior.ALLOW)
        self.assertIn("session", result2.reason)  # 命中会话级 allow 规则

    def test_ask_no_denies(self):
        pm = PermissionManager(on_ask=lambda n, a, r: ANSWER_NO)
        result = pm.decide(_tool_by_name("bash"), {"command": "ls"})
        self.assertIs(result.behavior, Behavior.DENY)
        self.assertIn("用户拒绝", result.reason)

    def test_no_callback_denies_safely(self):
        """无交互通道 → 安全默认拒绝（--quiet 场景）。"""
        pm = PermissionManager()
        result = pm.decide(_tool_by_name("bash"), {"command": "ls"})
        self.assertIs(result.behavior, Behavior.DENY)
        self.assertIn("无交互通道", result.reason)

    def test_denial_tracking_hint(self):
        """同一工具连续 3 次拒绝 → 注入「改变策略」提示（死循环防护）。"""
        pm = PermissionManager(on_ask=lambda n, a, r: ANSWER_NO)
        reasons = []
        for _ in range(3):
            reasons.append(pm.decide(_tool_by_name("bash"), {"command": "ls"}).reason)
        self.assertIn("连续 3 次", reasons[-1])
        self.assertIn("改变策略", reasons[-1])
        # 成功一次 → 计数重置
        pm._consecutive.pop("bash", None)

    def test_denial_reset_on_success(self):
        pm = PermissionManager(on_ask=lambda n, a, r: ANSWER_NO)
        pm.decide(_tool_by_name("bash"), {"command": "ls"})  # 拒绝 1 次
        pm.decide(_tool_by_name("bash"), {"command": "ls"})  # 拒绝 2 次
        # 成功 → 重置
        pm._record_success("bash")
        r = pm.decide(_tool_by_name("bash"), {"command": "ls"})
        self.assertNotIn("连续 3 次", r.reason)


# ---------------------------------------------------------------------------
# settings.json 加载（分层配置）
# ---------------------------------------------------------------------------

class SettingsLoadTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_cwd = os.getcwd()
        os.chdir(self._tmp.name)

    def tearDown(self):
        os.chdir(self._old_cwd)
        self._tmp.cleanup()

    def test_missing_settings_returns_empty(self):
        self.assertEqual(load_settings_rules(Path.cwd()), [])

    def test_load_full_settings(self):
        settings_dir = Path(".mini-agent")
        settings_dir.mkdir()
        (settings_dir / "settings.json").write_text(
            json.dumps(
                {
                    "permissions": {
                        "allow": ["read_file", "bash:git *"],
                        "ask": ["bash:rm *"],
                        "deny": ["bash:sudo *"],
                    }
                }
            ),
            encoding="utf-8",
        )
        rules = load_settings_rules(Path.cwd())
        behaviors = {(r.tool, r.content): r.behavior for r in rules}
        self.assertIs(behaviors[("read_file", None)], Behavior.ALLOW)
        self.assertIs(behaviors[("bash", "git *")], Behavior.ALLOW)
        self.assertIs(behaviors[("bash", "rm *")], Behavior.ASK)
        self.assertIs(behaviors[("bash", "sudo *")], Behavior.DENY)

    def test_corrupted_settings_ignored(self):
        settings_dir = Path(".mini-agent")
        settings_dir.mkdir()
        (settings_dir / "settings.json").write_text("{not valid json", encoding="utf-8")
        self.assertEqual(load_settings_rules(Path.cwd()), [])


# ---------------------------------------------------------------------------
# Agent 集成：权限拒绝回填 + ask 串行化
# ---------------------------------------------------------------------------

class AgentIntegrationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_cwd = os.getcwd()
        os.chdir(self._tmp.name)
        (Path(self._tmp.name) / "hello.txt").write_text("hi\n", encoding="utf-8")

    def tearDown(self):
        os.chdir(self._old_cwd)
        self._tmp.cleanup()

    def test_denied_tool_fed_back_as_observation(self):
        """deny → [权限拒绝] 文本回填，模型改变策略，循环不崩。"""
        client = FakeClient(
            [
                make_response(tool_calls=[make_tool_call("c1", "bash", '{"command": "ls"}')]),
                make_response(content="被拒绝了，改用只读工具完成"),
            ]
        )
        pm = PermissionManager(on_ask=lambda n, a, r: ANSWER_NO)
        result = Agent(client=client, tools=default_tools(), verbose=False, permission_manager=pm).run("列目录")
        self.assertEqual(result.stop_reason, "completed")
        self.assertTrue(result.tool_events[0].is_error)
        self.assertIn("[权限拒绝]", result.tool_events[0].result)
        self.assertIn("用户拒绝", result.tool_events[0].result)

    def test_agent_without_permissions_unchanged(self):
        """未注入权限管理器 → 行为与迭代 2 完全一致（向后兼容）。"""
        client = FakeClient(
            [
                make_response(tool_calls=[make_tool_call("c1", "bash", '{"command": "echo ok"}')]),
                make_response(content="done"),
            ]
        )
        result = Agent(client=client, tools=default_tools(), verbose=False).run("跑命令")
        self.assertFalse(result.tool_events[0].is_error)
        self.assertIn("exit_code=0", result.tool_events[0].result)

    def test_blacklist_deny_in_agent(self):
        """黑名单 deny → 回填；模型读到后换命令。"""
        client = FakeClient(
            [
                make_response(tool_calls=[make_tool_call("c1", "bash", '{"command": "sudo ls"}')]),
                make_response(content="明白了，不能用 sudo"),
            ]
        )
        pm = PermissionManager(mode="bypass")  # 连 bypass 都拦黑名单
        result = Agent(client=client, tools=default_tools(), verbose=False, permission_manager=pm).run("试试 sudo")
        self.assertTrue(result.tool_events[0].is_error)
        self.assertIn("黑名单", result.tool_events[0].result)

    def test_plan_mode_denies_write_in_agent(self):
        """plan 模式：写操作 deny 回填。"""
        client = FakeClient(
            [
                make_response(tool_calls=[make_tool_call("c1", "write_file", '{"path": "out.txt", "content": "x"}')]),
                make_response(content="只读模式下无法写入，改为报告"),
            ]
        )
        pm = PermissionManager(mode="plan")
        result = Agent(client=client, tools=default_tools(), verbose=False, permission_manager=pm).run("写文件")
        self.assertTrue(result.tool_events[0].is_error)
        self.assertIn("plan 模式", result.tool_events[0].result)

    def test_ask_in_concurrent_batch_forces_serial(self):
        """并发批次中含 ask 工具 → 整批串行（交互不并发）。"""
        import time as _t

        from mini_agent.tools import Tool

        def slow_read(args):
            _t.sleep(0.15)
            return "done-read"

        tools = [
            Tool(
                name="slow_read",
                description="slow read",
                parameters={"type": "object", "properties": {}, "required": []},
                handler=slow_read,
                is_concurrency_safe=True,
                is_read_only=True,
            )
        ]
        # 两个 slow_read + 规则让其中一个命中 ask（rule content 需匹配… 无 path 参数，
        # 用工具名通配 + 无 content 匹配全部）
        client = FakeClient(
            [
                make_response(
                    tool_calls=[
                        make_tool_call("c1", "slow_read", "{}"),
                        make_tool_call("c2", "slow_read", "{}"),
                    ]
                ),
                make_response(content="done"),
            ]
        )
        ask_count = []
        pm = PermissionManager(
            rules=[PermissionRule(tool="slow_read", behavior=Behavior.ASK)],
            on_ask=lambda n, a, r: (ask_count.append(n), ANSWER_YES)[1],
        )
        result = Agent(client=client, tools=tools, verbose=False, permission_manager=pm).run("x")
        self.assertEqual(result.stop_reason, "completed")
        # 两个工具都走了交互（串行逐个询问）
        self.assertEqual(ask_count, ["slow_read", "slow_read"])
        self.assertFalse(result.tool_events[0].is_error)

    def test_allowed_concurrent_batch_stays_parallel(self):
        """全 allow 的并发批次仍并发（权限不拖慢只读路径）。"""
        import time as _t

        from mini_agent.tools import Tool

        def slow_read(args):
            _t.sleep(0.3)
            return "ok"

        tools = [
            Tool(
                name=f"slow_{i}",
                description=f"slow {i}",
                parameters={"type": "object", "properties": {}, "required": []},
                handler=slow_read,
                is_concurrency_safe=True,
                is_read_only=True,
            )
            for i in range(2)
        ]
        client = FakeClient(
            [
                make_response(
                    tool_calls=[
                        make_tool_call("c1", "slow_0", "{}"),
                        make_tool_call("c2", "slow_1", "{}"),
                    ]
                ),
                make_response(content="done"),
            ]
        )
        pm = PermissionManager(mode="default")  # 只读默认 allow
        started = _t.perf_counter()
        result = Agent(client=client, tools=tools, verbose=False, permission_manager=pm).run("x")
        elapsed = _t.perf_counter() - started
        self.assertLess(elapsed, 0.5, f"耗时 {elapsed:.2f}s，权限不应拖慢并发批次")
        self.assertEqual(result.tool_calls_count, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
