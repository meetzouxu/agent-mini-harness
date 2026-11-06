"""迭代 7 单测：记忆系统 —— 存储/索引/召回/提取/整理/集成。

运行：.venv/bin/python -m unittest discover -s tests -v
全部使用临时目录与假客户端，不依赖 API key。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mini_agent.agent import Agent  # noqa: E402
from mini_agent.memory import (  # noqa: E402
    MAX_PER_CATEGORY,
    MemoryEntry,
    UserMemory,
    extract_memories,
)
from mini_agent.prompt import default_registry  # noqa: E402
from mini_agent.session import QueryEngine  # noqa: E402
from mini_agent.tools import default_tools  # noqa: E402

from tests.test_agent_loop import FakeClient, make_response  # noqa: E402


# ---------------------------------------------------------------------------
# 存储与索引
# ---------------------------------------------------------------------------

class StorageTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_cwd = os.getcwd()
        os.chdir(self._tmp.name)
        self.mem = UserMemory(Path(".memory"))

    def tearDown(self):
        os.chdir(self._old_cwd)
        self._tmp.cleanup()

    def test_invalid_category_rejected(self):
        with self.assertRaises(ValueError):
            MemoryEntry("bogus", "内容")

    def test_save_and_list(self):
        self.mem.save(MemoryEntry("user", "用户是后端开发，偏好简洁回答"))
        self.mem.save(MemoryEntry("feedback", "不要 mock 数据库"))
        entries = self.mem.list_all()
        self.assertEqual([e.content for e in entries],
                         ["用户是后端开发，偏好简洁回答", "不要 mock 数据库"])

    def test_duplicate_save_skipped(self):
        self.mem.save(MemoryEntry("user", "偏好简洁回答"))
        self.assertFalse(self.mem.save(MemoryEntry("user", "偏好简洁回答")))
        self.assertEqual(len(self.mem.list_all()), 1)

    def test_index_reflects_saves(self):
        self.mem.save(MemoryEntry("user", "用户是后端开发"))
        index = self.mem.load_index()
        self.assertIn("# Memory index", index)
        self.assertIn("## user", index)
        self.assertIn("用户是后端开发", index)
        # 索引文件落盘
        self.assertTrue((Path(".memory") / "MEMORY.md").exists())

    def test_empty_index_placeholder(self):
        index = self.mem.load_index()
        self.assertIn("# Memory index", index)


# ---------------------------------------------------------------------------
# 召回（side-query）
# ---------------------------------------------------------------------------

class SideQueryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_cwd = os.getcwd()
        os.chdir(self._tmp.name)
        self.mem = UserMemory(Path(".memory"))
        self.mem.save(MemoryEntry("project", "合并冻结从周四开始"))
        self.mem.save(MemoryEntry("reference", "流水线 bug 记录在 Linear INGEST"))
        self.mem.save(MemoryEntry("user", "用户偏好用 pytest 写测试"))
        self.mem.save(MemoryEntry("feedback", "不要 mock 数据库连接"))

    def tearDown(self):
        os.chdir(self._old_cwd)
        self._tmp.cleanup()

    def test_relevant_memories_ranked(self):
        hits = self.mem.side_query("本周四有合并冻结吗")
        self.assertTrue(hits)
        self.assertEqual(hits[0].content, "合并冻结从周四开始")

    def test_english_query(self):
        hits = self.mem.side_query("pipeline bug 记录在哪里")
        self.assertTrue(any("Linear" in h.content for h in hits))

    def test_limit_five(self):
        for i in range(10):
            self.mem.save(MemoryEntry("project", f"项目事实 {i} 关于部署"))
        hits = self.mem.side_query("部署")
        self.assertLessEqual(len(hits), 5)

    def test_no_match_returns_empty(self):
        self.assertEqual(self.mem.side_query("完全无关的量子物理问题"), [])

    def test_exclusion_dedup(self):
        first = self.mem.side_query("合并冻结")
        self.assertTrue(first)
        # 已展示的内容被排除 → 第二次同样查询不再返回同一条
        second = self.mem.side_query("合并冻结", exclude_contents={first[0].content})
        self.assertTrue(all(h.content != first[0].content for h in second))


# ---------------------------------------------------------------------------
# 提取器
# ---------------------------------------------------------------------------

class ExtractTests(unittest.TestCase):
    def test_explicit_remember(self):
        entries = extract_memories("记住：发布前要跑全量回归测试")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].category, "project")
        self.assertIn("回归测试", entries[0].content)

    def test_explicit_english_remember(self):
        entries = extract_memories("remember that staging DB is rebuilt nightly")
        self.assertEqual(len(entries), 1)
        self.assertIn("staging DB", entries[0].content)

    def test_preference_pattern(self):
        entries = extract_memories("我喜欢简洁的中文回答")
        self.assertEqual(entries[0].category, "user")

    def test_correction_pattern(self):
        entries = extract_memories("以后不要在周末自动合并分支")
        self.assertEqual(entries[0].category, "feedback")

    def test_no_signal_no_entry(self):
        self.assertEqual(extract_memories("读一下 README.md"), [])

    def test_explicit_takes_priority(self):
        # 显式「记住」时不再走句式启发（一条即可）
        entries = extract_memories("记住：我喜欢 pytest")
        self.assertEqual(len(entries), 1)


# ---------------------------------------------------------------------------
# 整理（consolidate）
# ---------------------------------------------------------------------------

class ConsolidateTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_cwd = os.getcwd()
        os.chdir(self._tmp.name)
        self.mem = UserMemory(Path(".memory"))

    def tearDown(self):
        os.chdir(self._old_cwd)
        self._tmp.cleanup()

    def test_auto_consolidate_on_threshold(self):
        # 超过 CONSOLIDATE_THRESHOLD 触发整理，每类裁到 MAX_PER_CATEGORY
        for i in range(30):
            self.mem.save(MemoryEntry("project", f"项目事实 {i}"))
        entries = [e for e in self.mem.list_all() if e.category == "project"]
        self.assertLessEqual(len(entries), MAX_PER_CATEGORY)
        # 保留的是最新的（编号最大的）
        contents = [e.content for e in entries]
        self.assertIn(f"项目事实 {29}", contents)

    def test_dedup_in_consolidate(self):
        # 手工写入重复行（模拟历史脏数据）→ consolidate 去重
        path = Path(".memory/project.md")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("- 事实 A\n- 事实 A\n- 事实 B\n", encoding="utf-8")
        removed = self.mem.consolidate()
        self.assertEqual(removed, 1)
        self.assertEqual(len(self.mem.list_all()), 2)


# ---------------------------------------------------------------------------
# 集成：memory 段（缓存失效自动反映）+ QueryEngine 注入/提取
# ---------------------------------------------------------------------------

class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_cwd = os.getcwd()
        os.chdir(self._tmp.name)
        self.mem = UserMemory(Path(".memory"))
        self.tools = default_tools()

    def tearDown(self):
        os.chdir(self._old_cwd)
        self._tmp.cleanup()

    def test_memory_section_cache_invalidation(self):
        """新记忆保存 → memory 段内容变化 → 缓存失效 → system 更新（确定性缓存的又一次兑现）。"""
        registry = default_registry(self.tools, user_memory=self.mem)
        first = registry.resolve()
        self.mem.save(MemoryEntry("user", "用户偏好中文回复"))
        second = registry.resolve()
        self.assertNotEqual(
            "\n".join(first), "\n".join(second)
        )
        self.assertIn("用户偏好中文回复", "\n".join(second))
        # 索引 miss 计数增加（缓存失效证据）
        self.assertGreater(registry.stats.misses, 0)

    def _engine(self, client):
        agent = Agent(client=client, tools=self.tools, verbose=False)
        registry = default_registry(self.tools, user_memory=self.mem)
        return QueryEngine(agent, registry, memory=self.mem)

    def test_side_query_injection_into_turn(self):
        """命中记忆 → 本轮 user 消息前注入 [相关记忆]（请求里可见）。"""
        self.mem.save(MemoryEntry("project", "项目的发布日是每周四"))
        client = FakeClient(
            [
                make_response(content="好的，记住发布节奏了"),
                make_response(content="如记忆所示，周四发布"),
            ]
        )
        engine = self._engine(client)
        engine.submit_message("这周什么时候发布？")  # 触发 side-query 注入
        # 第一次请求 = system + [相关记忆] + user（3 条起手）
        first_req = client.requests[0]
        user_msgs = [m for m in first_req["messages"] if m.get("role") == "user"]
        self.assertEqual(len(user_msgs), 2)
        self.assertIn("[相关记忆]", user_msgs[0]["content"])
        self.assertIn("发布日是每周四", user_msgs[0]["content"])
        # 已展示去重：第二轮同话题不再新增注入（历史里那条是第一轮注入的，
        # 消息永不删除——看出现次数而非存在性）
        engine.submit_message("再确认一下发布时间")
        second_req = client.requests[1]
        memory_count = sum(
            1 for m in second_req["messages"]
            if str(m.get("content", "")).startswith("[相关记忆]")
        )
        self.assertEqual(memory_count, 1)  # 只有第一轮那一条，无新增

    def test_extraction_saves_across_session(self):
        """显式「记住」→ 轮后保存 → 磁盘文件 + 下轮 system 含新记忆。"""
        client = FakeClient(
            [
                make_response(content="好的，我记住了"),
                make_response(content="继续"),
            ]
        )
        engine = self._engine(client)
        engine.submit_message("记住：项目用 DeepSeek 而不是 GPT")
        self.assertEqual(engine.stats.memories_saved, 1)
        self.assertIn(
            "DeepSeek",
            Path(".memory/project.md").read_text(encoding="utf-8"),
        )
        # 第二轮 system prompt 已含新记忆（缓存失效自动反映）
        engine.submit_message("继续干活")
        self.assertIn("DeepSeek", engine.messages[0]["content"])

    def test_clear_keeps_memory(self):
        """/clear 只清会话，不清跨会话记忆。"""
        self.mem.save(MemoryEntry("user", "用户偏好简洁"))
        client = FakeClient([make_response(content="ok")])
        engine = self._engine(client)
        engine.submit_message("hi")
        engine.clear()
        self.assertEqual(len(self.mem.list_all()), 1)

    def test_memory_off_backward_compatible(self):
        """memory=None → 行为与迭代 6 完全一致（无注入无提取）。"""
        agent = Agent(client=FakeClient([make_response(content="ok")]),
                      tools=self.tools, verbose=False)
        engine = QueryEngine(agent, default_registry(self.tools), memory=None)
        engine.submit_message("记住：某事")
        self.assertEqual(engine.stats.memories_saved, 0)
        self.assertFalse(Path(".memory").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
