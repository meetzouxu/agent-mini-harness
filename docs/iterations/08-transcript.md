# 迭代 8：保存状态 —— JSONL Transcript 与会话恢复

> 代码：`mini_agent/transcript.py`（写入/恢复/崩溃容错）｜`mini_agent/session.py`（自动持久化 + restore_from）
> 测试：`tests/test_transcript.py`（11 个新单测）
> 参考资料：[claude-code-best/claude-code](https://github.com/claude-code-best/claude-code) `docs/conversation/multi-turn.mdx` 的 sessionStorage 章节（JSONL 追加写入 / --resume 链路 / 50MB 上限）

## 0. 这一迭代要解决什么问题

迭代 5-7 让上下文与记忆跨轮、跨会话可用，但 agent 本身仍是「关机即失忆」：
Ctrl+C 或崩溃后，`engine.messages` 灰飞烟灭——哪怕上一秒刚读完 20 个文件。

「保存状态」= **把会话事件流持久化到磁盘**。Claude Code 的答案是 JSONL
transcript：`~/.claude/projects/<dir>/<session-uuid>.jsonl`，一行一条事件，
append-only。本轮完整复刻（教学版存 `sessions/<id>.jsonl`，已 gitignore）。

## 1. 环境（无新增依赖）

零新增依赖（json/uuid/datetime 标准库）。

## 2. 代码结构拆解

```
mini_agent/
├── transcript.py   # ★ TranscriptWriter（append-only 落盘）/ load_transcript（恢复+容错）
│                   #   / list_sessions / resolve_resume_target（--resume 三形态）
└── session.py      # QueryEngine：每轮增量落盘 _flush_turn_to_transcript + restore_from
tests/
└── test_transcript.py  # 11 个新单测
```

### 2.1 为什么是 JSONL 而不是「整个 JSON 文件」

| 维度 | JSONL（一行一条） | 整体 JSON 文件 |
|------|------------------|---------------|
| 写入 | **追加一行**（O(1)） | 读全文→改→重写全文（O(n)） |
| 崩溃 | 最多损毁**最后一行**，前面的完整 | 可能整个文件损坏 |
| 读取 | 流式逐行，可只读尾部 | 必须整体解析 |
| 定位 | `tail -1` 即最新状态 | 无此便利 |

这就是数据库 WAL（write-ahead log）与 append-only 日志的同款设计。
CC 在此之上加了异步写队列 + 定时刷盘（多并发源不互相覆盖）——教学版
REPL 单线程同步写即可，无并发覆盖风险。

### 2.2 会话生命周期（模块分组图）

```mermaid
flowchart TD
    subgraph SESSION["session.py · QueryEngine"]
        NEW["new_session()<br/>写 session_meta 首行"] --> SUB["submit_message(text)"]
        SUB --> TSL["turn_start_len = len(messages)"]
        TSL --> LOOP["压缩管线 → system → run_turn"]
        LOOP --> FLUSH["_flush_turn_to_transcript(start_len)<br/>本轮新增消息 + turn_stats"]
        FLUSH --> NEXT["下一轮"]
        RESTORE["restore_from(path)<br/>恢复 messages + stats<br/>续写同一 session_id"]
    end

    subgraph TMOD["transcript.py"]
        W["TranscriptWriter<br/>append-only 单行写入"]
        L["load_transcript(path)<br/>逐行解析 + 半行跳过计数<br/>+ 孤儿 tool_call 清理"]
        LS["list_sessions()<br/>按 mtime 倒序"]
        RR["resolve_resume_target<br/>latest / id / 路径"]
    end

    subgraph COMP["compaction.py · 复用"]
        INV["preserve_api_invariants()<br/>崩溃截断的孤儿清理<br/>(迭代 6 机制第四次兑现)"]
    end

    subgraph CLI["__main__.py"]
        RES["--resume 参数"]
        LST["--list-sessions"]
    end

    FLUSH --> W
    RESTORE --> L
    L --> INV
    CLI_RES["进程重启"] --> RES --> RR
    RES -.-> RESTORE
    LST --> LS
```

### 2.3 记录格式与文件内容（数据变化实例）

一次两轮 REPL 会话（第一轮读 hello.txt，第二轮总结）落盘后的
`sessions/a1b2c3d4e5f6.jsonl` **全文**——每行一条 JSON：

```json
{"type": "session_meta", "session_id": "a1b2c3d4e5f6", "created_at": "2026-10-28T11:00:00", "cwd": "/Users/nian/CatPaw/agent-mini-harness", "model": "deepseek-chat"}
{"type": "message", "role": "system", "content": "你是 mini-agent…（含工具清单/工作区/记忆索引）"}
{"type": "message", "role": "user", "content": "读 hello.txt"}
{"type": "message", "role": "assistant", "content": null, "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": "{\"path\": \"hello.txt\"}"}}]}
{"type": "message", "role": "tool", "tool_call_id": "c1", "name": "read_file", "content": "1\thello mini-agent"}
{"type": "message", "role": "assistant", "content": "文件内容是 hello mini-agent"}
{"type": "turn_stats", "turn": 1, "input_tokens": 812, "output_tokens": 47}
{"type": "message", "role": "user", "content": "总结"}
{"type": "message", "role": "assistant", "content": "读到了 hello mini-agent"}
{"type": "turn_stats", "turn": 2, "input_tokens": 1310, "output_tokens": 35}
```

三种记录类型的分工：

| type | 内容 | 作用 |
|------|------|------|
| `session_meta` | id/创建时间/cwd/model | 文件首行；list_sessions 与 resume 定位 |
| `message` | 一条对话消息全部字段 | 恢复 messages 的唯一来源 |
| `turn_stats` | turn 序号 + token 用量 | 恢复累计统计（CC restoreCostStateForSession） |

**增量落盘的关键**：`turn_start_len` 在压缩管线与 system 组装**之后**记录——
本轮只写「system 替换、user、assistant、tool、stats」这些**新**事件，
上一轮已写过的永不重写（对应 CC recordTranscript 的事件流水语义）。

### 2.4 恢复（--resume）的数据变化

进程重启后 `python -m mini_agent --resume latest`：

```text
load_transcript("sessions/a1b2c3d4e5f6.jsonl")
  逐行解析 10 行 → meta(1) + messages(8) + turn_stats(2)

restored.messages（重建的内存状态）:
  [0] system  （与落盘时逐字节一致）
  [1..7] 第一轮与第二轮的全部消息
restored.stats: turns=2, input_tokens=2122, output_tokens=82

engine2.restore_from(path) 之后:
  engine2.messages        = 8 条完整上下文 ← 模型看得到两轮历史
  engine2.session_meta    = 原 meta（续写同一文件，不另开新档）
  engine2.stats           = turns=2 …（累计恢复）, resumed=True
  engine2._auto_attempts  = 0（熔断计数不跨进程）

用户继续说「继续」→ 新事件追加到同一文件:
  {"type": "message", "role": "user", "content": "继续"}
  {"type": "message", "role": "assistant", "content": "…"}
  {"type": "turn_stats", "turn": 3, …}     ← 文件里 turn=3 出现，续写成功
```

### 2.5 崩溃容错（append-only 的红利）

两层防线（都有单测锁定）：

1. **半行 JSON**（进程被杀在写入中途）：`json.loads` 失败 → 跳过该行并计入
   `skipped_lines`（恢复时向用户报告「跳过 N 行损坏记录」）——前面的行完整无损
2. **孤儿 tool_call**（崩溃在 assistant(tool_calls) 与 tool 消息之间）：
   恢复时经 `preserve_api_invariants` 清理（迭代 6 的配对保护第四次兑现——
   压缩、恢复共用同一不变量，**宁可保守也不发非法请求**）

CC 的 50MB 读取上限（防超大会话 OOM）同样复刻。

### 2.6 与压缩的协作（boundary 不重复压缩）

transcript 照实记录一切事件（fact stream）——包括压缩后的消息形态。
恢复以**文件末尾的消息序列**为准：若上次会话已 auto 压缩，恢复的 messages
第一条就是 `[compact_boundary …]` 摘要——**不需要也不应该再次压缩**
（对应 CC getMessagesAfterBoundary 的恢复语义）。熔断计数跨进程归零是
安全的：新进程若水位仍高，下轮 run_pipeline 自然再触发。

## 3. 运行指令

```bash
cd /Users/nian/CatPaw/agent-mini-harness

# 单测（158 个，无需 key）
.venv/bin/python -m unittest discover -s tests -v

# 完整体感：两进程接力
.venv/bin/python -m mini_agent
mini-agent> 读一下 docs/ROADMAP.md 的迭代规划
mini-agent> exit
ls sessions/                    # ← 会话已自动落盘

.venv/bin/python -m mini_agent --resume latest
# ↻ 已恢复会话 a1b2c3d4e5f6（2026-10-28T11:00:00，1 轮，N 条消息）
mini-agent> 接着刚才的，下一步是什么迭代？
# ← 模型基于恢复的上下文回答（跨进程记忆的直接证据）

.venv/bin/python -m mini_agent --list-sessions   # 查看全部会话
head -1 sessions/<id>.jsonl                       # 人读文件：首行即元信息
```

## 4. 测试如何验证（11 个新单测）

| 组 | 验证点 |
|----|--------|
| WriterTests ×3 | meta 首行、JSONL 行格式（中文不转义）、目录删除后 mkdir 重试 |
| RestoreTests ×5 | 两轮完整往返（messages+stats）、**半行崩溃跳过计数**、**孤儿 tool_call 清理**、缺 meta 拒绝、list 倒序 |
| EngineIntegrationTests ×3 | **跨进程接力**（新 engine 恢复后续写同一文件、turn=3 落盘）、transcript=None 向后兼容、resume 三形态解析 |

开发中修掉两个集成 bug（都有普适教学价值，写进 2.5）：
`return result` 误置于 stats 累计之前（死代码）；`SimpleNamespace` 对象
无法 JSON 序列化——`_plain_message` 落盘前规约（与迭代 6 的 `_tc_field`
同源：假客户端的浅拷贝残留是反复出现的坑）。

## 5. 本迭代刻意不做的事

- 异步写队列 + 定时刷盘（CC 为多并发源设计；教学版单线程同步写即可）
- 远程持久化（CCR v2 internalEventWriter / Ingress 上传）
- --rewind-files（把工作区文件回滚到某消息时刻的快照）
- 多项目目录隔离（CC 按 sanitized-cwd 分目录；教学版单目录）
- 密钥/脱敏处理（内部 transcript 明文——.gitignore 已隔离）

## 6. 已知局限

- 每轮同步写盘有 IO 开销（REPL 场景无感；高频 agent 服务需批量刷盘）
- turn_stats 是累计值快照而非增量（恢复取 max(turn) 与求和的混合语义，够用但不严格）
- 恢复不还原权限管理器状态（会话级 always 规则/拒绝计数随进程消失——安全侧的可接受损失）
