"""迭代 8 单测：JSONL transcript —— 落盘/恢复/崩溃容错/续写。

运行：.venv/bin/python -m unittest discover -s tests -v
全部使用临时目录与假客户端，不依赖 API key。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mini_agent.agent import Agent  # noqa: E402
from mini_agent.compaction import preserve_api_invariants  # noqa: E402
from mini_agent.compaction import CompactionConfig  # noqa: E402
from mini_agent.prompt import default_registry  # noqa: E402
from mini_agent.session import QueryEngine  # noqa: E402
from mini_agent.tools import default_tools  # noqa: E402
from mini_agent.transcript import (  # noqa: E402
    TranscriptWriter,
    list_sessions,
    load_transcript,
    resolve_resume_target,
)

from tests.test_agent_loop import FakeClient, make_response, make_tool_call  # noqa: E402


# ---------------------------------------------------------------------------
# 写入器与文件格式
# ---------------------------------------------------------------------------

class WriterTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_cwd = os.getcwd()
        os.chdir(self._tmp.name)
        self.writer = TranscriptWriter(Path("sessions"))

    def tearDown(self):
        os.chdir(self._old_cwd)
        self._tmp.cleanup()

    def test_new_session_writes_meta_first_line(self):
        meta = self.writer.new_session(model="deepseek-chat")
        path = Path("sessions") / f"{meta.session_id}.jsonl"
        first = json.loads(path.read_text().splitlines()[0])
        self.assertEqual(first["type"], "session_meta")
        self.assertEqual(first["model"], "deepseek-chat")
        self.assertEqual(first["session_id"], meta.session_id)

    def test_append_message_jsonl_format(self):
        meta = self.writer.new_session(model="m")
        self.writer.append_message(meta.session_id, {"role": "user", "content": "你好"})
        path = Path("sessions") / f"{meta.session_id}.jsonl"
        lines = [json.loads(l) for l in path.read_text().splitlines()]
        self.assertEqual(lines[1], {"type": "message", "role": "user", "content": "你好"})
        # 中文不转义（ensure_ascii=False）
        self.assertIn("你好", path.read_text())

    def test_append_creates_dir_on_demand(self):
        # 删掉 sessions 目录再追加 → mkdir 重试路径
        meta = self.writer.new_session(model="m")
        path = Path("sessions") / f"{meta.session_id}.jsonl"
        import shutil

        shutil.rmtree("sessions")
        self.writer.append_message(meta.session_id, {"role": "user", "content": "after rm"})
        self.assertIn("after rm", path.read_text())


# ---------------------------------------------------------------------------
# 恢复：正常路径
# ---------------------------------------------------------------------------

class RestoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_cwd = os.getcwd()
        os.chdir(self._tmp.name)
        self.writer = TranscriptWriter(Path("sessions"))
        self.meta = self.writer.new_session(model="deepseek-chat")

    def tearDown(self):
        os.chdir(self._old_cwd)
        self._tmp.cleanup()

    def _path(self):
        return Path("sessions") / f"{self.meta.session_id}.jsonl"

    def test_roundtrip_messages_and_stats(self):
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "读文件"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "read_file", "arguments": '{"path": "x.txt"}'}}
            ]},
            {"role": "tool", "tool_call_id": "c1", "name": "read_file", "content": "内容"},
            {"role": "assistant", "content": "完成"},
        ]
        for m in msgs:
            self.writer.append_message(self.meta.session_id, m)
        self.writer.append_turn_stats(self.meta.session_id, 1, 500, 120)

        restored = load_transcript(self._path())
        self.assertEqual(restored.meta.session_id, self.meta.session_id)
        self.assertEqual(restored.messages, msgs)  # 完整往返
        self.assertEqual(restored.turns, 1)
        self.assertEqual(restored.input_tokens, 500)
        self.assertEqual(restored.output_tokens, 120)

    def test_crashed_half_line_skipped(self):
        """崩溃截断的半行 JSON → 跳过并计数（append-only 容错红利）。"""
        self.writer.append_message(self.meta.session_id, {"role": "user", "content": "ok"})
        # 模拟写入中途被 kill：追加半行
        with self._path().open("a", encoding="utf-8") as f:
            f.write('{"type": "message", "role": "user", "content": "被截断的半')
        restored = load_transcript(self._path())
        self.assertEqual(restored.skipped_lines, 1)
        self.assertEqual(len(restored.messages), 1)  # 完整行不受影响

    def test_orphan_tool_call_cleaned_on_restore(self):
        """崩溃在 assistant(tool_calls) 之后 → 恢复时孤儿 tool_call 被剔除。"""
        msgs = [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "跑命令"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "bash", "arguments": '{"command": "ls"}'}},
                {"id": "c2", "type": "function",
                 "function": {"name": "bash", "arguments": '{"command": "pwd"}'}},
            ]},
            {"role": "tool", "tool_call_id": "c1", "name": "bash", "content": "out"},
            # c2 的 tool 消息缺失（进程在此被杀）
        ]
        for m in msgs:
            self.writer.append_message(self.meta.session_id, m)
        restored = load_transcript(self._path())
        assistant = [m for m in restored.messages if m.get("role") == "assistant" and m.get("tool_calls")][0]
        self.assertEqual([tc["id"] for tc in assistant["tool_calls"]], ["c1"])  # c2 被剔

    def test_missing_meta_rejected(self):
        bad = Path("sessions") / "bad.jsonl"
        bad.parent.mkdir(exist_ok=True)
        bad.write_text('{"type": "message", "role": "user", "content": "x"}\n', encoding="utf-8")
        with self.assertRaises(SystemExit):
            load_transcript(bad)

    def test_list_sessions_newest_first(self):
        self.writer.append_message(self.meta.session_id, {"role": "user", "content": "a"})
        import time

        time.sleep(0.05)
        meta2 = self.writer.new_session(model="m2")
        sessions = list_sessions(Path("sessions"))
        self.assertEqual(len(sessions), 2)
        self.assertEqual(sessions[0].session_id, meta2.session_id)  # 最新在前


# ---------------------------------------------------------------------------
# QueryEngine 集成：自动持久化与 resume
# ---------------------------------------------------------------------------

class EngineIntegrationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_cwd = os.getcwd()
        os.chdir(self._tmp.name)
        (Path(self._tmp.name) / "hello.txt").write_text("hi\n", encoding="utf-8")
        self.tools = default_tools()

    def tearDown(self):
        os.chdir(self._old_cwd)
        self._tmp.cleanup()

    def _engine(self, client):
        agent = Agent(client=client, tools=self.tools, verbose=False)
        registry = default_registry(self.tools)
        return QueryEngine(
            agent, registry,
            transcript=TranscriptWriter(Path("sessions")),
            compaction_config=CompactionConfig(
                context_window_tokens=1000,   # 小窗口：长回答可触发 auto
                snip_message_threshold=100,   # 关闭 snip（不干扰本测试焦点）
                auto_keep_tail=2,
            ),
            summarizer=lambda t: "摘要：多轮任务已完成",
        )

    def test_auto_persist_and_resume_same_file(self):
        """两轮对话自动落盘；新进程（新 engine）恢复后续写同一文件。"""
        client = FakeClient(
            [
                make_response(tool_calls=[make_tool_call("c1", "read_file", '{"path": "hello.txt"}')]),
                make_response(content="第一轮完成"),
                make_response(content="恢复后继续"),
            ]
        )
        engine = self._engine(client)
        engine.submit_message("读 hello.txt")
        engine.submit_message("总结")
        sid = engine.session_meta.session_id
        path = Path("sessions") / f"{sid}.jsonl"

        # 模拟进程重启：全新 engine + client
        client2 = FakeClient([])
        engine2 = self._engine(client2)
        restored = engine2.restore_from(path)
        self.assertEqual(restored.meta.session_id, sid)
        self.assertTrue(engine2.stats.resumed)
        self.assertEqual(engine2.stats.turns, 2)
        # 上下文完整保留：恢复后的 messages 含第一轮的工具观察
        tool_msgs = [m for m in engine2.messages if m.get("role") == "tool"]
        self.assertIn("hi", tool_msgs[0]["content"])
        # 恢复续写同一文件（CC 语义：追加而非另开新档）
        self.assertEqual(engine2.session_meta.session_id, sid)

        # 恢复后继续对话 → 增量落盘到同一文件（总轮数 3）
        client2.responses = [make_response(content="恢复后继续")]
        engine2.submit_message("继续")
        lines = [json.loads(l) for l in path.read_text().splitlines()]
        stats_records = [l for l in lines if l["type"] == "turn_stats"]
        self.assertEqual(len(stats_records), 3)
        self.assertEqual(stats_records[-1]["turn"], 3)

    def test_no_transcript_backward_compatible(self):
        """transcript=None → 行为与迭代 7 完全一致。"""
        client = FakeClient([make_response(content="ok")])
        agent = Agent(client=client, tools=self.tools, verbose=False)
        engine = QueryEngine(agent, default_registry(self.tools))
        engine.submit_message("hi")
        self.assertFalse(Path("sessions").exists())
        with self.assertRaises(RuntimeError):
            engine.restore_from(Path("nonexistent.jsonl"))

    def test_boundary_persisted_and_restore_slices(self):
        """auto 压缩后 boundary 落盘；恢复时从 boundary 切片——不重新摘要。"""
        client = FakeClient(
            [make_response(content=f"第 {i} 轮回答" * 60) for i in range(4)]
        )
        engine = self._engine(client)  # 小窗口配置，长回答触发 auto
        for i in range(4):
            engine.submit_message(f"问题 {i}" * 100)
        sid = engine.session_meta.session_id
        path = Path("sessions") / f"{sid}.jsonl"
        raw = path.read_text(encoding="utf-8")
        # boundary 已作为事件落盘
        self.assertIn("[compact_boundary", raw)

        # 新进程恢复：拿到的应是压缩后工作集（boundary 开头），而非全历史
        client2 = FakeClient([])
        engine2 = self._engine(client2)
        restored = engine2.restore_from(path)
        first = str(restored.messages[0].get("content", ""))
        self.assertTrue(first.startswith("[compact_boundary"), msg=first[:80])

        # 恢复后水位低：下一轮不应再触发 auto（summarizer 若被调用即报错）
        def must_not_summarize(text: str) -> str:
            raise AssertionError("恢复后不应重新摘要")

        engine2._summarizer = must_not_summarize
        client2.responses = [make_response(content="继续")]
        engine2.submit_message("继续")
        self.assertFalse(engine2.last_compaction.auto_triggered)

    def test_clear_starts_new_session_file(self):
        """/clear 后新对话写入新文件，不再追加旧档（惰性开档）。"""
        client = FakeClient([make_response(content="第一轮")])
        engine = self._engine(client)
        engine.submit_message("hi")
        sid1 = engine.session_meta.session_id
        engine.clear()
        self.assertIsNone(engine.session_meta)  # 惰性：清空即重置，不立即开档
        client.responses = [make_response(content="清空后")]
        engine.submit_message("新开始")
        sid2 = engine.session_meta.session_id
        self.assertNotEqual(sid1, sid2)  # 首次 submit 才开新档
        self.assertIn("新开始", (Path("sessions") / f"{sid2}.jsonl").read_text())
        self.assertNotIn("新开始", (Path("sessions") / f"{sid1}.jsonl").read_text())

    def test_restore_boundary_first_system_inserted(self):
        """恢复后列表以 boundary(user) 开头 → 下轮 submit 应 insert system 而非覆掉 boundary。"""
        client = FakeClient([make_response(content=f"第 {i} 轮回答" * 60) for i in range(4)])
        engine = self._engine(client)
        for i in range(4):
            engine.submit_message(f"问题 {i}" * 100)
        path = Path("sessions") / f"{engine.session_meta.session_id}.jsonl"

        client2 = FakeClient([make_response(content="ok")])
        engine2 = self._engine(client2)
        engine2.restore_from(path)
        engine2.submit_message("继续")
        # system 在头部，boundary 紧随其后且未被覆掉
        self.assertEqual(engine2.messages[0].get("role"), "system")
        self.assertTrue(
            str(engine2.messages[1].get("content", "")).startswith("[compact_boundary")
        )

    def test_resume_does_not_create_orphan_session(self):
        """--resume 不应新开档（回归测试：构造时预开档的孤儿文件 bug）。

        曾的执行顺序：QueryEngine(...) 构造 → new_session 写新文件 →
        restore_from 覆写 session_meta —— 构造产生的空档永远留在 sessions/ 里，
        且 --resume latest 可能恢复到这个空档。修复后构造不再开档。
        """
        # 第一个 engine 跑一轮产生真实会话
        client = FakeClient([make_response(content="第一轮")])
        engine = self._engine(client)
        engine.submit_message("hi")
        sid = engine.session_meta.session_id
        before = set(p.name for p in Path("sessions").glob("*.jsonl"))

        # 模拟 CLI：新 engine（构造不开档）→ resume 目标档
        client2 = FakeClient([])
        engine2 = self._engine(client2)
        restored = engine2.restore_from(Path("sessions") / f"{sid}.jsonl")
        self.assertEqual(restored.meta.session_id, sid)
        # 此时 sessions/ 不应多出新文件（构造未预开档）
        after = set(p.name for p in Path("sessions").glob("*.jsonl"))
        self.assertEqual(after, before)
        # 未 submit 前 flush 不产生写入；恢复档已含完整响应脚本
        client2.responses = [make_response(content="续写进目标档")]
        engine2.submit_message("继续")  # ← 首次 submit 走 _ensure_session_open，
        # 但 session_meta 已是恢复档 → 不会开新档，续写目标档
        self.assertEqual(engine2.session_meta.session_id, sid)
        after2 = set(p.name for p in Path("sessions").glob("*.jsonl"))
        self.assertEqual(after2, before)  # 依然没有新文件

    def test_resume_target_resolution(self):
        """resolve_resume_target 三形态：latest / id / 文件路径。"""
        w = TranscriptWriter(Path("sessions"))
        meta = w.new_session(model="m")
        path = Path("sessions") / f"{meta.session_id}.jsonl"
        # 按 id
        self.assertEqual(resolve_resume_target(meta.session_id), path)
        # 按路径
        self.assertEqual(resolve_resume_target(str(path)), path)
        # latest
        self.assertEqual(resolve_resume_target("latest"), path)
        # 不存在
        with self.assertRaises(SystemExit):
            resolve_resume_target("no-such-id")


if __name__ == "__main__":
    unittest.main(verbosity=2)
