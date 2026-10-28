# Agent 能力地图：从 Agent Loop 到完备 Harness

> 本项目的能力参考地图：把「用 Python 复刻 Claude Code」这个目标拆成
> 可独立迭代的主题，与 [../ROADMAP.md](../ROADMAP.md) 的 8 阶段 23 迭代一一对应。
>
> 核心原则：**Harness = Tools + Knowledge + Observation + Action Interfaces + Permissions；
> 模型做决策，Harness 执行。**

## 总纲：Agent = Model + Harness

一个完备的编码 Agent（以 Claude Code 为原型）＝ agent loop + 工具系统 + 按需 skill
加载 + 上下文压缩 + 子 agent 派生 + 带依赖图的任务系统 + 异步邮箱的团队协调 +
worktree 隔离的并行执行 + 权限治理。

## 阶段一：Agent 基础能力（迭代 1–4）

### Agent Loop（→ 迭代 1 ✅）
- 最简 loop：messages → LLM → stop_reason != tool_use 则停 → 执行工具 → tool_result 回填
- Claude Code 保护机制：`needsFollowUp`（流式中 stop_reason 不可靠，`query.ts:830-834`）
- State 对象 10 字段：messages / toolUseContext / autoCompactTracking /
  maxOutputTokensRecoveryCount / hasAttemptedReactiveCompact / maxOutputTokensOverride /
  pendingToolUseSummary / stopHookActive / turnCount / transition
- `StreamingToolExecutor`（`query.ts:561`）：模型还在生成时工具即可并行执行

### 工具编排 Tool Use（→ 迭代 2）
- TOOLS 数组 + TOOL_HANDLERS 字典查表分发；Claude Code 用 buildTool() 独立对象
- `isConcurrencySafe` 判定：FileRead/Glob/`ls` 可并发；`rm` 独占；TaskCreate 可并发

### 权限管线 Permission（→ 迭代 3）
- 三道闸门：① 拒绝列表（硬拒）② 规则匹配 ③ 用户审批
- PermissionResult 四 behavior：allow / deny / ask / passthrough（`types/permissions.ts:241-266`）
- 多阶段管线：Zod schema → validateInput → backfill → PreToolUse hooks →
  resolveHookDecision → hasPermissionsToUseToolInner（7 层规则）→ 执行
- 分层配置：用户级 ~/.claude/settings.json → 项目级 → 本地级 → 功能标志 →
  企业策略 → CLI 参数 → 内联命令 → 会话级（优先级从低到高）
- YoloClassifier：auto 模式下 LLM 分类器判断安全性，连续拒绝多次回退人工

### Hooks 扩展点（→ 迭代 4）
- 四事件：UserPromptSubmit（输入后 LLM 前）/ PreToolUse / PostToolUse / Stop（退出前）
- hook 注册表 + trigger_hooks()；循环不直接调用检查函数
- Stop hook 返回字符串 = 注入该消息强制续跑；`stopHookActive` 防无限循环（`query.ts:212,1300`）
- Claude Code 实际有 27 个 hook 事件；HookResult 14 字段（blockingError → 注入对话自纠；
  permissionBehavior；updatedInput；preventContinuation 等）
- hook allow 不能绕过 settings.json 的 deny/ask（业务语义 vs 系统边界）

## 阶段二：上下文工程（迭代 5–7）

### System Prompt 组装（→ 迭代 5）
- PROMPT_SECTIONS 分段：identity/tools/workspace 始终 + memory 按需（基于真实状态非关键词）
- get_system_prompt 确定性序列化（json.dumps sort_keys）缓存
- Claude Code：getSystemContext（gitStatus/cacheBreaker → system 数组）vs
  getUserContext（CLAUDE.md/currentDate → `<system-reminder>` 用户消息）

### 上下文压缩 Compact（→ 迭代 6）
- 四层：L3 applyToolResultBudget（大结果落盘，必须在 micro 前）→ L1 snip_compact
  （>50 条保留头 3 尾 47，不拆 tool_use/tool_result 对）→ L2 micro_compact
  （旧 tool_result 占位）→ L4 auto_compact（LLM 全量摘要，熔断 3 次）
- reactive compact 应急：API 报 prompt_too_long 后从尾部回退，仅重试 1 次
- Claude Code 真实顺序 `query.ts:379-468`：budget → snip → micro → collapse → auto
- budget 阈值 200,000 字符（`toolLimits.ts:49`）；auto 阈值 contextWindow - maxOutputTokens - 13000

### 记忆系统 Memory（→ 迭代 7）
- User Memory（跨会话，`.memory/` 多 .md + MEMORY.md 索引常驻 system prompt）/
  Session Memory（单会话，compact 摘要）
- 四类记忆：user（你是谁）/ feedback（怎么做事）/ project（正在发生什么）/ reference（东西在哪）
- side-query 选相关记忆（≤5 条）注入当前 user turn，不破坏 cache；失败降级关键词匹配
- 每轮结束 extract_memories() 提取（用户显式"记住"或稳定偏好）；
  文件 ≥10 触发 consolidate 去重合并

## 阶段三：状态与规划（迭代 8–10）

### 会话持久化（→ 迭代 8）
- JSONL transcript：一行一条消息落盘；`--resume` 恢复；usage/费用累计（`sessionStorage.ts`）

### TodoWrite 任务规划（→ 迭代 9）
- 规划能力而非执行能力；pending/in_progress/completed；连续 3 轮未调注入 reminder
- V1（内存 AppState）与 V2 Task System 并存，`isTodoV2Enabled()` 切换

### Task System 任务图（→ 迭代 10）
- Task{id, subject, description, status, owner, blockedBy} 存 `.tasks/{id}.json`
- can_start：blockedBy 全部 completed 才可 claim；不存在的依赖视为 blocked
- claim（pending→in_progress 设 owner）/ complete（→completed 并解锁下游）
- 依赖判断：产出/时序/决策/环境四类；DAG 无环检测
- Claude Code：proper-lockfile 锁（重试 30 次退避 5-100ms）防 TOCTOU；
  blocks（下游）+ blockedBy（上游）双向；fs.watch 响应式；TaskCreated/TaskCompleted hooks

## 阶段四：执行模型（迭代 11–14）

### 失败重试 Error Recovery（→ 迭代 11）
- 路径 1 输出截断：8K→64K 升级（不追加截断输出重试原请求）→ 续写提示 ≤3 次
- 路径 2 prompt_too_long：reactive compact → 重试 1 次 → 仍超则退出
- 路径 3 临时故障：429/529 指数退避（500ms×2^n 上限 32s）+ 抖动 ≤10 次；
  连续 3 次 529 → fallback 模型
- Claude Code 十几种 reason/transition；流式中可恢复错误暂扣不展示（`query.ts:788-822`）
- token budget continuation：连续 3 次增量 < 500 停止（`tokenBudget.ts:60-62`）

### 异步执行与后台任务（→ 迭代 12）
- 慢操作 daemon 线程；占位 tool_result 带 bg_id；`<task_notification>` 与 tool_result
  合入同一条 user 消息
- Claude Code：后台 = 子进程 + 输出写文件（管道缓冲区满会阻塞子进程）+ 不 await

### Subagent 子代理（→ 迭代 13）
- spawn_subagent：全新 messages[]、只回传结论、禁止递归、权限不跳过
- Claude Code 三模式：Normal / Fork（buildForkedMessages 共享 prompt cache，五组件字节级一致）/
  General-Purpose；`permissionMode: 'bubble'` 权限冒泡；`run_in_background` 异步子 agent
- createSubagentContext：readFileState 从父克隆（避免重复读文件）

### Cron 调度（→ 迭代 14）
- 四层：Scheduler（每秒轮询）→ Queue → Queue Processor（空闲时拉起 agent_loop）→ Consumer
- CronJob{id, cron, prompt, recurring, durable}；五段式 cron；DOM/DOW 是 OR 语义
- minute_marker "YYYY-MM-DD HH:MM" 防同分钟重复；单 job try/except 不拖垮线程；
  durable 任务 `.scheduled_tasks.json` 重启恢复

## 阶段五：可观测性（迭代 15）

### 日志监控（→ 迭代 15）
- 结构化 JSON 日志落盘（logs/）；run 级 trace（turn/tool_event/耗时）；token 与费用统计
- Claude Code：`cost-tracker.ts`、telemetry；PostToolUse hook 可挂大输出告警

## 阶段六：多 Agent 协作（迭代 16–18）

### Agent Teams（→ 迭代 16）
- MessageBus：每 agent 一个 `.jsonl` 收件箱，append 写 + 消费式读（读完删除）
- 用文件而非内存队列：teammate 是独立 OS 进程，不共享堆内存；
  写入加 .lock + atomicWrite；InboxPoller 每 1s 轮询，会话忙碌时排队
- turn vs conversation：turn = 一次完整迭代（输入→推理→工具→停止交还控制权）
- 权限冒泡：队友 permission_request → Lead 审批 → permission_response（500ms 轮询）

### Team Protocols（→ 迭代 17）
- ProtocolState{request_id, type, sender, target, status, payload}；pending → approved/rejected
- match_response：request_id 关联 + 类型校验（shutdown_response 不会 approve plan_approval）+
  防重复 resolve
- 双入口读 inbox：check_inbox 工具（LLM 主动）+ 主循环末尾（兜底）

### Autonomous Agents（→ 迭代 18）
- 三阶段：WORK（inbox→LLM→工具循环）/ IDLE（每 5s 轮询 inbox+看板，60s 超时）/ SHUTDOWN
- scan_unclaimed_tasks：pending + 无 owner + can_start → 自动 claim
- 文件锁防多队友同时认领同一任务

## 阶段七：外部能力（迭代 19–20）

### Skill Loading（→ 迭代 19）
- 两级加载：目录（system prompt，~100 token/技能，每轮都带）+
  内容（load_skill tool_result，~2000 token，按需）
- SKILL.md frontmatter：name/description/when_to_use/allowed-tools/context(inline|fork)/
  model/hooks/paths/user-invocable
- 注册表查找，不走文件路径，无遍历风险；catalog 预算 = 上下文窗口 ~1%（上限 8000 字符）

### MCP 工具接入（→ 迭代 20）
- MCPClient{register, call_tool}；connect_mcp 动态发现；
  assemble_tool_pool 每轮组装内置 + MCP 工具；`mcp__{server}__{tool}` 前缀防冲突

## 阶段八：总装与编排（迭代 21–23）

### Integrated Harness 总装（→ 迭代 21）
- 全链路：用户输入 → UserPromptSubmit hooks → cron/background 通知注入 →
  context compact → memory+skills+MCP 组装 system prompt → LLM →
  tool_use? 否→Stop hooks 返回 / 是→PreToolUse+permission →
  TOOL_HANDLERS/MCP/后台分派 → PostToolUse → 回填 → 下一轮

### Workflow Runtime（→ 迭代 22）
- 编排原语：agent() / parallel() / pipeline() / phase()
- Journal 磁盘持久化（每步输入/输出/耗时/状态）→ 续跑核心；
  中间结果存 runtime 变量不进对话历史；`resume_from_run_id` 幂等
  （没改过的 agent() 直接用 journal 结果）
- run_id 由 runtime 创建执行时生成，返回给模型供续跑传回

### Goal Loop（→ 迭代 23，可选）
- 真正返回前的独立判断：目标达成校验，未达成自动续跑
