"""上下文压缩 —— 迭代 6 的核心。

复刻 Claude Code 的压缩体系（参考 claude-code docs/context/compaction.mdx）。
CC 真实顺序（query.ts:379-468）：budget → snip → micro → collapse → auto。
教学版四层 + reactive 应急 + 配对不变量保护：

    L1 budget  applyToolResultBudget   工具结果 > 4000 字符 → 落盘文件，
                                       上下文只留占位+路径（即时执行，agent 回填时）
    L2 snip    snip_compact            消息 > 50 条 → 保留头 3 尾 47，
                                       中段折叠为一条占位（刀口避开工具对）
    L3 micro   micro_compact           估算 token > 50% 窗口 → 旧 tool 消息
                                       content 换占位（消息保留，对应 CC
                                       「清除内容不删消息」；白名单工具）
    L4 auto    auto_compact            估算 token > 75% 窗口（或 reactive
                                       > 95%）→ LLM 摘要全历史 →
                                       [system, 摘要(boundary), 尾部]
                                       熔断：最多 3 次（防压缩风暴）
    reactive   emergency_compact       auto 熔断后仍超硬限 → 本地截断式
                                       摘要兜底（不调 LLM，永不失败）

配对不变量（对应 CC adjustIndexToPreserveAPIInvariants）：
    API 要求每个 assistant.tool_calls[i].id 有配对的 tool.tool_call_id，
    反之亦然。所有压缩变换后跑 preserve_api_invariants() 清理孤儿。

CompactBoundary（对应 CC createCompactBoundaryMessage）：
    auto 压缩后插入边界标记消息，get_messages_after_boundary() 只处理
    最后一个 boundary 之后的消息（会话恢复时避免重复压缩）。
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

# 占位文本前缀（幂等标记：已压缩的结果不再重复处理）
BUDGET_PLACEHOLDER_PREFIX = "[工具结果过大"
MICRO_PLACEHOLDER = "[旧工具结果已清除"
SNIP_PLACEHOLDER_PREFIX = "[snip_compact]"
COMPACT_BOUNDARY_PREFIX = "[compact_boundary"

# micro 层可压缩的工具白名单（对应 CC COMPACTABLE_TOOLS；
# 全部内置工具的输出都是「可再生的观察」，都可清除）
COMPACTABLE_TOOLS = frozenset(
    {"read_file", "bash", "grep", "glob_files", "list_dir", "edit_file", "write_file"}
)


@dataclass
class CompactionConfig:
    """压缩阈值配置（教学默认值；测试用小窗口触发）。"""

    context_window_tokens: int = 64_000
    # L1 budget
    tool_result_disk_threshold: int = 4_000  # 超此字符数 → 落盘
    # L2 snip
    snip_message_threshold: int = 50
    snip_keep_head: int = 3
    snip_keep_tail: int = 47
    # L3 micro
    micro_threshold_ratio: float = 0.50
    micro_keep_recent: int = 6   # 最近 N 个工具结果保留
    # L4 auto / reactive
    auto_threshold_ratio: float = 0.75
    hard_limit_ratio: float = 0.95
    auto_keep_tail: int = 6
    auto_max_attempts: int = 3   # 熔断：auto 最多触发 3 次
    cache_dir: Path = field(default_factory=lambda: Path(".compact-cache"))


@dataclass
class CompactionReport:
    """一次压缩管线的执行报告（可观测性）。"""

    pre_tokens: int = 0
    post_tokens: int = 0
    budget_saved: int = 0     # budget 层节省字符数
    snip_folded: int = 0      # snip 折叠的消息条数
    micro_cleared: int = 0    # micro 清除的工具结果数
    auto_triggered: bool = False
    reactive_triggered: bool = False
    fused: bool = False       # auto 熔断

    @property
    def tokens_saved(self) -> int:
        return max(0, self.pre_tokens - self.post_tokens)


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------

def _tc_field(tc: Any, key: str, default: Any = None) -> Any:
    """兼容 dict 与属性对象两种 tool_call 形态（真实 SDK 为 dict，
    测试假客户端的 model_dump 浅拷贝会保留 SimpleNamespace）。"""
    if isinstance(tc, dict):
        return tc.get(key, default)
    return getattr(tc, key, default)


def estimate_tokens(messages: list[dict]) -> int:
    """粗估 token：字符数 / 4（英文近似；中文偏高约 1.5 倍，教学版够用）。

    CC 用精确 encoder；估算的意义在于「触发判定」，±20% 误差可接受。
    """
    total_chars = 0
    for m in messages:
        total_chars += len(str(m.get("content") or ""))
        for tc in m.get("tool_calls") or []:
            fn = _tc_field(tc, "function", {}) or {}
            arguments = (
                fn.get("arguments") if isinstance(fn, dict) else getattr(fn, "arguments", "")
            )
            total_chars += len(str(arguments or ""))
    return total_chars // 4


# ---------------------------------------------------------------------------
# L1 budget：大工具结果落盘（即时层，agent 回填时调用）
# ---------------------------------------------------------------------------

def budget_tool_result(content: str, tool_name: str, cfg: CompactionConfig) -> str:
    """工具结果超过阈值 → 完整内容落盘，上下文留占位 + 路径 + 预览。

    对应 CC applyToolResultBudget：必须在 micro 之前执行（否则落盘的
    是被 micro 清掉一半的内容）。幂等：占位文本直接原样返回。
    """
    if content.startswith(BUDGET_PLACEHOLDER_PREFIX) or len(content) <= cfg.tool_result_disk_threshold:
        return content
    cache_dir = Path.cwd() / cfg.cache_dir
    cache_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha1(f"{tool_name}:{content}".encode()).hexdigest()[:12]
    path = cache_dir / f"{tool_name}-{digest}.txt"
    path.write_text(content, encoding="utf-8")
    preview = content[:200].replace("\n", "\\n")
    return (
        f"{BUDGET_PLACEHOLDER_PREFIX}（{len(content)} 字符，已完整保存至 {path}，"
        f"需要时可 read_file 读取）]\n开头预览: {preview}"
    )


# ---------------------------------------------------------------------------
# 配对不变量保护（对应 CC adjustIndexToPreserveAPIInvariants）
# ---------------------------------------------------------------------------

def _has_tool_calls(m: dict) -> bool:
    return bool(m.get("tool_calls"))


def preserve_api_invariants(messages: list[dict]) -> list[dict]:
    """清理孤儿：无配对 assistant 的 tool 消息删除；无配对 tool 的
    tool_call 项从 assistant 中剔除（剔空且无文本的 assistant 删除）。

    任何压缩变换之后必须调用 —— API 对 tool_use/tool_result 配对
    是硬性要求，漏配直接 400。
    """
    # 收集所有 assistant 声明的 tool_call id
    declared_ids: set[str] = set()
    for m in messages:
        for tc in m.get("tool_calls") or []:
            tc_id = _tc_field(tc, "id")
            if tc_id:
                declared_ids.add(tc_id)
    # 收集所有 tool 消息响应的 id
    answered_ids: set[str] = set()
    for m in messages:
        if m.get("role") == "tool" and m.get("tool_call_id"):
            answered_ids.add(m["tool_call_id"])

    out: list[dict] = []
    for m in messages:
        role = m.get("role")
        if role == "tool" and m.get("tool_call_id") not in declared_ids:
            continue  # 孤儿 tool 消息（其 assistant 被压缩掉）
        if role == "assistant" and _has_tool_calls(m):
            kept = [
                tc for tc in m["tool_calls"]
                if _tc_field(tc, "id") in answered_ids
            ]
            if len(kept) != len(m["tool_calls"]):
                m = dict(m)
                m["tool_calls"] = kept
                if not kept and not m.get("content"):
                    continue  # 剔空且无文本的 assistant
        out.append(m)
    return out


# ---------------------------------------------------------------------------
# L2 snip：中段折叠
# ---------------------------------------------------------------------------

def _fold_boundary_adjust(messages: list[dict], start: int, end: int) -> tuple[int, int]:
    """调整折叠区间边界，保证不拆散 assistant(tool_calls)+tool 组。

    规则（对应 CC 的 startIndex 前移逻辑）：
    - 起点：若落在组内（前一条是带 tool_calls 的 assistant 且本条是其 tool）
      → 起点前移把 assistant 一起折叠
    - 终点：若落在组内（本条是 assistant(tool_calls) 且后一条是其 tool）
      → 终点后移把 tool 一起折叠
    """
    if start < end < len(messages):
        # 终点：messages[end-1] 是 assistant(tool_calls) 且 messages[end] 是其 tool → end+1
        if (
            messages[end - 1].get("role") == "assistant"
            and _has_tool_calls(messages[end - 1])
            and messages[end].get("role") == "tool"
        ):
            end += 1
    if 0 < start < end:
        # 起点：messages[start] 是 tool 且 messages[start-1] 是其 assistant
        if (
            messages[start].get("role") == "tool"
            and messages[start - 1].get("role") == "assistant"
            and _has_tool_calls(messages[start - 1])
        ):
            start -= 1
    return start, end


def snip_compact(
    messages: list[dict], cfg: CompactionConfig
) -> tuple[list[dict], int]:
    """消息条数超阈值 → 头尾保留、中段折叠为一条占位（不拆工具对）。"""
    if len(messages) <= cfg.snip_message_threshold:
        return messages, 0
    start = cfg.snip_keep_head
    end = len(messages) - cfg.snip_keep_tail
    start, end = _fold_boundary_adjust(messages, start, end)
    if end <= start:
        return messages, 0
    folded = messages[start:end]
    placeholder = {
        "role": "user",
        "content": (
            f"{SNIP_PLACEHOLDER_PREFIX} 中段 {len(folded)} 条消息已折叠"
            f"（{start}..{end}）：早期探索过程省略，关键结论保留在后续消息中"
        ),
    }
    return messages[:start] + [placeholder] + messages[end:], len(folded)


# ---------------------------------------------------------------------------
# L3 micro：旧工具结果占位
# ---------------------------------------------------------------------------

def micro_compact(
    messages: list[dict], cfg: CompactionConfig
) -> tuple[list[dict], int]:
    """估算 token 超软阈值 → 旧 tool 消息 content 换占位（消息与配对不动）。

    对应 CC MicroCompact：清除内容、保留消息；最近的 keep_recent 个
    工具结果保留（时间衰减的极简版）。
    """
    threshold = int(cfg.context_window_tokens * cfg.micro_threshold_ratio)
    if estimate_tokens(messages) <= threshold:
        return messages, 0
    # 从尾往前找最近 keep_recent 个 tool 消息（含白名单工具）
    recent_tool_ids: set[str] = set()
    seen = 0
    for m in reversed(messages):
        if (
            m.get("role") == "tool"
            and m.get("name") in COMPACTABLE_TOOLS
            and not str(m.get("content", "")).startswith(MICRO_PLACEHOLDER)
        ):
            recent_tool_ids.add(m.get("tool_call_id"))
            seen += 1
            if seen >= cfg.micro_keep_recent:
                break
    out: list[dict] = []
    cleared = 0
    for m in messages:
        if (
            m.get("role") == "tool"
            and m.get("name") in COMPACTABLE_TOOLS
            and m.get("tool_call_id") not in recent_tool_ids
            and not str(m.get("content", "")).startswith(MICRO_PLACEHOLDER)
        ):
            original = str(m.get("content", ""))
            out.append(
                {
                    **m,
                    "content": (
                        f"{MICRO_PLACEHOLDER}（原 {len(original)} 字符，"
                        f"工具 {m.get('name')} 的旧观察；如需原文可重新调用工具）"
                    ),
                }
            )
            cleared += 1
        else:
            out.append(m)
    return out, cleared


# ---------------------------------------------------------------------------
# L4 auto：LLM 摘要全量压缩
# ---------------------------------------------------------------------------

def _render_for_summary(messages: list[dict], max_chars_per_msg: int = 400) -> str:
    """把消息列表渲染为摘要模型的输入文本。"""
    lines = []
    for m in messages:
        role = m.get("role")
        if role == "system":
            continue  # system 不参与摘要（压缩后原样保留）
        content = str(m.get("content") or "")
        if role == "assistant" and m.get("tool_calls"):
            calls = ", ".join(
                str(_tc_field(_tc_field(tc, "function", {}) or {}, "name", "?"))
                for tc in m["tool_calls"]
            )
            lines.append(f"[assistant 调用工具: {calls}] {content[:max_chars_per_msg]}")
        elif role == "tool":
            lines.append(f"[tool {m.get('name')}] {content[:max_chars_per_msg]}")
        else:
            lines.append(f"[{role}] {content[:max_chars_per_msg]}")
    return "\n".join(lines)


def auto_compact(
    messages: list[dict],
    summarize: Callable[[str], str],
    cfg: CompactionConfig,
    boundary_type: str = "auto",
) -> tuple[list[dict], str | None]:
    """全量摘要压缩：[system] + 摘要(boundary) + 尾部 keep_tail 条。

    summarize 失败由调用方兜底（降级为 emergency_compact）。
    """
    keep_tail = min(cfg.auto_keep_tail, len(messages) - 1)
    head = messages[0] if messages and messages[0].get("role") == "system" else None
    body_start = 1 if head else 0
    compacted = messages[body_start : len(messages) - keep_tail]
    if not compacted:
        return messages, None
    summary = summarize(_render_for_summary(compacted))
    boundary = {
        "role": "user",
        "content": (
            f"{COMPACT_BOUNDARY_PREFIX} type={boundary_type} "
            f"pre_tokens={estimate_tokens(messages)} "
            f"compacted_messages={len(compacted)}]\n"
            f"以下是对话摘要，请基于它继续工作：\n{summary}"
        ),
    }
    out = ([head] if head else []) + [boundary] + messages[-keep_tail:]
    return preserve_api_invariants(out), summary


def emergency_compact(
    messages: list[dict], cfg: CompactionConfig
) -> list[dict]:
    """reactive 兜底（不调 LLM，永不失败）：中段换成每条消息首行的拼接。"""
    keep_tail = min(cfg.auto_keep_tail, len(messages) - 1)
    head = messages[0] if messages and messages[0].get("role") == "system" else None
    body_start = 1 if head else 0
    compacted = messages[body_start : len(messages) - keep_tail]
    if not compacted:
        return messages
    fallback_summary = "\n".join(
        str(m.get("content") or "")[:100] for m in compacted
    )[:2000]
    boundary = {
        "role": "user",
        "content": (
            f"{COMPACT_BOUNDARY_PREFIX} type=reactive pre_tokens={estimate_tokens(messages)}]\n"
            f"（紧急截断式摘要，LLM 摘要不可用）\n{fallback_summary}"
        ),
    }
    return preserve_api_invariants(
        ([head] if head else []) + [boundary] + messages[-keep_tail:]
    )


def get_messages_after_boundary(messages: list[dict]) -> list[dict]:
    """只处理最后一个 compact_boundary 之后的消息（对应 CC 同名函数）。"""
    last = -1
    for i, m in enumerate(messages):
        if str(m.get("content", "")).startswith(COMPACT_BOUNDARY_PREFIX):
            last = i
    return messages[last + 1 :] if last >= 0 else messages


# ---------------------------------------------------------------------------
# 管线：budget(即时已做) → snip → micro → auto/reactive
# ---------------------------------------------------------------------------

def run_pipeline(
    messages: list[dict],
    cfg: CompactionConfig,
    summarize: Callable[[str], str] | None,
    auto_attempts: int = 0,
) -> tuple[list[dict], CompactionReport]:
    """每轮 submit 前执行的压缩管线（原地语义：返回新列表）。

    顺序与 CC 一致：snip（条数）→ micro（局部）→ auto（全量）。
    reactive：auto 熔断后仍超硬限 → emergency 兜底。
    """
    report = CompactionReport(pre_tokens=estimate_tokens(messages))
    out = list(messages)

    out, report.snip_folded = snip_compact(out, cfg)
    out, report.micro_cleared = micro_compact(out, cfg)

    tokens = estimate_tokens(out)
    auto_threshold = int(cfg.context_window_tokens * cfg.auto_threshold_ratio)
    hard_limit = int(cfg.context_window_tokens * cfg.hard_limit_ratio)

    if tokens > auto_threshold or tokens > hard_limit:
        if auto_attempts >= cfg.auto_max_attempts:
            # 熔断（对应 CC maxConsecutiveAutoCompactions）：不再 LLM 摘要
            report.fused = True
            if tokens > hard_limit:
                report.reactive_triggered = True
                out = emergency_compact(out, cfg)
        elif summarize is not None:
            report.auto_triggered = True
            out, _summary = auto_compact(out, summarize, cfg)
        # summarize 不可用（测试/离线）且未熔断 → 仅依赖 snip+micro 的成果

    out = preserve_api_invariants(out)
    report.post_tokens = estimate_tokens(out)
    return out, report
