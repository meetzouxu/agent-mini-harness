# mini-agent 渐进式迭代路线图

目标：用 Python 从零复刻 Claude Code 的核心能力，每次迭代只加一个主题，
代码保持「一个下午能读完」的规模。

**参考来源**（唯一能力参考，公开可查）：
- [claude-code-best/claude-code](https://github.com/claude-code-best/claude-code) —— Claude Code CLI 的逆向开源仓库（完整源码 `src/` + 白皮书 `docs/`）：`the-loop.mdx`、`multi-turn.mdx`、`compaction.mdx` 等
- 能力全景与机制细节：[references/agent-capability-map.md](references/agent-capability-map.md)

八大核心能力全覆盖：
维护上下文 → 迭代 5/6/7；编排工具 → 迭代 2；权限校验 → 迭代 3；
保存状态 → 迭代 8；控制并发 → 迭代 10/12；异步执行 → 迭代 12；
失败重试 → 迭代 11；日志监控 → 迭代 15。

## 阶段总览

| 阶段 | 主题 | 迭代 |
|------|------|------|
| 一 | Agent 基础能力 | 1–4 |
| 二 | 上下文工程 | 5–7 |
| 三 | 状态与规划 | 8–10 |
| 四 | 执行模型 | 11–14 |
| 五 | 可观测性 | 15 |
| 六 | 多 Agent 协作 | 16–18 |
| 七 | 外部能力 | 19–20 |
| 八 | 总装与编排 | 21–23 |

## 迭代明细

### 阶段一：Agent 基础能力

| # | 主题 | 核心内容 | 对应 Claude Code 概念 |
|---|------|---------|----------------------|
| 1 | **Agent Loop** ✅ | while 循环：调模型 → 执行 tool_calls → 回填 tool 消息 → 继续/终止；max_turns 熔断；工具错误回填不崩溃 | `query.ts queryLoop()`、`needsFollowUp`、`State` |
| 2 | **工具编排 Tool Use** ✅ | 查表分发 + 参数校验（required/类型，对应 validateInput）；扩展至 7 工具（bash/edit_file/grep）；`is_concurrency_safe` 只读并发判定（全或无策略：全只读批次线程池并发，混写批次全串行保序） | `TOOL_HANDLERS` 查表、`buildTool()`、`isConcurrencySafe` |
| 3 | **权限管线 Permission** ✅ | 三道闸门：工具硬边界（黑名单/围栏）→ deny 规则 → allow/ask 规则 → 模式默认；allow/deny/ask/passthrough 四态；四模式表（default/plan/acceptEdits/bypass）；Denial Tracking（连续 3 次/总 20 次）；分层配置（settings/cli/session） | `hasPermissionsToUseToolInner`、`permission-model.mdx`、PermissionResult 四态 |
| 4 | **Hooks 扩展点** | 四事件注册表：`UserPromptSubmit / PreToolUse / PostToolUse / Stop`；权限检查从循环体移到 hook；Stop hook 返回字符串可强制续跑；`stopHookActive` 防无限循环 | 27 个 hook 事件、`HookResult` 14 字段 |

### 阶段二：上下文工程

| # | 主题 | 核心内容 | 对应 Claude Code 概念 |
|---|------|---------|----------------------|
| 5 | **维护上下文：多轮会话 + System Prompt 组装** | REPL 多轮对话（`submitMessage` 模式）；PROMPT_SECTIONS 分段（identity/tools/workspace/memory 按需拼接）；context 确定性序列化缓存；工具结果预算细化 | `QueryEngine`、`getSystemContext/getUserContext` |
| 6 | **上下文压缩 Compact** | 四层管线：`applyToolResultBudget`（大结果落盘）→ `snip_compact`（裁中段，保留头 3 尾 47）→ `micro_compact`（旧 tool_result 占位）→ `auto_compact`（LLM 全量摘要）；reactive compact 应急；tool_use/tool_result 配对完整性保护；3 次熔断 | `compaction.mdx`、`adjustIndexToPreserveAPIInvariants` |
| 7 | **记忆系统 Memory** | User/Session Memory 两层；`.memory/` 目录 + MEMORY.md 索引（常驻 system prompt）；四类记忆（user/feedback/project/reference）；side-query 按需注入（≤5 条）；每轮结束提取器 + 阈值触发整理去重 | memory 子系统（两层记忆 + 索引常驻） |

### 阶段三：状态与规划

| # | 主题 | 核心内容 | 对应 Claude Code 概念 |
|---|------|---------|----------------------|
| 8 | **保存状态：会话持久化** | JSONL transcript 落盘（一行一条消息）；`--resume` 会话恢复；usage/费用累计 | `sessionStorage.ts`、JSONL transcript |
| 9 | **TodoWrite 任务规划** | `todo_write` 工具（pending/in_progress/completed 状态列表）；连续 3 轮未规划注入 reminder；规划能力而非执行能力 | `todo_write` 工具、V1 内存清单与 V2 Task System 切换 |
| 10 | **Task System 任务图** | `.tasks/{id}.json` 持久化；`blockedBy` 依赖 DAG + `can_start` 检查；claim/complete 认领解锁；文件锁防并发认领（TOCTOU 防护）；与 TodoWrite V1/V2 并存切换 | `utils/tasks.ts`、proper-lockfile |

### 阶段四：执行模型

| # | 主题 | 核心内容 | 对应 Claude Code 概念 |
|---|------|---------|----------------------|
| 11 | **失败重试：错误恢复** | 路径 1 输出截断：max_tokens 8K→64K 升级 + 续写提示（≤3 次）；路径 2 prompt_too_long：reactive compact 后重试（仅 1 次）；路径 3 临时故障：429/529 指数退避 + 抖动（≤10 次）；连续 3 次 529 → fallback 模型降级 | `attemptWithFallback`、`maxOutputTokensOverride` |
| 12 | **异步执行 + 后台任务** | `run()` 改 async + AsyncGenerator 流式 yield 事件；`run_in_background` 慢操作放 daemon 线程，立即返回 bg_id 占位；`<task_notification>` 完成通知注入下一轮；并发工具按 `isConcurrencySafe` 分派 | 流式 API、`messageQueueManager` |
| 13 | **Subagent 子代理** | `spawn_subagent`：全新 messages[] 上下文隔离、只回传最终结论、禁止递归（子代理无 task 工具）；权限不隔离（PreToolUse 照走）；异步子代理 | Fork 模式 prompt cache 共享、`createSubagentContext` |
| 14 | **Cron 调度** | 五段式 cron 表达式（DOM/DOW OR 语义）；独立调度线程每秒轮询 + minute_marker 防重；queue processor 在 Agent 空闲时交付；durable 任务 `.scheduled_tasks.json` 跨会话恢复 | `useQueueProcessor.ts`、四层调度架构 |

### 阶段五：可观测性

| # | 主题 | 核心内容 | 对应 Claude Code 概念 |
|---|------|---------|----------------------|
| 15 | **日志监控** | 结构化 JSON 日志落盘（logs/）；每次 run 的 trace（turn/tool_event/耗时）；token 与费用统计；PostToolUse hook 挂大输出告警；会话级汇总报表 | `cost-tracker.ts`、telemetry |

### 阶段六：多 Agent 协作

| # | 主题 | 核心内容 | 对应 Claude Code 概念 |
|---|------|---------|----------------------|
| 16 | **Agent Teams** | MessageBus 文件收件箱（`.jsonl` append + 消费式读取）；`spawn_teammate` 队友线程（独立 system prompt / messages / 精简工具集）；Lead inbox 注入 history；权限冒泡（队友 → Lead → 用户） | `inboxes/<agent>.json`、InboxPoller |
| 17 | **Team Protocols** | ProtocolState 状态机（pending → approved/rejected）；request_id 关联请求与响应；shutdown / plan_approval 两类握手；类型校验防串扰；check_inbox 工具 + 主循环末尾双入口 | `match_response`、dispatch_message |
| 18 | **Autonomous Agents** | 三阶段生命周期 WORK → IDLE → SHUTDOWN；idle_poll 每 5s 轮询 inbox + 任务看板；`scan_unclaimed_tasks` 自动认领；60s 超时退出 | 文件锁防重复认领、自治生命周期 |

### 阶段七：外部能力

| # | 主题 | 核心内容 | 对应 Claude Code 概念 |
|---|------|---------|----------------------|
| 19 | **Skill Loading** | `skills/` 目录 + SKILL.md frontmatter（name/description）；两级加载：目录常驻 system prompt（~100 token/技能）+ `load_skill` 按需注入全文（~2000 token）；注册表查找无路径遍历风险 | `loadSkillsDir.ts`、catalog 预算 ~1% |
| 20 | **MCP 工具接入** | MCPClient（register + call_tool）；`connect_mcp` 动态发现工具；`assemble_tool_pool` 组装内置 + MCP 工具池；`mcp__{server}__{tool}` 命名防冲突 | `SkillTool.ts`、工具池动态组装 |

### 阶段八：总装与编排

| # | 主题 | 核心内容 | 对应 Claude Code 概念 |
|---|------|---------|----------------------|
| 21 | **Integrated Harness 总装** | 全组件集成：输入 → hooks → 通知注入 → 压缩 → prompt 组装 → LLM → 权限 → 工具分发（内置/MCP/后台）→ PostToolUse → 回填循环；端到端集成测试 | 系统架构全链路 |
| 22 | **Workflow Runtime** | 可恢复脚本运行时：agent()/parallel()/pipeline()/phase() 编排原语；Journal 磁盘持久化（每步输入/输出/状态）；中间结果存变量不进对话历史；`resume_from_run_id` 幂等续跑 | Journal 缓存与幂等 |
| 23 | **Goal Loop（收官，可选）** | 返回前独立判断：真正返回之前再加一次目标达成校验，未达成则自动续跑 | Goal Loop 独立校验 |

## 每次迭代的固定产出

1. `mini_agent/` 下的增量代码（不推翻上一轮，向前兼容）
2. `tests/` 新增单测（假客户端，不烧 API key）
3. `docs/iterations/NN-xxx.md` 拆解：动机 → 设计 → 代码讲解 → 环境与运行指令 → 与 Claude Code 概念对应关系
4. git commit（Conventional Commits）

## 当前状态

- 迭代 1 ✅ Agent Loop：10 单测 + DeepSeek 真实运行验证。详见 [iterations/01-agent-loop.md](iterations/01-agent-loop.md)（含 response/state 字段解剖）
- 迭代 2 ✅ 工具编排：7 内置工具 + 参数校验 + 并发分派，24 新单测（全套 34 个通过）。详见 [iterations/02-tool-use.md](iterations/02-tool-use.md)
- 迭代 3 ✅ 权限管线：三道闸门 + 四态裁决 + 四模式 + Denial Tracking + settings 分层配置，29 新单测（全套 63 个通过）。详见 [iterations/03-permissions.md](iterations/03-permissions.md)
- 下一步：迭代 4 Hooks 扩展点（四事件注册表，权限检查移入 PreToolUse）
