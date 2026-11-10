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
from pathlib import Path
from typing import Callable

from .agent import Agent, RunResult
from .compaction import (
    COMPACT_BOUNDARY_PREFIX,
    CompactionConfig,
    CompactionReport,
    auto_compact,
    run_pipeline,
)
from .memory import UserMemory, extract_memories
from .prompt import SectionRegistry, build_system_prompt
from .transcript import RestoredSession, TranscriptWriter, load_transcript


@dataclass
class SessionStats:
    """跨轮累计的会话统计（对应 CC 的 cost-tracker 会话累计）。"""

    turns: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    tool_calls: int = 0
    tool_errors: int = 0
    # 迭代 6：压缩观测
    compactions: int = 0          # auto 触发次数
    tokens_saved: int = 0         # 压缩节省的估算 token
    # 迭代 7：记忆观测
    memories_saved: int = 0       # 本会话新保存的记忆条数
    # 迭代 8：持久化观测
    resumed: bool = False         # 本会话是从 transcript 恢复的


class QueryEngine:
    """多轮会话引擎：Agent + Section 注册表 + 唯一的对话历史 + 压缩管线。"""

    def __init__(
        self,
        agent: Agent,
        registry: SectionRegistry,
        compaction_config: CompactionConfig | None = None,
        summarizer: Callable[[str], str] | None = None,
        memory: UserMemory | None = None,
        transcript: TranscriptWriter | None = None,
    ) -> None:
        self.agent = agent
        self.registry = registry
        self.compaction_config = compaction_config or CompactionConfig()
        self._summarizer = summarizer  # None → 用 agent 的 client 现查
        self.memory = memory  # 迭代 7：跨会话记忆（None → 功能关闭）
        self._surfaced: set[str] = set()  # 已展示去重（对应 CC alreadySurfaced）
        # 迭代 8：transcript 持久化（None → 功能关闭，兼容旧测试）
        self.transcript = transcript
        self.session_meta = None  # transcript 开启时由 _init_transcript 填充
        self.messages: list[dict] = []
        self.stats = SessionStats()
        self.history: list[RunResult] = []  # 每轮的 RunResult（观测用）
        self.last_compaction: CompactionReport | None = None
        self._auto_attempts = 0  # auto 熔断计数
        # 迭代 8 修正：**惰性开档**——不在构造时 new_session：
        # CLI 装配顺序是先构造 engine、后 restore_from；构造即开档会先留下
        # 一个只有 meta 的孤儿文件，且 --resume latest 可能恢复到这个空档。
        # 改为首次 submit 时才开新档（restore_from 则直接覆写为恢复的目标档）

    def _ensure_session_open(self) -> None:
        """首次 submit 时开新档（写 session_meta 首行）。已开档/已恢复 → 幂等跳过。"""
        if self.transcript is None or self.session_meta is not None:
            return
        self.session_meta = self.transcript.new_session(
            model=self.agent.model, cwd=str(Path.cwd())
        )

    # ------------------------------------------------------------------

    def _resolve_summarizer(self) -> Callable[[str], str] | None:
        """摘要器：注入优先；否则用 agent 的 client 现查；异常降级截断。"""
        if self._summarizer is not None:
            return self._summarizer
        client = self.agent.client
        model = self.agent.model

        def _llm_summarize(text: str) -> str:
            try:
                resp = client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": (
                            "把对话历史压缩为要点摘要：保留任务目标、关键决策、"
                            "重要文件路径与数据、未完成事项。用中文，简洁分点。"
                        )},
                        {"role": "user", "content": text},
                    ],
                )
                return resp.choices[0].message.content or "（摘要为空）"
            except Exception:  # 摘要失败 → 降级为截断式，压缩永不崩
                return "\n".join(text.splitlines()[:50])[:2000]

        return _llm_summarize

    def compact_now(self, custom_instruction: str = "") -> CompactionReport:
        """手动压缩（对应 CC /compact，可附自定义聚焦指令）。"""
        pre = len(self.messages)
        if pre <= 2:
            return CompactionReport(pre_tokens=0, post_tokens=0)
        summarize = self._resolve_summarizer()
        instruction = (
            f"（用户要求聚焦：{custom_instruction}）" if custom_instruction else ""
        )
        if summarize is None:
            return CompactionReport(pre_tokens=0, post_tokens=0)
        wrapped = (lambda text: summarize(text + instruction)) if instruction else summarize
        self.messages, _summary = auto_compact(
            self.messages, wrapped, self.compaction_config, boundary_type="manual"
        )
        self._auto_attempts += 1
        # 迭代 8：手动压缩的 boundary 也是事件，同样落盘（恢复时切片消费）
        self._persist_compaction_boundary()
        report = CompactionReport(
            pre_tokens=pre,
            post_tokens=len(self.messages),
            auto_triggered=True,
        )
        self.last_compaction = report
        return report

    def _persist_compaction_boundary(self) -> None:
        """把本轮压缩产生的 boundary 消息落盘（迭代 8 修正）。

        run_pipeline 替换内存 messages 后，boundary 位于列表前部（turn_start_len
        之前），常规 flush 写不到它——必须显式落盘。否则恢复时拿不到压缩结果，
        每个新进程都要重新 LLM 摘要一次。snip/micro 的占位同理不落盘，但它们
        无 LLM 成本、恢复后确定性重放即可，无需持久化。
        """
        if self.transcript is None or self.session_meta is None:
            return
        for m in self.messages:
            if str(m.get("content", "")).startswith(COMPACT_BOUNDARY_PREFIX):
                self.transcript.append_message(self.session_meta.session_id, m)
                return

    def _flush_turn_to_transcript(self, start_len: int) -> None:
        """把本轮新增的消息追加写入 transcript（append-only 事件流水）。"""
        if self.transcript is None or self.session_meta is None:
            return
        sid = self.session_meta.session_id
        for msg in self.messages[start_len:]:
            self.transcript.append_message(sid, msg)
        self.transcript.append_turn_stats(
            sid, self.stats.turns,
            self.stats.input_tokens, self.stats.output_tokens,
        )

    def restore_from(self, path) -> RestoredSession:
        """从 transcript 文件恢复会话（对应 CC --resume 链路）。

        恢复全部对话消息 + 累计 stats；恢复后**直接续写目标档**（构造时
        不再预开新档——否则每次 --resume 都会多出一个孤儿空档）。
        """
        if self.transcript is None:
            raise RuntimeError("restore_from 需要 transcript 已启用")
        restored = load_transcript(path)
        # boundary 感知恢复（对应 CC getMessagesAfterBoundary）：transcript 是
        # 事实流水——压缩前的旧消息永远留在文件里；若会话压缩过，文件里会有
        # boundary 事件。恢复时从最后一个 boundary（含）开始切：拿到的就是
        # 压缩后的工作集（摘要+尾部），无需重新 LLM 摘要。
        msgs = restored.messages
        last_boundary = -1
        for i, m in enumerate(msgs):
            if str(m.get("content", "")).startswith(COMPACT_BOUNDARY_PREFIX):
                last_boundary = i
        if last_boundary >= 0:
            msgs = msgs[last_boundary:]
            # 切片后没有 system；下一轮 submit 会 insert(0) 补上当前 system
            restored = RestoredSession(
                restored.meta, msgs, restored.turns,
                restored.input_tokens, restored.output_tokens, restored.skipped_lines,
            )
        self.messages = msgs
        self.session_meta = restored.meta  # 续写同一文件
        self.stats = SessionStats(
            turns=restored.turns,
            input_tokens=restored.input_tokens,
            output_tokens=restored.output_tokens,
            resumed=True,
        )
        # 熔断计数不跨进程；恢复后若水位仍高，下轮 run_pipeline 自然再触发
        self._auto_attempts = 0
        return restored

    def submit_message(self, text: str) -> RunResult:
        """提交一条用户消息，跑一轮完整 Agent Loop，返回该轮结果。

        messages 跨轮保留（上下文维护的核心）：
        第 N 轮请求 = system + 前 N-1 轮全部消息 + 本轮 user 消息。
        """
        # ---- 迭代 6：压缩管线（调模型之前，每轮执行） ----
        # budget 已在 agent 回填时即时处理；此处跑 snip → micro → auto
        self.messages, report = run_pipeline(
            self.messages, self.compaction_config, self._resolve_summarizer(),
            auto_attempts=self._auto_attempts,
        )
        if report.auto_triggered or report.reactive_triggered:
            self._auto_attempts += 1  # 熔断计数
            # 迭代 8：boundary 是事件，落盘（恢复时切片消费，避免重复摘要）
            self._persist_compaction_boundary()
        self.stats.compactions += 1 if report.auto_triggered else 0
        self.stats.tokens_saved += report.tokens_saved
        self.last_compaction = report

        # 每轮组装 system prompt（静态段命中缓存；动态段确定性比较）
        self._ensure_session_open()
        system = build_system_prompt(self.registry.resolve())
        if not self.messages:
            if system:
                self.messages.append({"role": "system", "content": system})
        elif self.messages[0].get("role") == "system":
            if self.messages[0].get("content") != system:
                # 动态区变化（工具增删 / cwd 切换 / 新记忆）→ 替换 system，
                # 静态前缀不变的部分在真实 API 上仍可命中 prompt cache
                self.messages[0] = {"role": "system", "content": system}
        elif system:
            # 恢复/压缩后的列表可能以 boundary(user) 开头：插入而非替换，
            # 否则 boundary 会被当 system 覆掉（潜在毁灭性 bug）
            self.messages.insert(0, {"role": "system", "content": system})

        # 迭代 8：记录本轮起点（transcript 增量落盘用；须在管线/system
        # 之后，避免把上一轮已写过的事件重复写入）
        turn_start_len = len(self.messages)

        # ---- 迭代 7：side-query 按需注入（≤5 条，已展示去重） ----
        # 命中时注入到本轮 user 消息之前——历史前缀不变，不破坏 cache
        if self.memory is not None:
            hits = self.memory.side_query(text, exclude_contents=self._surfaced)
            if hits:
                lines = "\n".join(f"- [{h.category}] {h.content}" for h in hits)
                self.messages.append(
                    {"role": "user", "content": f"[相关记忆]（按需注入，引用前验证时效）\n{lines}"}
                )
                self._surfaced.update(h.content for h in hits)

        result = self.agent.run_turn(self.messages, text)

        # 跨轮累计（对应 CC 会话级 cost 累计）
        self.stats.turns += 1
        self.stats.input_tokens += result.input_tokens
        self.stats.output_tokens += result.output_tokens
        self.stats.tool_calls += result.tool_calls_count
        self.stats.tool_errors += sum(1 for e in result.tool_events if e.is_error)
        self.history.append(result)

        # ---- 迭代 7：轮后提取（显式「记住」/ 偏好句式 → 跨会话保存） ----
        if self.memory is not None:
            for entry in extract_memories(text, result.final_text):
                if self.memory.save(entry):
                    self.stats.memories_saved += 1  # 下轮 memory 段缓存失效自动反映

        # ---- 迭代 8：本轮增量落盘（崩溃后最多丢当前轮） ----
        self._flush_turn_to_transcript(turn_start_len)

        return result

    # ------------------------------------------------------------------

    def clear(self) -> None:
        """清空会话（对应 CC /clear）：丢弃全部历史、section 缓存与熔断计数。
        跨会话记忆不受影响（记忆的生命周期 = 磁盘文件，非会话）。
        迭代 8：transcript 开新档（新 session 文件）——旧文件保留供回溯，
        新对话不再写入旧文件（否则恢复旧档会重放已清空的历史）。"""
        self.messages = []
        self.registry.reset()
        self.stats = SessionStats()
        self.history = []
        self.last_compaction = None
        self._auto_attempts = 0
        self._surfaced = set()
        # 惰性开档：清空 session_meta → 下次 submit 自动开新档（_ensure_session_open）
        self.session_meta = None

    def message_count(self) -> int:
        return len(self.messages)

    def cache_hit_rate(self) -> float:
        return self.registry.stats.hit_rate()
