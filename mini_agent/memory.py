"""记忆系统 —— 迭代 7 的核心。

复刻 Claude Code 的跨会话记忆（参考 claude-code docs/context/project-memory.mdx）。
CC 的记忆系统是**纯文件**的——没有数据库、没有向量库，只有 Markdown。
教学版同样纯文件，并在三处刻意简化（文档里都有取舍说明）：

| CC 机制 | 教学版 | 简化点 |
|---------|--------|--------|
| 每条记忆一个 .md + frontmatter(name/description/type) | 按类四文件，每条一行 | 条目粒度 |
| Sonnet side-query 召回（LLM 筛 ≤5 条） | 2-gram 关键词打分 | 召回器（零 LLM 调用） |
| KAIROS 每日日志 / /dream 夜间蒸馏 | consolidate 去重 + 每类上限 | 整理策略 |

保留的 CC 核心设计：
- **四类型封闭分类**（memoryTypes.ts）：user/feedback/project/reference——
  只存「无法从当前项目状态推导的信息」
- **MEMORY.md 入口索引**：双上限（200 行 AND 25KB，对应
  MAX_ENTRYPOINT_LINES/MAX_ENTRYPOINT_BYTES）超限截断
- **索引常驻 system prompt**（systemPromptSection('memory')）
- **side-query ≤5 条按需注入**当前 user turn + **已展示去重**
  （alreadySurfaced——5 槽预算花在新候选上）
- **漂移防御**：注入文本携带「引用前先验证时效」指令（对应
  TRUSTING_RECALL_SECTION "Before recommending from memory"）

Session Memory（单会话层）：迭代 6 的 auto_compact 摘要即承担此角色
（CC 里 SM-compact 直接用 Session Memory 代替摘要模型）——本迭代只做
概念正名，代码不动。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# ---- 四类型（对应 CC memoryTypes.ts 的封闭分类） ----
MEMORY_CATEGORIES = ("user", "feedback", "project", "reference")

CATEGORY_DESCRIPTIONS = {
    "user": "用户角色、偏好、技术背景（「我是后端开发」「我写了十年 Java」）",
    "feedback": "用户对 AI 行为的纠正与确认（「别 mock 数据库」「单 PR 更好」）",
    "project": "非代码可推导的项目上下文（「合并冻结从周四开始」）",
    "reference": "外部系统指针（「流水线 bug 记录在 Linear INGEST 项目」）",
}

# ---- 索引双上限（对应 CC MAX_ENTRYPOINT_LINES / MAX_ENTRYPOINT_BYTES） ----
MAX_INDEX_LINES = 200
MAX_INDEX_BYTES = 25_000

# ---- consolidate 参数 ----
MAX_PER_CATEGORY = 8   # 每类记忆上限（超出即触发整理裁最旧）

SIDE_QUERY_LIMIT = 5   # 按需注入上限（对应 CC 5 槽预算）

# 漂移防御提示（对应 CC "Before recommending from memory" A/B 测试胜出版本）
DRIFT_GUARD_TEXT = "引用记忆前先验证时效：提到文件/函数的记忆是写入时的主张，可能已过时。"


@dataclass
class MemoryEntry:
    """一条记忆：类别 + 一句话内容（CC 为 frontmatter 文件，教学版为行）。"""

    category: str
    content: str
    created_at: str = ""

    def __post_init__(self) -> None:
        if not self.created_at:
            self.created_at = datetime.now().isoformat(timespec="seconds")
        if self.category not in MEMORY_CATEGORIES:
            raise ValueError(
                f"未知记忆类别: {self.category}（可用: {', '.join(MEMORY_CATEGORIES)}）"
            )


# ---------------------------------------------------------------------------
# UserMemory：文件型存储 + 索引 + 召回 + 整理
# ---------------------------------------------------------------------------

class UserMemory:
    """跨会话记忆（纯文件存储，对应 CC 的 memoryDir + MEMORY.md）。

    文件布局：
        .memory/
        ├── MEMORY.md       ← 入口索引（常驻 system prompt 的 memory 段）
        ├── user.md         ← 每条一行 "- 内容"
        ├── feedback.md
        ├── project.md
        └── reference.md
    """

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or Path(".memory")

    # ---- 存储 ----

    def _category_file(self, category: str) -> Path:
        return self.root / f"{category}.md"

    def save(self, entry: MemoryEntry) -> bool:
        """保存一条记忆。同类别同内容已存在 → 跳过（返回 False）。

        保存后该类超上限 → consolidate（裁最旧 + 去重，对应 CC 整理）。
        触发条件按「类内条数」而非总数——否则触发一次裁剪后，
        后续保存不再到达总数阈值，限制失效。
        """
        existing = {e.content for e in self.list_all() if e.category == entry.category}
        if entry.content in existing:
            return False
        path = self._category_file(entry.category)
        self.root.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(f"- {entry.content}\n")
        category_count = len(
            [e for e in self.list_all() if e.category == entry.category]
        )
        if category_count > MAX_PER_CATEGORY:
            self.consolidate()
        return True

    def list_all(self) -> list[MemoryEntry]:
        """读全部记忆（按文件序，文件内按行序）。"""
        entries: list[MemoryEntry] = []
        if not self.root.exists():
            return entries
        for category in MEMORY_CATEGORIES:
            path = self._category_file(category)
            if not path.exists():
                continue
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("- "):
                    entries.append(MemoryEntry(category, line[2:]))
        return entries

    # ---- 索引 ----

    def load_index(self) -> str:
        """入口索引：MEMORY.md。不存在或过期 → 从四文件重建。

        双上限（200 行 AND 25KB）超限截断并附警告（对应 CC
        truncateEntrypointContent——200 行覆盖 p97，字节上限捕捉长行异常）。
        """
        index_path = self.root / "MEMORY.md"
        entries = self.list_all()
        lines = ["# Memory index", ""]
        for category in MEMORY_CATEGORIES:
            cat_entries = [e for e in entries if e.category == category]
            if not cat_entries:
                continue
            lines.append(f"## {category}")
            for e in cat_entries:
                lines.append(f"- {e.content}")
            lines.append("")
        content = "\n".join(lines).rstrip() + "\n"
        truncated = False
        if len(content.splitlines()) > MAX_INDEX_LINES:
            content = "\n".join(content.splitlines()[:MAX_INDEX_LINES]) + "\n"
            truncated = True
        if len(content.encode("utf-8")) > MAX_INDEX_BYTES:
            content = content[: MAX_INDEX_BYTES] + "\n"
            truncated = True
        if truncated:
            content += f"<!-- 索引超限已截断（>200 行或 25KB） -->\n"
        index_path.parent.mkdir(parents=True, exist_ok=True)
        index_path.write_text(content, encoding="utf-8")
        return content

    # ---- 召回（side-query，教学版 2-gram 关键词打分） ----

    def side_query(
        self,
        query: str,
        limit: int = SIDE_QUERY_LIMIT,
        exclude_contents: set[str] | None = None,
    ) -> list[MemoryEntry]:
        """选与当前 query 相关的记忆（≤limit 条）。

        CC 用 Sonnet 侧查询从清单中筛 ≤5 条；教学版用 2-gram 词集合
        交集打分（零 LLM 调用、可离线测试；中文天然按 2-gram 切分）。
        exclude_contents = 已展示去重（对应 CC alreadySurfaced——
        5 槽预算花在新候选上，不重复召回）。
        """
        exclude = exclude_contents or set()
        query_tokens = _tokens(query)
        if not query_tokens:
            return []
        scored: list[tuple[int, MemoryEntry]] = []
        for entry in self.list_all():
            if entry.content in exclude:
                continue
            entry_tokens = _tokens(entry.content)
            overlap = len(query_tokens & entry_tokens)
            if overlap > 0:
                scored.append((overlap, entry))
        scored.sort(key=lambda pair: -pair[0])
        return [e for _, e in scored[:limit]]

    # ---- 整理 ----

    def consolidate(self) -> int:
        """去重 + 每类上限裁最旧（对应 CC 记忆整理/夜间蒸馏的教学版）。

        返回删除的条数。
        """
        removed = 0
        for category in MEMORY_CATEGORIES:
            path = self._category_file(category)
            if not path.exists():
                continue
            seen: set[str] = set()
            kept: list[str] = []
            for line in path.read_text(encoding="utf-8").splitlines():
                content = line.strip()[2:] if line.strip().startswith("- ") else ""
                if not content:
                    continue
                if content in seen:
                    removed += 1
                    continue
                seen.add(content)
                kept.append(f"- {content}")
            if len(kept) > MAX_PER_CATEGORY:
                removed += len(kept) - MAX_PER_CATEGORY
                kept = kept[-MAX_PER_CATEGORY:]  # 裁最旧（文件序 = 时间序）
            path.write_text("\n".join(kept) + "\n", encoding="utf-8")
        return removed


def _tokens(text: str) -> set[str]:
    """轻量分词：英文小写词（单复数归一）+ 中文 2-gram（side-query 打分用）。

    已知局限：无法跨语言语义匹配（如 "pipeline" 对「流水线」）——
    CC 用 LLM 语义召回解决，教学版的关键词打分只保证字面交集命中。
    """
    tokens: set[str] = set()
    for word in re.findall(r"[a-zA-Z_][a-zA-Z0-9_]*", text.lower()):
        if len(word) >= 2:
            tokens.add(word[:-1] if len(word) > 3 and word.endswith("s") else word)
    for i in range(len(text) - 1):
        pair = text[i : i + 2]
        if re.search(r"[\u4e00-\u9fff]", pair):  # 只对含汉字的 2-gram 计数
            tokens.add(pair)
    return tokens


# ---------------------------------------------------------------------------
# 提取器（每轮结束跑，对应 CC 的记忆保存指令）
# ---------------------------------------------------------------------------

# 显式记忆指令（对应 CC 用户说「记住 X」）
_EXPLICIT_PATTERNS = (
    re.compile(r"(?:请)?记住[:：，,]?\s*(.+)"),
    re.compile(r"remember\s+(?:that\s+)?(.+)", re.IGNORECASE),
)
# 偏好/反馈句式启发（教学版规则；CC 由模型判断）
_PREFERENCE_PATTERNS = (
    (re.compile(r"我(?:喜欢|偏好|习惯|总是|一直)…?(.+)"), "user"),
    (re.compile(r"(?:以后|以后请|下次)?(?:不要|别)(.+)"), "feedback"),
    (re.compile(r"(?:对|没错|就是这样)[，,。].*"), "feedback"),
)


def extract_memories(user_text: str, final_text: str = "") -> list[MemoryEntry]:
    """从本轮对话提取值得跨会话保留的记忆。

    教学版规则（CC 由模型的记忆保存指令判断）：
    1. 显式指令：「记住 X」/「remember X」→ project 类
    2. 偏好句式：「我喜欢/我习惯 X」→ user 类
    3. 纠正句式：「不要/别 X」→ feedback 类
    只处理用户消息（AI 的输出不该自我沉淀为用户记忆）。
    """
    entries: list[MemoryEntry] = []
    text = user_text.strip()
    if not text:
        return entries
    for pattern in _EXPLICIT_PATTERNS:
        m = pattern.search(text)
        if m:
            content = m.group(1).strip().rstrip("。.!！")
            if 2 <= len(content) <= 120:  # 一句话粒度
                entries.append(MemoryEntry("project", content))
    if any(p.search(text) for p in _EXPLICIT_PATTERNS):
        return entries  # 显式指令优先，不再走句式启发
    for pattern, category in _PREFERENCE_PATTERNS:
        m = pattern.search(text)
        if m:
            content = m.group(0).strip().rstrip("。.!！")
            if 4 <= len(content) <= 120:
                entries.append(MemoryEntry(category, content))
                break  # 一轮至多一条句式记忆
    return entries
