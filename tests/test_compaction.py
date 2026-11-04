"""迭代 6 单测：上下文压缩 —— 四层管线、配对不变量、熔断、集成。

运行：.venv/bin/python -m unittest discover -s tests -v
全部使用小窗口配置与假 summarizer，不依赖 API key。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mini_agent.compaction import (  # noqa: E402
    COMPACT_BOUNDARY_PREFIX,
    MICRO_PLACEHOLDER,
    SNIP_PLACEHOLDER_PREFIX,
    CompactionConfig,
    auto_compact,
    budget_tool_result,
    emergency_compact,
    estimate_tokens,
    get_messages_after_boundary,
    micro_compact,
    preserve_api_invariants,
    run_pipeline,
    snip_compact,
)
from mini_agent.agent import Agent  # noqa: E402
from mini_agent.prompt import default_registry  # noqa: E402
from mini_agent.session import QueryEngine  # noqa: E402
from mini_agent.tools import default_tools  # noqa: E402

from tests.test_agent_loop import FakeClient, make_response, make_tool_call  # noqa: E402


def _small_cfg(**kw) -> CompactionConfig:
    """小窗口配置：让各层阈值容易触达。"""
    defaults = dict(
        context_window_tokens=1000,
        snip_message_threshold=12,
        snip_keep_head=2,
        snip_keep_tail=3,
        micro_keep_recent=1,
        auto_keep_tail=2,
    )
    defaults.update(kw)
    return CompactionConfig(**defaults)


def _mk_msgs(n_pairs: int = 0, long_tool_results: bool = False) -> list[dict]:
    """构造会话消息：system + 交替的 (user, assistant) + 可选工具对。"""
    msgs = [{"role": "system", "content": "sys"}]
    for i in range(n_pairs):
        msgs.append({"role": "user", "content": f"问题 {i}" * 10})
        msgs.append({"role": "assistant", "content": f"回答 {i}" * 10})
    if long_tool_results:
        msgs.append({"role": "user", "content": "读大文件"})
        msgs.append(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": '{"path": "big.txt"}'},
                    }
                ],
            }
        )
        msgs.append(
            {
                "role": "tool",
                "tool_call_id": "c1",
                "name": "read_file",
                "content": "x" * 3000,  # 3000 字符 ≈ 750 token（小窗口下显著）
            }
        )
    return msgs


# ---------------------------------------------------------------------------
# L1 budget：大结果落盘
# ---------------------------------------------------------------------------

class BudgetTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_cwd = os.getcwd()
        os.chdir(self._tmp.name)
        self.cfg = _small_cfg()

    def tearDown(self):
        os.chdir(self._old_cwd)
        self._tmp.cleanup()

    def test_large_result_persisted_to_disk(self):
        content = "重要数据\n" * 1000  # 5000 字符 > 4000 阈值
        out = budget_tool_result(content, "read_file", self.cfg)
        self.assertTrue(out.startswith("[工具结果过大"))
        self.assertIn(".compact-cache", out)
        # 落盘文件存在且完整
        cache_files = list(Path(".compact-cache").glob("read_file-*.txt"))
        self.assertEqual(len(cache_files), 1)
        self.assertEqual(cache_files[0].read_text(), content)
        self.assertIn("开头预览", out)

    def test_small_result_untouched(self):
        out = budget_tool_result("短结果", "read_file", self.cfg)
        self.assertEqual(out, "短结果")

    def test_idempotent_on_placeholder(self):
        once = budget_tool_result("长" * 5000, "read_file", self.cfg)
        twice = budget_tool_result(once, "read_file", self.cfg)
        self.assertEqual(once, twice)  # 占位不再重复处理


# ---------------------------------------------------------------------------
# 配对不变量保护
# ---------------------------------------------------------------------------

class InvariantTests(unittest.TestCase):
    def test_orphan_tool_message_removed(self):
        msgs = [
            {"role": "system", "content": "s"},
            {"role": "tool", "tool_call_id": "ghost", "name": "read_file", "content": "孤儿"},
            {"role": "user", "content": "hi"},
        ]
        out = preserve_api_invariants(msgs)
        self.assertEqual(len(out), 2)  # 孤儿 tool 被删

    def test_orphan_tool_call_stripped_from_assistant(self):
        msgs = [
            {"role": "system", "content": "s"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}},
                    {"id": "c2", "type": "function", "function": {"name": "bash", "arguments": "{}"}},
                ],
            },
            {"role": "tool", "tool_call_id": "c1", "name": "read_file", "content": "r1"},
        ]
        out = preserve_api_invariants(msgs)
        # c2 无 tool 响应 → 剔除；assistant 仍有 c1
        assistant = [m for m in out if m.get("role") == "assistant"][0]
        self.assertEqual([tc["id"] for tc in assistant["tool_calls"]], ["c1"])

    def test_intact_pairs_untouched(self):
        msgs = _mk_msgs(n_pairs=1, long_tool_results=True)
        out = preserve_api_invariants(msgs)
        self.assertEqual(out, msgs)  # 完整配对原样


# ---------------------------------------------------------------------------
# L2 snip：中段折叠
# ---------------------------------------------------------------------------

class SnipTests(unittest.TestCase):
    def test_below_threshold_noop(self):
        cfg = _small_cfg()
        msgs = _mk_msgs(n_pairs=4)  # 9 条 < 12
        out, folded = snip_compact(msgs, cfg)
        self.assertEqual(folded, 0)
        self.assertEqual(out, msgs)

    def test_fold_keeps_head_and_tail(self):
        cfg = _small_cfg()
        msgs = _mk_msgs(n_pairs=10)  # 21 条 > 12
        out, folded = snip_compact(msgs, cfg)
        # 头 2 + 占位 1 + 尾 3 = 6 条
        self.assertEqual(len(out), 6)
        self.assertEqual(folded, 21 - 2 - 3)
        self.assertTrue(out[2]["content"].startswith(SNIP_PLACEHOLDER_PREFIX))
        self.assertEqual(out[0], msgs[0])       # 头保留
        self.assertEqual(out[-1], msgs[-1])     # 尾保留

    def test_fold_boundary_not_splitting_pair(self):
        """刀口落在 assistant(tool_calls)|tool 之间 → 整组一起折叠。"""
        cfg = _small_cfg()
        msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
        # 位置 2 放 assistant(tool_calls)，其后 tool —— keep_head=2 意味着折叠从 2 开始
        msgs.append(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "cA", "type": "function",
                                "function": {"name": "read_file", "arguments": "{}"}}],
            }
        )
        msgs.append({"role": "tool", "tool_call_id": "cA", "name": "read_file", "content": "r"})
        for i in range(8):
            msgs.append({"role": "user", "content": f"q{i}"})
        # 共 12 条 + ... 用 n_pairs 补到 > 12
        msgs.append({"role": "assistant", "content": "end"})
        out, folded = snip_compact(msgs, cfg)
        # 配对不拆：out 里若 tool cA 被保留则 assistant 也保留；若折叠则一起消失
        if any(m.get("tool_call_id") == "cA" for m in out):
            asst = [m for m in out if m.get("role") == "assistant" and any(
                tc.get("id") == "cA" for tc in m.get("tool_calls") or [])]
            self.assertEqual(len(asst), 1)
        else:
            asst_gone = not any(
                any(tc.get("id") == "cA" for tc in m.get("tool_calls") or []) for m in out
            )
            self.assertTrue(asst_gone)  # 整组一起折掉
        # 折叠后经不变量校验无孤儿
        self.assertEqual(preserve_api_invariants(out), out)


# ---------------------------------------------------------------------------
# L3 micro：旧工具结果占位
# ---------------------------------------------------------------------------

class MicroTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_cwd = os.getcwd()
        os.chdir(self._tmp.name)

    def tearDown(self):
        os.chdir(self._old_cwd)
        self._tmp.cleanup()

    def test_old_tool_result_cleared_recent_kept(self):
        cfg = _small_cfg()  # micro 阈值 = 500 token
        msgs = [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "q1" * 300},  # 600 字符 ≈ 150 token
        ]
        # 两个旧工具对 + 一个新工具对
        for cid in ("old1", "old2", "new1"):
            msgs.append({
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": cid, "type": "function",
                                "function": {"name": "read_file", "arguments": "{}"}}],
            })
            msgs.append({"role": "tool", "tool_call_id": cid, "name": "read_file",
                         "content": "数据" * 500})  # 每条 1000 字符
        out, cleared = micro_compact(msgs, cfg)
        self.assertGreater(cleared, 0)
        contents = {m.get("tool_call_id"): m.get("content") for m in out
                    if m.get("role") == "tool"}
        self.assertTrue(contents["new1"].startswith("数据"))  # 最近的保留
        self.assertTrue(contents["old1"].startswith(MICRO_PLACEHOLDER))
        # 消息条数不变（只清内容不删消息）
        self.assertEqual(len(out), len(msgs))

    def test_below_threshold_noop(self):
        cfg = _small_cfg(micro_threshold_ratio=100.0)  # 阈值拉满
        msgs = _mk_msgs(n_pairs=2, long_tool_results=True)
        out, cleared = micro_compact(msgs, cfg)
        self.assertEqual(cleared, 0)
        self.assertEqual(out, msgs)


# ---------------------------------------------------------------------------
# L4 auto / reactive
# ---------------------------------------------------------------------------

class AutoCompactTests(unittest.TestCase):
    def test_auto_compact_structure(self):
        cfg = _small_cfg()
        msgs = _mk_msgs(n_pairs=8, long_tool_results=True)
        summarize_inputs = []

        def fake_summarize(text: str) -> str:
            summarize_inputs.append(text)
            return "1. 用户读了多个文件；2. 关键结论 X"

        out, summary = auto_compact(msgs, fake_summarize, cfg)
        self.assertEqual(summary, "1. 用户读了多个文件；2. 关键结论 X")
        self.assertTrue(summarize_inputs and "回答 0" in summarize_inputs[0])
        # 结构：system + boundary + 尾部 keep_tail=2
        self.assertEqual(out[0]["role"], "system")
        self.assertTrue(out[1]["content"].startswith(COMPACT_BOUNDARY_PREFIX))
        self.assertIn("type=auto", out[1]["content"])
        self.assertEqual(len(out), 1 + 1 + 2)
        self.assertEqual(out[-1], msgs[-1])  # 尾部原样

    def test_emergency_compact_never_fails(self):
        cfg = _small_cfg()
        msgs = _mk_msgs(n_pairs=5, long_tool_results=True)
        out = emergency_compact(msgs, cfg)
        self.assertTrue(out[1]["content"].startswith(COMPACT_BOUNDARY_PREFIX))
        self.assertIn("type=reactive", out[1]["content"])

    def test_boundary_split(self):
        msgs = [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "旧"},
            {"role": "user", "content": f"{COMPACT_BOUNDARY_PREFIX} type=manual] 摘要"},
            {"role": "user", "content": "新"},
        ]
        after = get_messages_after_boundary(msgs)
        self.assertEqual([m["content"] for m in after], ["新"])


# ---------------------------------------------------------------------------
# 管线集成：触发顺序与熔断
# ---------------------------------------------------------------------------

class PipelineTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_cwd = os.getcwd()
        os.chdir(self._tmp.name)

    def tearDown(self):
        os.chdir(self._old_cwd)
        self._tmp.cleanup()

    def test_pipeline_snip_micro_only_below_auto(self):
        """未到 auto 阈值 → 只触发 snip/micro，不调摘要。"""
        cfg = _small_cfg(auto_threshold_ratio=10.0)  # auto 阈值 10000 → 不触发
        msgs = _mk_msgs(n_pairs=10)
        out, report = run_pipeline(msgs, cfg, summarize=lambda t: "不应被调用")
        self.assertFalse(report.auto_triggered)
        self.assertGreater(report.snip_folded, 0)

    def test_pipeline_auto_triggers_and_saves(self):
        cfg = _small_cfg()  # auto 阈值 750
        msgs = _mk_msgs(n_pairs=10, long_tool_results=True)
        out, report = run_pipeline(msgs, cfg, summarize=lambda t: "要点摘要")
        self.assertTrue(report.auto_triggered)
        self.assertLess(report.post_tokens, report.pre_tokens)
        self.assertLess(estimate_tokens(out), estimate_tokens(msgs))

    def test_auto_fuse_after_max_attempts(self):
        cfg = _small_cfg(auto_max_attempts=1)
        msgs = _mk_msgs(n_pairs=10, long_tool_results=True)
        # attempts=1 ≥ max=1 → 熔断，不调摘要
        out, report = run_pipeline(msgs, cfg, summarize=lambda t: "不应被调用", auto_attempts=1)
        self.assertTrue(report.fused)
        self.assertFalse(report.auto_triggered)

    def test_reactive_after_fuse_when_over_hard_limit(self):
        cfg = _small_cfg(auto_max_attempts=0, hard_limit_ratio=0.001)
        msgs = _mk_msgs(n_pairs=10, long_tool_results=True)
        out, report = run_pipeline(msgs, cfg, summarize=lambda t: "不应被调用", auto_attempts=0)
        self.assertTrue(report.fused)
        self.assertTrue(report.reactive_triggered)
        self.assertTrue(any(
            str(m.get("content", "")).startswith(COMPACT_BOUNDARY_PREFIX) for m in out
        ))

    def test_summarizer_none_no_auto(self):
        cfg = _small_cfg()
        msgs = _mk_msgs(n_pairs=10, long_tool_results=True)
        out, report = run_pipeline(msgs, cfg, summarize=None)
        self.assertFalse(report.auto_triggered)  # 无摘要器 → 依赖 snip/micro


# ---------------------------------------------------------------------------
# QueryEngine 集成
# ---------------------------------------------------------------------------

class QueryEngineCompactionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_cwd = os.getcwd()
        os.chdir(self._tmp.name)
        (Path(self._tmp.name) / "hello.txt").write_text("hi\n", encoding="utf-8")
        self.tools = default_tools()

    def tearDown(self):
        os.chdir(self._old_cwd)
        self._tmp.cleanup()

    def _engine(self, client, **cfg_kw):
        agent = Agent(client=client, tools=self.tools, verbose=False)
        registry = default_registry(self.tools)
        return QueryEngine(
            agent, registry,
            compaction_config=_small_cfg(**cfg_kw),
            summarizer=lambda t: "摘要：完成了任务",  # 注入假摘要器（不烧 key）
        )

    def test_long_session_gets_compacted(self):
        """小窗口下多轮会话 → auto 压缩触发，后续请求消息变少。"""
        responses = []
        for i in range(4):
            responses.append(make_response(content=f"第 {i} 轮回答" * 100))
        client = FakeClient(responses)
        engine = self._engine(client)
        for i in range(4):
            engine.submit_message(f"问题 {i}" * 200)
        self.assertGreater(engine.stats.compactions, 0)
        self.assertGreater(engine.stats.tokens_saved, 0)
        # 压缩后的消息里存在 boundary
        self.assertTrue(any(
            str(m.get("content", "")).startswith(COMPACT_BOUNDARY_PREFIX)
            for m in engine.messages
        ))

    def test_agent_budget_on_tool_result_backfill(self):
        """工具返回超 4000 字符 → 回填的是占位（含落盘路径），文件落盘。"""
        client = FakeClient(
            [
                make_response(
                    tool_calls=[
                        make_tool_call(
                            "c1", "bash",
                            '{"command": "python3 -c \'print(\\"x\\"*5000)\'"}',
                        )
                    ]
                ),
                make_response(content="done"),
            ]
        )
        agent = Agent(client=client, tools=self.tools, verbose=False)
        result = agent.run("生成大输出")
        tool_msg = [m for m in result.messages if m.get("role") == "tool"][0]
        self.assertIn("[工具结果过大", tool_msg["content"])
        self.assertIn(".compact-cache", tool_msg["content"])
        # 落盘文件存在且含完整输出
        cache_files = list(Path(".compact-cache").glob("bash-*.txt"))
        self.assertEqual(len(cache_files), 1)

    def test_compact_now_manual(self):
        client = FakeClient(
            [
                make_response(content="第一轮回答"),
                make_response(content="第二轮回答"),
            ]
        )
        received = []

        def recording_summarize(text: str) -> str:
            received.append(text)
            return "摘要：完成了任务"

        agent = Agent(client=client, tools=self.tools, verbose=False)
        engine = QueryEngine(
            agent, default_registry(self.tools),
            compaction_config=_small_cfg(auto_threshold_ratio=10.0),
            summarizer=recording_summarize,
        )
        engine.submit_message("第一轮")
        engine.submit_message("第二轮")
        report = engine.compact_now("聚焦在文件操作")
        after = engine.message_count()
        self.assertLessEqual(after, 1 + 1 + _small_cfg().auto_keep_tail)
        self.assertTrue(
            str(engine.messages[1]["content"]).startswith(COMPACT_BOUNDARY_PREFIX)
        )
        self.assertIn("type=manual", engine.messages[1]["content"])
        # 自定义聚焦指令随摘要请求下发（对应 CC /compact <指令>）
        self.assertTrue(any("聚焦在文件操作" in t for t in received))


if __name__ == "__main__":
    unittest.main(verbosity=2)
