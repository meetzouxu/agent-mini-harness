"""QueryEngine 多轮会话引擎 —— 迭代 5 的核心之二。

对应 Claude Code 的多轮对话管理（参考 claude-code
docs/conversation/multi-turn.mdx 的 onQueryImpl / submitMessage 链路）：

    CC REPL 调用链路：                     教学版：
    onSubmit → handlePromptSubmit          QueryEngine.submit_message()
      → onQueryImpl → query()               → registry.resolve()（system prompt 组装）
        （每轮重建 system prompt）           → agent.run_turn(messages, text)
                                             （messages 跨轮复用 = 维护上下文）

两个职责：
1. **上下文维护**：持有唯一的 messages 历史，每轮 append 而非重建——
   第 N 轮的模型请求里完整包含前 N-1 轮的 user/assistant/tool 消息
2. **system prompt 每轮组装**：resolve sections（确定性缓存），
   内容变化（工具增删/目录切换）时替换 messages[0]——静态前缀稳定不变，
   最大化 prompt cache 命中
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .agent import Agent, RunResult
from .prompt import SectionRegistry, build_system_prompt


@dataclass
class SessionStats:
    """跨轮累计的会话统计（对应 CC 的 cost-tracker 会话累计）。"""

    turns: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    tool_calls: int = 0
    tool_errors: int = 0


class QueryEngine:
    """多轮会话引擎：Agent + Section 注册表 + 唯一的对话历史。"""

    def __init__(self, agent: Agent, registry: SectionRegistry) -> None:
        self.agent = agent
        self.registry = registry
        self.messages: list[dict] = []
        self.stats = SessionStats()
        self.history: list[RunResult] = []  # 每轮的 RunResult（观测用）

    # ------------------------------------------------------------------

    def submit_message(self, text: str) -> RunResult:
        """提交一条用户消息，跑一轮完整 Agent Loop，返回该轮结果。

        messages 跨轮保留（上下文维护的核心）：
        第 N 轮请求 = system + 前 N-1 轮全部消息 + 本轮 user 消息。
        """
        # 每轮组装 system prompt（静态段命中缓存；动态段确定性比较）
        system = build_system_prompt(self.registry.resolve())
        if not self.messages:
            if system:
                self.messages.append({"role": "system", "content": system})
        elif self.messages[0].get("content") != system:
            # 动态区变化（工具增删 / cwd 切换）→ 替换 system，
            # 静态前缀不变的部分在真实 API 上仍可命中 prompt cache
            self.messages[0] = {"role": "system", "content": system}

        result = self.agent.run_turn(self.messages, text)

        # 跨轮累计（对应 CC 会话级 cost 累计）
        self.stats.turns += 1
        self.stats.input_tokens += result.input_tokens
        self.stats.output_tokens += result.output_tokens
        self.stats.tool_calls += result.tool_calls_count
        self.stats.tool_errors += sum(1 for e in result.tool_events if e.is_error)
        self.history.append(result)
        return result

    # ------------------------------------------------------------------

    def clear(self) -> None:
        """清空会话（对应 CC /clear）：丢弃全部历史与 section 缓存。"""
        self.messages = []
        self.registry.reset()
        self.stats = SessionStats()
        self.history = []

    def message_count(self) -> int:
        return len(self.messages)

    def cache_hit_rate(self) -> float:
        return self.registry.stats.hit_rate()
