"""Hooks 扩展点 —— 迭代 4 的核心。

复刻 Claude Code Hooks 系统的教学精简版（参考 claude-code docs/extensibility/hooks.mdx）：

CC 有 27 种事件、6 种类型（command/prompt/agent/http/callback/function）。
教学版取最核心的 **4 事件** 与 **1 种类型（function）**——这是理解 Hook
本质的最小集合：Hook = 在生命周期固定点上，可注入的外部决策函数。

四事件与触发点（对应 CC 生命周期表）：

    UserPromptSubmit   用户提交任务后、调模型前    → 可注入额外上下文
    PreToolUse         工具执行前（权限裁决前）    → 可拦截 / 改参数
    PostToolUse        工具执行后（成功路径）      → 可修改结果 / 注入上下文
    Stop               模型给出最终回复、循环终止前 → 可强制续跑

HookResult 的字段设计（对应 CC syncHookResponseSchema 的关键字段，
即「四种 Hook 能力」）：

    permission_behavior: "allow" | "deny" | "ask" | None
        → PreToolUse 专用：直接给出权限决策（None = 不干预，交给权限管线）
    updated_input: dict | None
        → PreToolUse 专用：改写工具参数（如给 npm test 加 -- --bail）
    updated_output: str | None
        → PostToolUse 专用：改写工具输出（如脱敏/追加告警）
    additional_context: str | None
        → UserPromptSubmit / PostToolUse：注入到对话的额外上下文
    system_message: str | None
        → 直接显示给用户看的系统消息（警告/提示，不进对话）
    continue: bool（默认 True）
        → False = 阻止后续操作：PreToolUse 拦截工具、UserPromptSubmit 拦截任务
    stop_reason: str | None
        → continue=False 时给模型看的理由（会回填为观察）
    force_continue: str | None
        → Stop 专用：返回非空文本 = 注入该消息并强制续跑（对应 CC Stop hook
          返回字符串阻止停止的语义；对应 blocking 模式）

设计原则（从 CC 继承）：
1. hook 异常永不打断主循环 —— 捕获后转 system_message 警告，继续走
2. 多个 hook 按注册序执行，结果**合并**而非覆盖：
   permission_behavior 取最严格（deny > ask > allow > None）
3. Stop hook 的 force_continue 有防循环护栏（agent 侧 stopHookActive，
   对应 CC query.ts 的 stopHookActive 字段：只允许续跑一次）
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from typing import Any, Callable

# 事件常量（对应 CC HOOK_EVENTS 的教学子集）
EVENT_USER_PROMPT_SUBMIT = "UserPromptSubmit"
EVENT_PRE_TOOL_USE = "PreToolUse"
EVENT_POST_TOOL_USE = "PostToolUse"
EVENT_STOP = "Stop"

HOOK_EVENTS = (
    EVENT_USER_PROMPT_SUBMIT,
    EVENT_PRE_TOOL_USE,
    EVENT_POST_TOOL_USE,
    EVENT_STOP,
)

# 权限决策的严格度排序（合并多 hook 结果时取最严格）
_BEHAVIOR_STRICTNESS = {"allow": 0, "ask": 1, "deny": 2}


@dataclass
class HookResult:
    """一个 hook 的返回值（对应 CC syncHookResponseSchema 教学精简版）。"""

    permission_behavior: str | None = None   # "allow" / "deny" / "ask" / None(不干预)
    updated_input: dict | None = None        # PreToolUse：改写工具参数
    updated_output: str | None = None        # PostToolUse：改写工具输出
    additional_context: str | None = None    # 注入到对话的额外上下文
    system_message: str | None = None        # 直接给用户看的消息
    continue_: bool = True                   # False = 拦截（工具/任务不执行）
    stop_reason: str | None = None           # continue_=False 的理由（回填给模型）
    force_continue: str | None = None        # Stop 专用：非空 = 强制续跑注入文本

    @staticmethod
    def allow() -> "HookResult":
        return HookResult(permission_behavior="allow")

    @staticmethod
    def deny(reason: str = "") -> "HookResult":
        return HookResult(permission_behavior="deny", stop_reason=reason)

    @staticmethod
    def ask(reason: str = "") -> "HookResult":
        return HookResult(permission_behavior="ask", stop_reason=reason)


@dataclass
class Hook:
    """一个已注册的 hook（对应 CC 的 function 型 hook）。

    matcher 支持 CC 同款三模式（matchesPattern 语义）：
      "bash"            → 精确匹配工具名
      "bash|write_file" → 管道分隔多值
      "*"/""            → 通配（匹配所有工具）
    （正则模式留给后续迭代；工具名匹配已覆盖教学场景。）
    """

    event: str
    name: str
    handler: Callable[[dict[str, Any]], HookResult]  # 入参：事件 payload（可变字段）
    matcher: str = "*"
    source: str = "builtin"  # builtin / session

    def matches_tool(self, tool_name: str) -> bool:
        if self.matcher in ("*", ""):
            return True
        if "|" in self.matcher:
            return any(
                fnmatch.fnmatchcase(tool_name, m.strip())
                for m in self.matcher.split("|")
                if m.strip()
            )
        return fnmatch.fnmatchcase(tool_name, self.matcher)


def _merge_results(results: list[HookResult]) -> HookResult:
    """合并多个 hook 的结果（按注册序执行后聚合）。

    规则（对应 CC 多 hook 冲突处理的教学版）：
    - permission_behavior 取最严格（deny > ask > allow > None）
    - updated_input / updated_output 取最后一个非空（后写覆盖，注册序优先级）
    - additional_context 拼接（每条独立注入）
    - continue_ 全 False 才 False（任一 hook 放行即放行）
    - stop_reason 取第一个非空的（最早拦截者最优先）
    - force_continue 取第一个非空
    """
    merged = HookResult()
    behaviors = [r.permission_behavior for r in results if r.permission_behavior]
    if behaviors:
        merged.permission_behavior = max(behaviors, key=lambda b: _BEHAVIOR_STRICTNESS[b])
    for r in results:
        if r.updated_input is not None:
            merged.updated_input = r.updated_input
        if r.updated_output is not None:
            merged.updated_output = r.updated_output
        if r.additional_context:
            merged.additional_context = (
                f"{merged.additional_context}\n{r.additional_context}"
                if merged.additional_context
                else r.additional_context
            )
        if r.system_message:
            merged.system_message = (
                f"{merged.system_message}\n{r.system_message}"
                if merged.system_message
                else r.system_message
            )
        if r.stop_reason and not merged.stop_reason:
            merged.stop_reason = r.stop_reason  # deny/拦截理由，第一个非空优先
        if not r.continue_:
            merged.continue_ = False
        if r.force_continue:
            merged.force_continue = merged.force_continue or r.force_continue
    return merged


class HookRegistry:
    """Hook 注册表与运行器（对应 CC getMatchingHooks + trigger_hooks）。"""

    def __init__(self) -> None:
        # (event, 注册序) → Hook；用列表保序，注册序即执行序
        self._hooks: list[Hook] = []

    def register(self, hook: Hook) -> None:
        if hook.event not in HOOK_EVENTS:
            raise ValueError(f"未知 hook 事件: {hook.event}（可用: {', '.join(HOOK_EVENTS)}）")
        self._hooks.append(hook)

    def unregister(self, name: str) -> bool:
        before = len(self._hooks)
        self._hooks = [h for h in self._hooks if h.name != name]
        return len(self._hooks) < before

    def get_matching(self, event: str, tool_name: str | None = None) -> list[Hook]:
        """查找匹配事件与工具的 hooks（对应 CC getMatchingHooks）。"""
        out = []
        for h in self._hooks:
            if h.event != event:
                continue
            if event in (EVENT_PRE_TOOL_USE, EVENT_POST_TOOL_USE):
                if tool_name is None or not h.matches_tool(tool_name):
                    continue
            out.append(h)
        return out

    def trigger(self, event: str, payload: dict[str, Any]) -> HookResult:
        """触发一个事件：按注册序执行所有匹配 hooks 并合并结果。

        hook 异常不打断主循环（CC 同款原则）：捕获 → system_message 警告 → 继续。
        """
        tool_name = payload.get("tool_name")
        matched = self.get_matching(event, tool_name)
        results: list[HookResult] = []
        for hook in matched:
            try:
                r = hook.handler(payload)
                results.append(r if r is not None else HookResult())
            except Exception as e:  # noqa: BLE001 —— hook 异常永不打断主循环
                results.append(
                    HookResult(
                        system_message=(
                            f"[hook {hook.name} 执行异常: {type(e).__name__}: {e}，已忽略]"
                        )
                    )
                )
        return _merge_results(results) if results else HookResult()
