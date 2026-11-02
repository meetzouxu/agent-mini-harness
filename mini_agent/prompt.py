"""System Prompt 分段组装 —— 迭代 5 的核心之一。

复刻 Claude Code 的 System Prompt 动态组装（参考
claude-code docs/context/system-prompt.mdx）：

CC 三阶段管道：
    getSystemPrompt()            → string[]   （组装：静态区 + BOUNDARY + 动态区）
    buildEffectiveSystemPrompt() → SystemPrompt（优先级选择）
    buildSystemPromptBlocks()    → blocks     （分块 + cache_control 标记）

教学版合并为两层：
    SectionRegistry.resolve()    → list[str]  （组装 + 确定性缓存）
    build_system_prompt()        → str        （拼接；BOUNDARY 在发送前移除）

核心概念：
- **静态区**：identity 等对所有会话相同的段（is_static=True，只计算一次）
- **动态区**：tools / workspace 等随环境变化的段（每轮 resolve）
- **BOUNDARY 分界标记**（对应 CC SYSTEM_PROMPT_DYNAMIC_BOUNDARY）：分割静态区
  与动态区，为 prompt cache 划定「不变前缀」；发送给 API 前移除，模型不可见
- **确定性缓存**（对应 CC systemPromptSection 的 cacheBreak 语义）：动态段
  内容由输入完全决定（工具列表、cwd），输入不变 → 内容不变 → 缓存命中；
  cache_break=True 的段每轮强制重算（对应 DANGEROUS_uncachedSystemPromptSection，
  用于 MCP 指令这类会话中途变化的段，必须给出破坏缓存的理由）
"""

from __future__ import annotations

import json
import platform
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

# 分界标记（不发送给 API，仅用于分割缓存区）
SYSTEM_PROMPT_BOUNDARY = "__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__"


@dataclass
class CacheStats:
    """缓存命中统计（观察确定性缓存的效率）。"""

    hits: int = 0
    misses: int = 0

    @property
    def total(self) -> int:
        return self.hits + self.misses

    def hit_rate(self) -> float:
        return self.hits / self.total if self.total else 0.0


@dataclass
class PromptSection:
    """一个 prompt 段（对应 CC systemPromptSection() 注册的单元）。

    builder 必须是纯函数：相同输入 → 相同输出（确定性是缓存的前提）。
    """

    name: str
    builder: Callable[[], str]
    is_static: bool = False      # 静态区段：只计算一次
    cache_break: bool = False    # True = 每轮强制重算（破坏缓存，须有充分理由）


class SectionRegistry:
    """Section 注册表与解析器（对应 CC 的 Section 注册表 + resolveSystemPromptSections）。"""

    def __init__(self) -> None:
        self._sections: list[PromptSection] = []
        self._cache: dict[str, str] = {}
        self.stats = CacheStats()

    def register(
        self,
        name: str,
        builder: Callable[[], str],
        is_static: bool = False,
        cache_break: bool = False,
    ) -> None:
        if any(s.name == name for s in self._sections):
            raise ValueError(f"重复的 section 名: {name}")
        self._sections.append(PromptSection(name, builder, is_static, cache_break))

    def resolve(self) -> list[str]:
        """按注册序解析所有段，在静态区与动态区之间插入 BOUNDARY。

        返回的列表含 BOUNDARY 标记（供分层观察）；发送前用
        build_system_prompt() 移除。
        """
        parts: list[str] = []
        first_dynamic: int | None = None
        for i, sec in enumerate(self._sections):
            if not sec.is_static and first_dynamic is None:
                first_dynamic = i  # 此刻 parts 与 sections 索引一致
            parts.append(self._resolve_one(sec))
        if first_dynamic is not None:
            parts.insert(first_dynamic, SYSTEM_PROMPT_BOUNDARY)
        return parts

    def _resolve_one(self, sec: PromptSection) -> str:
        """解析单个段：静态段一次性缓存；动态段按内容确定性缓存。"""
        if sec.is_static:
            if sec.name in self._cache:
                self.stats.hits += 1
                return self._cache[sec.name]
            content = sec.builder()
            self._cache[sec.name] = content
            self.stats.misses += 1
            return content
        # 动态段：重算 → 与缓存比较（内容相同 = 输入未变 = 命中）
        content = sec.builder()
        if not sec.cache_break and self._cache.get(sec.name) == content:
            self.stats.hits += 1
            return content
        self._cache[sec.name] = content
        self.stats.misses += 1
        return content

    def reset(self) -> None:
        """清空缓存（/clear 语义：下轮全部重算）。"""
        self._cache.clear()
        self.stats = CacheStats()


def build_system_prompt(parts: list[str]) -> str:
    """把段列表拼接为最终 system prompt；BOUNDARY 在此处移除（模型不可见）。"""
    return "\n\n".join(p for p in parts if p != SYSTEM_PROMPT_BOUNDARY)


# ---------------------------------------------------------------------------
# 默认 Section 集（identity 静态 + tools/workspace 动态）
# ---------------------------------------------------------------------------

IDENTITY_TEXT = """\
你是 mini-agent，一个运行在终端里的编码助手，通过工具读写文件、搜索代码、执行命令来完成任务。

规则：
1. 调用工具前先想清楚参数；能用一次工具完成的不要拆成多次。
2. 工具返回错误时，仔细阅读错误信息、修正参数后重试，不要放弃。
   特别是 edit_file：old_string 必须唯一，报错不唯一时扩大上下文重试。
3. 被权限拒绝的操作不要原样重试 —— 阅读拒绝理由，改变策略或换一条路。
4. 任务完成后，用简洁的中文总结你做了什么、改了哪些文件。
5. 所有文件路径都相对当前工作目录。"""


def _tools_section_text(tools) -> str:
    """工具清单段：确定性来源 = 工具名与描述（工具集不变 → 内容不变 → 缓存命中）。"""
    lines = ["可用工具（调用前先确认参数）:"]
    for t in sorted(tools, key=lambda x: x.name):
        first_sentence = t.description.split("。")[0].split("\n")[0]
        lines.append(f"- {t.name}: {first_sentence}")
    return "\n".join(lines)


def _git_branch() -> str:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=5, cwd=Path.cwd(),
        )
        return proc.stdout.strip() if proc.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _workspace_section_text() -> str:
    """工作区段：确定性来源 = cwd / 平台 / git 分支。"""
    branch = _git_branch()
    return (
        f"工作目录: {Path.cwd()}\n"
        f"平台: {platform.system()} {platform.machine()}\n"
        f"git 分支: {branch or '(非 git 仓库)'}"
    )


def default_registry(tools) -> SectionRegistry:
    """默认注册表：静态区（identity）+ 动态区（tools / workspace）。

    工具列表以引用捕获——运行中注册新工具后，下一轮 resolve 自动反映变化
    （内容变化 → 缓存失效 → system prompt 更新）。
    """
    registry = SectionRegistry()
    registry.register("identity", lambda: IDENTITY_TEXT, is_static=True)
    registry.register("tools", lambda: _tools_section_text(tools))
    registry.register("workspace", lambda: _workspace_section_text())
    return registry
