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
    ) -> None:
        self.client = client
        self.tools: dict[str, Tool] = {t.name: t for t in tools}
        self.system_prompt = system_prompt
        self.max_turns = max_turns
        self.verbose = verbose
        self.model = model
        self.max_workers = max_workers
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
            self._say(f"{_DIM}⚡ {len(tool_calls)} 个只读工具 → 并发执行{_RESET}")
            with ThreadPoolExecutor(
                max_workers=min(self.max_workers, len(tool_calls))
            ) as pool:
                futures = [
                    pool.submit(self._execute_tool_call, turn, tc) for tc in tool_calls
                ]
                return [fut.result() for fut in futures]  # futures 按提交序 → 结果天然保序

        return [self._execute_tool_call(turn, tc) for tc in tool_calls]

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

        任何失败（JSON 解析失败/未知工具/参数错误/运行异常）都不抛出，
        而是转成错误文本作为观察结果回填 —— 模型读到错误后可自行修正重试。
        这对应 claude-code「错误即时恢复」的设计。
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
            result = tool.run(arguments)
            is_error = False
        except ToolError as e:
            result, is_error = f"[工具错误] {e}", True
        except json.JSONDecodeError as e:
            result, is_error = f"[参数解析失败] {e}", True
        except Exception as e:  # noqa: BLE001 —— 工具内部任何异常都不允许打断循环
            result, is_error = f"[工具异常] {type(e).__name__}: {e}", True

        duration_ms = (time.perf_counter() - started) * 1000
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
