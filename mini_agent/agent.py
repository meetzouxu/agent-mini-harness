"""Agent Loop —— 迭代 1 的核心；迭代 2 升级了工具分派（并发执行）。

复刻 Claude Code src/query.ts queryLoop() 的最小状态机：

    while True:
        1. 组装消息 → 调模型（思考）
        2. 收集 tool_calls（行动指令）
        3. 执行工具，结果作为 tool 消息回填（观察）
        4. 有 tool_calls → 继续；没有 → 终止（completed）
           轮次超限 → 终止（max_turns）

对应关系（claude-code → 本文件）：
    queryLoop() while(true)  → Agent.run() 的 while True
    needsFollowUp 标志       → tool_calls 是否非空
    State.transition         → LoopState.transition（'next_turn' / None）
    State.turnCount          → LoopState.turn_count
    max_turns 终止           → max_turns 参数
    tool_result 回填         → {"role": "tool", ...} 消息 append
    工具错误不崩溃           → 异常转成错误文本回填，让模型自行修正
"""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from .tools import Tool, ToolError
from .permissions import Behavior, PermissionManager, PermissionResult
from .hooks import (
    EVENT_POST_TOOL_USE,
    EVENT_PRE_TOOL_USE,
    EVENT_STOP,
    EVENT_USER_PROMPT_SUBMIT,
    HookRegistry,
    HookResult,
)

# ---- ANSI 颜色（macOS/Linux 终端） ----
_DIM = "\033[2m"
_CYAN = "\033[36m"
_YELLOW = "\033[33m"
_GREEN = "\033[32m"
_RED = "\033[31m"
_RESET = "\033[0m"


@dataclass
class ToolEvent:
    """一次工具调用的执行记录（日志/监控的原始素材，迭代 15 会落盘）。"""

    turn: int
    name: str
    arguments: dict
    result: str
    duration_ms: float
    is_error: bool = False


@dataclass
class LoopState:
    """单次 run 的循环状态 —— 对应 claude-code queryLoop 的 State 对象（精简版）。

    每次 continue 都会更新字段（这里是就地更新；claude-code 用不可变重建，
    迭代 12 做异步改造时再对齐）。
    """

    messages: list[dict[str, Any]] = field(default_factory=list)
    turn_count: int = 0          # 已完成的「思考→行动→观察」轮数
    api_calls: int = 0           # 实际 API 调用次数（迭代 11 重试会使其 > turn_count）
    input_tokens: int = 0
    output_tokens: int = 0
    transition: str | None = None  # 上一轮继续的原因：'next_turn' / None
    stop_reason: str | None = None  # 'completed' / 'max_turns'


@dataclass
class RunResult:
    """run() 的最终返回。"""

    final_text: str
    stop_reason: str
    turn_count: int
    tool_calls_count: int
    input_tokens: int
    output_tokens: int
    messages: list[dict[str, Any]]
    tool_events: list[ToolEvent]


class Agent:
    """最小 Agent：模型 + 工具集 + 运行循环 + 当前状态。"""

    def __init__(
        self,
        client: Any,  # openai.OpenAI（或测试里的假客户端）
        tools: list[Tool],
        system_prompt: str | None = None,
        max_turns: int = 15,
        verbose: bool = True,
        model: str = "deepseek-chat",
        max_workers: int = 4,
        permission_manager: PermissionManager | None = None,
        hooks: HookRegistry | None = None,
    ) -> None:
        self.client = client
        self.tools: dict[str, Tool] = {t.name: t for t in tools}
        self.system_prompt = system_prompt
        self.max_turns = max_turns
        self.verbose = verbose
        self.model = model
        self.max_workers = max_workers
        # 迭代 3：权限管理器；未提供时行为与迭代 2 完全一致（兼容旧测试）
        self.permissions = permission_manager
        # 迭代 4：hook 注册表；未提供时四个触发点均为空操作（兼容旧测试）
        self.hooks = hooks or HookRegistry()
        # 并发执行工具时，终端打印需要互斥，避免多线程输出交错撕裂
        self._print_lock = threading.Lock()

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------

    def run(self, prompt: str) -> RunResult:
        """执行一次完整的 Agentic Loop：输入任务，输出最终结果。"""
        state = LoopState()
        if self.system_prompt:
            state.messages.append({"role": "system", "content": self.system_prompt})
        state.messages.append({"role": "user", "content": prompt})

        tool_events: list[ToolEvent] = []
        tool_calls_count = 0
        final_text = ""
        # 迭代 4：Stop hook 防循环护栏（对应 CC stopHookActive）——
        # 只允许续跑一次；第二次 Stop 触发时忽略 force_continue
        stop_hook_active = False

        # ---- 迭代 4：UserPromptSubmit（任务提交后、调模型前） ----
        upr = self.hooks.trigger(
            EVENT_USER_PROMPT_SUBMIT, {"prompt": prompt, "messages": state.messages}
        )
        if upr.system_message:
            self._say(f"{_YELLOW}⚠ {upr.system_message}{_RESET}")
        if upr.additional_context:
            state.messages.append(
                {"role": "user", "content": upr.additional_context}
            )
            self._say(f"{_DIM}📌 UserPromptSubmit 注入上下文{_RESET}")
        if not upr.continue_:
            self._say(f"{_RED}✗ UserPromptSubmit hook 拦截任务: {upr.stop_reason}{_RESET}")
            return RunResult(
                final_text=f"任务被 UserPromptSubmit hook 拦截: {upr.stop_reason or '未提供原因'}",
                stop_reason="hook_blocked",
                turn_count=0,
                tool_calls_count=0,
                input_tokens=0,
                output_tokens=0,
                messages=state.messages,
                tool_events=[],
            )

        while True:
            # ---- 阶段 2：调用模型（思考） ----
            response = self._call_model(state.messages)
            message = response.choices[0].message
            state.messages.append(message.model_dump(exclude_none=True))
            state.api_calls += 1
            self._accumulate_usage(response, state)

            tool_calls = list(message.tool_calls or [])

            if not tool_calls:
                # ---- 阶段 4：终止（claude-code 的 needsFollowUp = False） ----
                # 迭代 4：Stop hook（终止前）——force_continue 可注入消息强制续跑
                sr = self.hooks.trigger(
                    EVENT_STOP,
                    {"messages": state.messages, "final_text": message.content or ""},
                )
                if sr.system_message:
                    self._say(f"{_YELLOW}⚠ {sr.system_message}{_RESET}")
                if sr.force_continue and not stop_hook_active:
                    stop_hook_active = True  # 护栏置位：只允许续跑一次
                    self._say(
                        f"{_YELLOW}↻ Stop hook 强制续跑: {sr.force_continue}{_RESET}"
                    )
                    state.messages.append({"role": "user", "content": sr.force_continue})
                    continue
                state.stop_reason = "completed"
                final_text = message.content or ""
                self._say(f"{_GREEN}✓ 完成{_RESET}")
                break

            # ---- 阶段 3：执行工具（行动 + 观察，迭代 2：并发分派） ----
            self._say(f"{_DIM}── turn {state.turn_count + 1} ──────────────────────{_RESET}")
            events = self._dispatch_tool_calls(state.turn_count + 1, tool_calls)
            for tc, event in zip(tool_calls, events):
                tool_events.append(event)
                tool_calls_count += 1
                state.messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "name": tc.function.name,
                        "content": event.result,
                    }
                )

            # ---- 阶段 4：继续还是终止？ ----
            state.turn_count += 1
            if state.turn_count >= self.max_turns:
                state.stop_reason = "max_turns"
                final_text = message.content or ""
                self._say(f"{_RED}✗ 达到最大轮次 {self.max_turns}，强制终止{_RESET}")
                break
            state.transition = "next_turn"  # 记录继续原因，回到阶段 1

        return RunResult(
            final_text=final_text,
            stop_reason=state.stop_reason or "unknown",
            turn_count=state.turn_count,
            tool_calls_count=tool_calls_count,
            input_tokens=state.input_tokens,
            output_tokens=state.output_tokens,
            messages=state.messages,
            tool_events=tool_events,
        )

    # ------------------------------------------------------------------
    # 工具分派（迭代 2 新增）
    # ------------------------------------------------------------------

    def _dispatch_tool_calls(self, turn: int, tool_calls: list[Any]) -> list[ToolEvent]:
        """分派本轮所有 tool_calls —— 对应 claude-code runTools() 的并发判定。

        策略（教学版，保序优先）：
        - 本轮全部工具都 is_concurrency_safe（只读无副作用）→ 线程池并发执行
        - 只要有一个不安全（写/改/命令）→ 全部串行，保持原始顺序

        为什么「全或无」而不是「只读并发 + 写串行」：若同一轮里 read 和 write
        指向同一文件，重排序会改变 read 看到的内容（读到写前还是写后）。
        全或无策略保证：只要可能出现写，行为就与串行完全一致。

        无论并发还是串行，事件列表都按 tool_calls 原始顺序返回 ——
        API 协议要求每个 tool_call_id 都有对应的 tool 消息，按原序回填最稳妥。
        """
        def _is_safe(tc: Any) -> bool:
            tool = self.tools.get(tc.function.name)
            return tool is not None and tool.is_concurrency_safe

        if len(tool_calls) > 1 and all(_is_safe(tc) for tc in tool_calls):
            # 迭代 3 补充：并发批次里任何一个可能触发 ask 交互 → 整批串行化
            # （交互回调只能串行调用，避免多个确认弹窗交错）
            if self.permissions is None or not any(
                self.permissions.peek(
                    self.tools[tc.function.name], _parse_args_for_check(tc)
                ).behavior
                is Behavior.ASK
                for tc in tool_calls
            ):
                self._say(f"{_DIM}⚡ {len(tool_calls)} 个只读工具 → 并发执行{_RESET}")
                with ThreadPoolExecutor(
                    max_workers=min(self.max_workers, len(tool_calls))
                ) as pool:
                    futures = [
                        pool.submit(self._execute_tool_call, turn, tc) for tc in tool_calls
                    ]
                return [fut.result() for fut in futures]  # futures 按提交序 → 结果天然保序

        return [self._execute_tool_call(turn, tc) for tc in tool_calls]

    def _permission_with_hook(
        self, tool: Tool, arguments: dict, pre: "HookResult"
    ) -> PermissionResult:
        """融合 PreToolUse hook 决策与权限管线（迭代 4）。

        融合规则（CC 同款：hook allow 不能凌驾系统边界）：
        - hook deny → 直接 DENY（hook 可加严，不可放宽）
        - hook ask → 权限结果升为 ASK（若本为 allow）
        - hook allow → 仅当权限结果为 ask 时免除询问；deny（黑名单/规则）不变
        - hook None → 原样返回权限结果
        """
        decision = self.permissions.check(tool, arguments)
        hb = pre.permission_behavior
        if hb == "deny":
            return PermissionResult(
                Behavior.DENY,
                pre.stop_reason or "PreToolUse hook 拒绝",
            )
        if hb == "ask" and decision.behavior is Behavior.ALLOW:
            decision = PermissionResult(Behavior.ASK, "PreToolUse hook 要求确认")
        if hb == "allow" and decision.behavior is Behavior.ASK:
            decision = PermissionResult(Behavior.ALLOW, "PreToolUse hook 免除询问")
        if decision.behavior is Behavior.DENY:
            return decision  # 走 decide 的拒绝记录链路
        if decision.behavior is Behavior.ALLOW:
            self.permissions._record_success(tool.name)
            return decision
        return self.permissions.decide(tool, arguments)  # ask → 走交互

    # ------------------------------------------------------------------
    # 子步骤
    # ------------------------------------------------------------------

    def _call_model(self, messages: list[dict[str, Any]]) -> Any:
        """发起一次模型调用（迭代 11 会加入重试与模型降级）。"""
        return self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            tools=[t.to_api_schema() for t in self.tools.values()],
        )

    def _execute_tool_call(self, turn: int, tc: Any) -> ToolEvent:
        """执行单个 tool_call。

        迭代 4 完整链路：PreToolUse hook → 权限管线 → 执行 → PostToolUse hook。
        - PreToolUse 在权限裁决**之前**（hook 可拦截/改参数，也可提前 allow/deny）；
          但 hook 的 allow 不能绕过 deny 规则与黑名单（CC 同款：hook allow 只能
          免询问，不能凌驾系统硬边界）
        - PostToolUse 在成功路径上触发，可改写输出/注入上下文
        任何失败都不抛出，循环永不因工具问题崩溃。
        """
        name = tc.function.name
        started = time.perf_counter()
        try:
            arguments = json.loads(tc.function.arguments or "{}")
            if not isinstance(arguments, dict):
                raise ToolError(f"参数必须是 JSON 对象，收到: {type(arguments).__name__}")
            tool = self.tools.get(name)
            if tool is None:
                raise ToolError(f"未知工具: {name}（可用: {', '.join(self.tools)}）")

            # ---- 迭代 4：PreToolUse hook（权限之前） ----
            pre = self.hooks.trigger(
                EVENT_PRE_TOOL_USE,
                {"tool_name": name, "arguments": arguments, "turn": turn},
            )
            if pre.system_message:
                self._say(f"{_YELLOW}⚠ {pre.system_message}{_RESET}")
            if not pre.continue_:
                raise ToolError(
                    f"[PreToolUse 拦截] {pre.stop_reason or 'hook 未提供原因'}"
                )
            if pre.updated_input is not None:
                if not isinstance(pre.updated_input, dict):
                    raise ToolError("[PreToolUse 拦截] updated_input 必须是对象")
                arguments = pre.updated_input  # 改写参数后继续走权限/校验
                self._say(f"{_DIM}✎ PreToolUse 改写参数{_RESET}")
            if pre.additional_context:
                # CC 语义：注入为下一条用户消息；教学版直接拼进工具结果旁的说明
                self._say(f"{_DIM}📌 PreToolUse 附加上下文: {self._brief(pre.additional_context)}{_RESET}")

            # ---- 迭代 3：权限裁决（在参数校验之后、执行之前） ----
            if self.permissions is not None:
                decision = self._permission_with_hook(tool, arguments, pre)
                if decision.behavior is Behavior.DENY:
                    raise ToolError(f"[权限拒绝] {decision.reason}")
                self._say(
                    f"  {_YELLOW}🔒 {decision.behavior.value}({_DIM}{decision.reason}{_RESET}{_YELLOW}){_RESET}"
                )

            result = tool.run(arguments)
            is_error = False
        except ToolError as e:
            result, is_error = f"[工具错误] {e}", True
        except json.JSONDecodeError as e:
            result, is_error = f"[参数解析失败] {e}", True
        except Exception as e:  # noqa: BLE001 —— 工具内部任何异常都不允许打断循环
            result, is_error = f"[工具异常] {type(e).__name__}: {e}", True

        duration_ms = (time.perf_counter() - started) * 1000

        # ---- 迭代 4：PostToolUse hook（成功路径，可改写输出/注入上下文） ----
        if not is_error:
            post = self.hooks.trigger(
                EVENT_POST_TOOL_USE,
                {
                    "tool_name": name,
                    "arguments": arguments,
                    "result": result,
                    "duration_ms": duration_ms,
                    "turn": turn,
                },
            )
            if post.system_message:
                self._say(f"{_YELLOW}⚠ {post.system_message}{_RESET}")
            if post.updated_output is not None:
                result = post.updated_output  # 改写回填给模型的观察文本
            if post.additional_context:
                result = f"{result}\n[hook 上下文] {post.additional_context}"

        self._say(
            f"{_CYAN}⟳ {name}{_RESET} {_DIM}{self._brief(tc.function.arguments)}{_RESET}"
        )
        self._say(
            f"  {'← ' + self._brief(result, 80) if not is_error else _RED + '← ' + self._brief(result, 80) + _RESET}"
            f" {_DIM}({duration_ms:.0f} ms){_RESET}"
        )
        return ToolEvent(
            turn=turn,
            name=name,
            arguments=json.loads(tc.function.arguments) if _is_json(tc.function.arguments) else {"_raw": tc.function.arguments},
            result=result,
            duration_ms=duration_ms,
            is_error=is_error,
        )

    def _accumulate_usage(self, response: Any, state: LoopState) -> None:
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        state.input_tokens += getattr(usage, "prompt_tokens", 0) or 0
        state.output_tokens += getattr(usage, "completion_tokens", 0) or 0

    # ------------------------------------------------------------------
    # 输出辅助
    # ------------------------------------------------------------------

    def _say(self, text: str) -> None:
        if self.verbose:
            with self._print_lock:  # 并发执行时避免多线程打印交错撕裂
                print(text)

    @staticmethod
    def _brief(text: str | None, limit: int = 60) -> str:
        s = (text or "").replace("\n", "\\n")
        return s if len(s) <= limit else s[: limit - 3] + "..."


def _is_json(raw: str | None) -> bool:
    try:
        json.loads(raw or "")
        return True
    except (json.JSONDecodeError, TypeError):
        return False


def _parse_args_for_check(tc: Any) -> dict:
    """并发预检时安全解析参数（解析失败 → 空字典，串行路径会再报错）。"""
    try:
        args = json.loads(tc.function.arguments or "{}")
        return args if isinstance(args, dict) else {}
    except json.JSONDecodeError:
        return {}
