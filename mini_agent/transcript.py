"""JSONL Transcript 会话持久化 —— 迭代 8 的核心。

复刻 Claude Code 的会话存储（参考 claude-code
docs/conversation/multi-turn.mdx 的 sessionStorage 段落）。

CC 设计要点 → 教学版：
    ~/.claude/projects/<sanitized-cwd>/<uuid>.jsonl   → sessions/<uuid>.jsonl
    recordTranscript + 异步写队列 + 定时刷盘          → 每轮 submit 后同步 flush
                                                       （教学版 REPL 单线程，
                                                        无并发覆盖风险）
    loadTranscriptFromFile + 重建 Message[]           → load_transcript()
    恢复费用/agent 设置                                → 恢复累计 stats

核心概念：
- **JSONL（JSON Lines）**：一行一条 JSON 记录。选它而非「整个 JSON 文件」
  的理由与 append-only 日志相同——追加写入无需读-改-写整个文件；
  崩溃时最多损毁最后一行（部分行），前面的记录完整。
- **append-only**：transcript 是事件流水（fact），不是状态快照——
  压缩/清空等操作只影响内存中的 messages，transcript 照实记录发生了什么。
  恢复时以最后状态为准（见 restore 的 boundary 语义）。

记录类型（type 字段区分）：
    session_meta  会话元信息（id/创建时间/cwd/model）——文件首行
    message       一条对话消息（role/content/tool_calls/tool_call_id/name）
    turn_stats    一轮结束时的统计（turn 序号 + token 用量）

崩溃安全：
- 半行 JSON（进程被杀在写入中途）→ 解析失败的行跳过并告警
- assistant(tool_calls) 后被截断（tool 消息缺失）→ 恢复时经
  preserve_api_invariants 清理孤儿（迭代 6 的机制第四次兑现）
"""

from __future__ import annotations

import json
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .compaction import preserve_api_invariants

# 会话目录（gitignored；CC 为 ~/.claude/projects/<sanitized-cwd>/）
SESSIONS_DIR = "sessions"

# 读取上限（对应 CC MAX_TRANSCRIPT_READ_BYTES，防 OOM）
MAX_READ_BYTES = 50 * 1024 * 1024

RECORD_TYPES = ("session_meta", "message", "turn_stats")


@dataclass
class SessionMeta:
    """会话元信息（文件首行）。"""

    session_id: str
    created_at: str
    cwd: str
    model: str


class TranscriptWriter:
    """把会话事件追加写入 JSONL 文件。"""

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or Path(SESSIONS_DIR)

    def new_session(self, model: str, cwd: str | None = None) -> SessionMeta:
        """开新会话：生成 id + 写 session_meta 首行。"""
        meta = SessionMeta(
            session_id=uuid.uuid4().hex[:12],
            created_at=datetime.now().isoformat(timespec="seconds"),
            cwd=str(cwd or Path.cwd()),
            model=model,
        )
        self.root.mkdir(parents=True, exist_ok=True)
        self._append(self._path(meta.session_id), {"type": "session_meta", **meta.__dict__})
        return meta

    def append_message(self, session_id: str, message: dict) -> None:
        """追加一条对话消息（含 role/content/tool_calls 等全部字段）。

        tool_calls 可能含对象形态（假客户端的 SimpleNamespace 浅拷贝残留，
        迭代 6 的 _tc_field 已兼容读取）——落盘前统一规约为纯 dict，
        恢复侧拿到的必然是干净的可序列化数据。
        """
        record = {"type": "message", **_plain_message(message)}
        self._append(self._path(session_id), record)

    def append_turn_stats(self, session_id: str, turn: int, input_tokens: int, output_tokens: int) -> None:
        self._append(
            self._path(session_id),
            {"type": "turn_stats", "turn": turn, "input_tokens": input_tokens,
             "output_tokens": output_tokens},
        )

    def _path(self, session_id: str) -> Path:
        return self.root / f"{session_id}.jsonl"

    def _append(self, path: Path, record: dict) -> None:
        """单条追加（对应 CC appendEntryToFile 同步路径；失败时 mkdir 重试）。"""
        try:
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except FileNotFoundError:
            self.root.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _plain_message(message: dict) -> dict:
    """把消息规约为纯 dict：tool_calls 里的对象形态转 dict。"""
    out = {}
    for key, value in message.items():
        if key == "tool_calls" and value:
            plain_calls = []
            for tc in value:
                if isinstance(tc, dict):
                    fn = tc.get("function")
                    if fn is not None and not isinstance(fn, dict):
                        fn = {"name": getattr(fn, "name", ""),
                              "arguments": getattr(fn, "arguments", "")}
                    plain_calls.append({**tc, "function": fn} if fn is not None else tc)
                else:
                    fn = getattr(tc, "function", None)
                    plain_calls.append({
                        "id": getattr(tc, "id", ""),
                        "type": "function",
                        "function": {
                            "name": getattr(fn, "name", ""),
                            "arguments": getattr(fn, "arguments", ""),
                        },
                    })
            out[key] = plain_calls
        else:
            out[key] = value
    return out


# ---------------------------------------------------------------------------
# 读取与恢复
# ---------------------------------------------------------------------------

@dataclass
class RestoredSession:
    """load_transcript 的产出：恢复一个会话所需的全部状态。"""

    meta: SessionMeta
    messages: list[dict]
    turns: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    skipped_lines: int = 0  # 崩溃截断的损坏行数（可观测性）


def load_transcript(path: Path) -> RestoredSession:
    """从 JSONL 文件恢复会话：解析 → 过滤 message → 重建 → 配对清理。"""
    raw = path.read_text(encoding="utf-8", errors="replace")
    if len(raw.encode("utf-8")) > MAX_READ_BYTES:
        raise SystemExit(f"transcript 超过读取上限 {MAX_READ_BYTES} 字节: {path}")

    meta: SessionMeta | None = None
    messages: list[dict] = []
    turns = input_tokens = output_tokens = skipped = 0

    for line_no, line in enumerate(raw.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            skipped += 1  # 崩溃截断的半行 → 跳过（append-only 的容错红利）
            continue
        rtype = record.get("type")
        if rtype == "session_meta":
            meta = SessionMeta(
                session_id=record.get("session_id", ""),
                created_at=record.get("created_at", ""),
                cwd=record.get("cwd", ""),
                model=record.get("model", ""),
            )
        elif rtype == "message":
            msg = {k: v for k, v in record.items() if k != "type"}
            messages.append(msg)
        elif rtype == "turn_stats":
            turns = max(turns, record.get("turn", 0))
            input_tokens += record.get("input_tokens", 0)
            output_tokens += record.get("output_tokens", 0)

    if meta is None:
        raise SystemExit(f"transcript 缺少 session_meta 首行: {path}")

    # 崩溃截断保护：最后的 assistant(tool_calls) 可能没有配对 tool
    # → 清孤儿（宁可保守也不发非法请求，与压缩层共用同一不变量）
    messages = preserve_api_invariants(messages)
    return RestoredSession(meta, messages, turns, input_tokens, output_tokens, skipped)


def list_sessions(root: Path | None = None) -> list[SessionMeta]:
    """列出全部会话（按创建时间倒序，供 --resume 选择）。"""
    root = root or Path(SESSIONS_DIR)
    if not root.exists():
        return []
    out: list[SessionMeta] = []
    for path in sorted(root.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            first = path.read_text(encoding="utf-8").splitlines()[0]
            record = json.loads(first)
            if record.get("type") == "session_meta":
                out.append(SessionMeta(
                    session_id=record.get("session_id", path.stem),
                    created_at=record.get("created_at", ""),
                    cwd=record.get("cwd", ""),
                    model=record.get("model", ""),
                ))
        except (OSError, json.JSONDecodeError, IndexError):
            continue  # 空/损坏文件跳过
    return out


def resolve_resume_target(target: str, root: Path | None = None) -> Path:
    """解析 --resume 参数（对应 CC main.ts 的三形态）：

    - "latest"       → 最近修改的会话文件
    - <session_id>   → sessions/<id>.jsonl
    - <文件路径>     → 直接使用
    """
    root = root or Path(SESSIONS_DIR)
    if target == "latest":
        sessions = sorted(root.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True) if root.exists() else []
        if not sessions:
            raise SystemExit("没有可恢复的会话（sessions/ 为空）")
        return sessions[0]
    candidate = Path(target)
    if candidate.exists():
        return candidate
    by_id = root / f"{target}.jsonl"
    if by_id.exists():
        return by_id
    raise SystemExit(f"找不到会话: {target}（可用 --list-sessions 查看）")
