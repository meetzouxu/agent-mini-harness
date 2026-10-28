# 迭代 1：Agent Loop —— 从零跑通「思考 → 行动 → 观察」循环

> 代码：`mini_agent/agent.py`（核心 200 行）｜测试：`tests/test_agent_loop.py`
> 参考资料：[claude-code-best/claude-code](https://github.com/claude-code-best/claude-code)（Claude Code CLI 逆向开源仓库）`docs/conversation/the-loop.mdx`（源码级解析 queryLoop 状态机）

## 0. 这一迭代要解决什么问题

聊天机器人是「一问一答」；Agent 是「你说一个目标，它连续执行多步才给你结果」。
让模型能多步执行的那个机制，就是 **Agent Loop**：

```
while True:
    思考：把对话（含历史工具结果）发给模型
    行动：模型返回 tool_calls（要调什么工具、什么参数）
    观察：执行工具，把结果作为 tool 消息追加进对话
    终止：没有 tool_calls → 完成；轮次超限 → 熔断
```

Claude Code 的实现在 `src/query.ts` 的 `queryLoop()` —— 一个 `while(true)` 异步生成器。
我们用 200 行 Python 复刻它的**最小可用版本**。

## 1. 环境搭建（一次性）

```bash
cd /Users/nian/CatPaw/agent-mini-harness

# 1. 创建虚拟环境（Python 3.12）
python3 -m venv .venv

# 2. 安装依赖（仅 2 个：openai SDK + dotenv）
.venv/bin/pip install -r requirements.txt

# 3. 配置密钥（.env 已被 .gitignore 排除，不会进 git）
cp .env.example .env
# 编辑 .env，填入你的 DeepSeek API Key（platform.deepseek.com 创建）
```

`.env` 内容（4 项）：

```ini
DEEPSEEK_API_KEY=sk-你的真实密钥
DEEPSEEK_BASE_URL=https://api.deepseek.com
MINI_AGENT_MODEL=deepseek-chat
MINI_AGENT_MAX_TURNS=15
```

**为什么 DeepSeek 可以用 openai SDK？** DeepSeek 提供 OpenAI 兼容协议，
`OpenAI(base_url="https://api.deepseek.com", api_key=...)` 即可直连。
后续所有迭代不锁定厂商 —— 只要兼容 OpenAI Chat Completions 协议都能跑。

## 2. 代码结构拆解

```
mini_agent/
├── __init__.py     # 包声明与版本
├── __main__.py     # CLI 入口：python -m mini_agent "任务"  （对应 claude -p）
├── config.py       # 从 .env 加载配置；密钥缺失给出可操作报错
├── llm.py          # 客户端工厂 build_client()（对应 claude-code 的 deps.callModel 注入点）
├── tools.py        # Tool 抽象 + 4 个内置工具 + 工作目录围栏 + 结果截断
└── agent.py        # ★ Agent 类：Agent Loop 状态机（本轮核心）
tests/
└── test_agent_loop.py  # 10 个单测：FakeClient 脚本化模型响应，不烧 key
```

### 2.1 Tool 抽象（tools.py）

一个工具 = 元信息 + JSON Schema + 执行函数：

```python
@dataclass
class Tool:
    name: str
    description: str
    parameters: dict          # JSON Schema
    handler: Callable[[dict], str]

    def to_api_schema(self):  # 转成 DeepSeek tools 参数格式
        ...
```

内置 4 个工具（刻意最小）：`read_file`、`write_file`、`list_dir`、`glob_files`。

两个安全/预算机制从第一轮就埋下伏笔：

- **工作目录围栏** `safe_path()`：所有路径 resolve 后必须落在 `cwd` 内，
  `../../etc/passwd`、绝对路径 `/etc/passwd` 直接抛 `ToolError`。
  （迭代 3 的完整权限管线会替换这道粗围栏）
- **工具结果预算** `MAX_RESULT_CHARS = 8000`：超长结果截断。
  对应 claude-code 的 `applyToolResultBudget()`（迭代 6 压缩细化的基础）。

### 2.2 Agent Loop 状态机（agent.py）

`Agent.run(prompt)` 的主循环，与 claude-code 的对应关系：

| claude-code（query.ts）      | mini_agent（agent.py）            |
|------------------------------|-----------------------------------|
| `queryLoop()` 的 `while(true)` | `Agent.run()` 的 `while True`     |
| `needsFollowUp` 标志          | `tool_calls` 是否非空             |
| `State.turnCount`             | `LoopState.turn_count`            |
| `State.transition`            | `LoopState.transition`（'next_turn'/None） |
| max_turns 终止                | `max_turns` 参数（默认 15）       |
| `runTools()` 执行工具         | `_execute_tool_call()`            |
| 工具错误即时恢复              | 异常→错误文本回填（见 2.3）       |

关键片段（`run()` 精简）：

```python
while True:
    response = self._call_model(state.messages)          # 思考
    message = response.choices[0].message
    state.messages.append(message.model_dump(exclude_none=True))

    tool_calls = list(message.tool_calls or [])
    if not tool_calls:                                    # 终止：completed
        state.stop_reason = "completed"
        final_text = message.content or ""
        break

    for tc in tool_calls:                                 # 行动+观察
        event = self._execute_tool_call(turn, tc)
        state.messages.append({
            "role": "tool",
            "tool_call_id": tc.id,                        # 必须配对 tool_call
            "name": tc.function.name,
            "content": event.result,                      # 错误也是观察结果
        })

    state.turn_count += 1
    if state.turn_count >= self.max_turns:                # 熔断：max_turns
        state.stop_reason = "max_turns"
        break
    state.transition = "next_turn"                        # 继续原因
```

**API 协议要点（最容易踩的坑）**：每条 `tool` 消息必须带 `tool_call_id`
与 assistant 消息里的 `tool_calls[i].id` 配对，漏一个 API 直接 400。
「tool_use 与 tool_result 配对完整性」是 claude-code 压缩模块里
`adjustIndexToPreserveAPIInvariants()` 专门保护的不变量，迭代 6 会再遇到。

### 2.3 解剖 response 与 state：用一次真实运行讲透两个字段

以 2026-10-27 的真实运行为例（任务：统计 .md 文件写入 files.txt，
3 轮、5 次工具调用、completed 终止）：

> turn 1：`list_dir {}` + `glob_files {"pattern": "**/*.md"}`
> turn 2：`list_dir {"path": "docs"}` + `list_dir {"path": "docs/references"}`
> turn 3：`write_file {"path": "files.txt", ...}` → 最终轮纯文本总结

#### 示例 A：response —— 每轮的快照，用完即弃

`_call_model()` 返回的 DeepSeek ChatCompletion（OpenAI 兼容结构），
turn 1 时模型**同时下发两个工具**，响应形如：

```json
{
  "id": "chatcmpl-xxxx",
  "model": "deepseek-chat",
  "choices": [{
    "index": 0,
    "finish_reason": "tool_calls",
    "message": {
      "role": "assistant",
      "content": null,
      "tool_calls": [
        { "id": "call_0_a1", "type": "function",
          "function": { "name": "list_dir", "arguments": "{}" } },
        { "id": "call_0_b2", "type": "function",
          "function": { "name": "glob_files",
                        "arguments": "{\"pattern\": \"**/*.md\"}" } }
      ]
    }
  }],
  "usage": { "prompt_tokens": 812, "completion_tokens": 47,
             "total_tokens": 859 }
}
```

逐字段设计意图：

| 字段 | 含义与设计意图 |
|------|----------------|
| `choices[0].message` | 本轮的「思考」。`content: null` 表示模型**没说话直接动手** —— 这次运行的 turn 1/2/3 全是这种；只有最终轮 content 才有总结文本 |
| `message.tool_calls[]` | 行动指令列表。一轮可下发多个（turn 1 就是双工具并行侦察）。注意 `arguments` 是 **JSON 字符串**而非对象，所以代码里必须 `json.loads` —— 也就是它可能解析失败，错误路径必须有 |
| `finish_reason` | 终止判断的第一信号：`tool_calls` = 还要继续；`stop` = 说完了。非流式下可靠，**流式下不可靠**（Claude Code 为此加了 `needsFollowUp` 保护；我们直接看 tool_calls 是否非空，两者等价） |
| `usage` | 本轮消耗。`input_tokens` 含**把全部历史重发**的大小，是上下文水位监控的原始数据 |

代码对它的消费只有两行 —— 有用部分被吸收进 state，response 本身用完即弃：

```python
message = response.choices[0].message
state.messages.append(message.model_dump(exclude_none=True))  # 思考 → 账本
state.input_tokens += response.usage.prompt_tokens            # 计数 → 账本
```

#### 示例 B：state —— 贯穿全程的账本

`LoopState` 在这次运行中的完整演变（messages 条数含 system+user 起点 2 条）：

| 时刻 | turn_count | api_calls | input_tokens | messages | transition / stop_reason |
|------|-----------|-----------|--------------|----------|--------------------------|
| `run()` 开始 | 0 | 0 | 0 | 2 | — |
| turn 1 吸收完 | 1 | 1 | +~800 | 5（+assistant +2 tool） | `next_turn` |
| turn 2 吸收完 | 2 | 2 | +~1300 | 8（+assistant +2 tool） | `next_turn` |
| turn 3 吸收完 | 3 | 3 | +~1600 | 10（+assistant +1 tool） | — |
| 最终轮（纯文本） | 3 | 4 | +~1900 | 11（+final assistant） | `completed` |

字段设计意图：

- **`messages` 是唯一滚动的资产**：「上下文」就是它，其余字段都是为观测它而存在的派生量。看第 2 轮 input_tokens 比第 1 轮大 —— 历史含工具结果整包重发，这是 Agent 的固有成本（迭代 6 压缩要解决的对象）
- **`turn_count` 与 `api_calls` 分开数**：本迭代恒等（每轮恰好 1 次调用），迭代 11 重试上线后 `api_calls > turn_count`，两数相减即重试开销 —— 现在分开数是为了那时不用改结构
- **`transition`（为什么继续）/ `stop_reason`（为什么停）成对**：对应 Claude Code queryLoop State 的同名字段。调试死循环时，「继续原因」比「停止原因」更有价值
- 对照 claude-code 的 State 有 10 个字段（多出 `autoCompactTracking`、`maxOutputTokensOverride`、`stopHookActive`…），分别属于迭代 6/11/4 的主题 —— **字段是功能落地的痕迹**，我们的 8 字段就是当前功能边界的精确刻画

一句话总结：**response 是一轮的快照（用完即弃，有用部分被吸收进 state）；
state 是全程的账本（messages 是资产，计数器是仪表盘）**。

顺带一个这次运行里的真实细节：`glob_files("**/*.md")` 扫进了 `.venv/`
site-packages 里的几百个 LICENSE.md，8000 字符结果预算立刻截断生效 ——
这就是 `applyToolResultBudget()` 最小版在真实场景的价值（否则第 2 轮的
input_tokens 会暴涨）。

### 2.4 错误恢复设计：错误也是观察

`_execute_tool_call()` 捕获**所有**异常（JSON 解析失败、未知工具、参数错、
工具内部异常），统一转成 `[工具错误] ...` 文本回填。模型读到错误后
自行修正参数重试 —— 循环永不因工具问题崩溃。

这对应 claude-code 的「错误即时恢复」：工具失败不需要推倒重来。

### 2.5 终止条件（本迭代实现 2 个，claude-code 有 11 个）

| 终止原因 | 触发 | claude-code 行号 |
|----------|------|------------------|
| `completed` | 模型未发出 tool_use → `needsFollowUp=False` | query.ts:1401 |
| `max_turns` | 轮次计数超限 | query.ts:1755 |

其余 9 个（prompt_too_long、aborted、model_error、stop_hook...）在
迭代 12（异步+中断）、迭代 11（重试+降级）中陆续引入。

## 3. 运行指令

```bash
cd /Users/nian/CatPaw/agent-mini-harness

# 单元测试（不需要 API key，验证循环机制本身）
.venv/bin/python -m unittest discover -s tests -v

# 真实运行（需要 .env 里填好 DEEPSEEK_API_KEY）
.venv/bin/python -m mini_agent "统计当前目录下有几个 .md 文件，把文件名列表写入 files.txt"
.venv/bin/python -m mini_agent "读取 docs/ROADMAP.md，告诉我一共规划了几次迭代" --quiet
.venv/bin/python -m mini_agent "列出所有 Python 文件并统计总行数" --max-turns 8
```

真实运行时终端输出形如（2026-10-27 实录）：

```
── turn 1 ──────────────────────
⟳ list_dir {}
  ← [dir] .git\n[dir] .venv\n[dir] docs\n[dir] mini_agent\n... (1 ms)
⟳ glob_files {"pattern": "**/*.md"}
  ← .venv/lib/python3.12/site-packages/... (113 ms)
── turn 2 ──────────────────────
⟳ list_dir {"path": "docs"}
  ← [dir] iterations\n[dir] references\n[file]ROADMAP.md\n共 3 项 (0 ms)
⟳ list_dir {"path": "docs/references"}
  ← [file]agent-capabilities-index.md\n... (0 ms)
── turn 3 ──────────────────────
⟳ write_file {"path": "files.txt", ...}
  ← 已写入 files.txt（155 字符，6 行） (2 ms)
✓ 完成
```

3 轮 5 次工具调用，字段解剖详见 2.3 节。

## 4. 测试如何验证循环（不烧 key）

`FakeClient` 实现 `chat.completions.create()` 接口，按脚本依次吐出
预设响应。断言的不是「模型说了什么」，而是**循环机制本身**：

| 测试 | 验证点 |
|------|--------|
| test_loop_completes_after_tool_use | tool 消息回填、配对 id、usage 累计、completed 终止 |
| test_max_turns_termination | 死循环熔断 |
| test_tool_error_is_fed_back_not_crash | 错误文本回填、循环继续 |
| test_unknown_tool_and_bad_json | 未知工具/非法 JSON 参数的降级路径 |
| test_multi_tool_calls_in_one_turn | 一轮多个 tool_calls 逐个回填、顺序保持 |
| test_path_traversal_blocked 等 4 个 | 围栏、读写流、截断 |

这套「假客户端」模式会贯穿全部 23 个迭代 —— 它就是 claude-code
`deps.callModel` 依赖注入设计在测试侧的翻版。

## 5. 本迭代刻意不做的事

- 不做多轮对话/REPL（迭代 5：多轮会话 + System Prompt 组装）
- 不做流式输出（迭代 12：AsyncGenerator + 后台任务）
- 不做 API 重试（迭代 11：指数退避 + fallback）
- 不做真正的权限系统（迭代 3：三道闸门权限管线）
- 不做 hooks 扩展点（迭代 4：四事件注册表）
- 工具串行执行（迭代 2 引入 isConcurrencySafe 并发分派）

**每次迭代只加一个主题**，这是本项目的方法论。

## 6. 已知局限

- ~~真实 key 的联网验证尚未跑过~~ **已完成**：2026-10-27 真实运行 3 轮 5 工具调用 completed（见 2.3 节）
- 工具围栏以 `cwd` 为根，进程内 `os.chdir` 会影响它（测试用例已按此约定隔离）
- ~~assistant 消息直接 `model_dump()` 回填，DeepSeek 空参数工具调用的兼容性以实测为准~~ **已实测通过**（2026-10-27 真实运行，含空参数 `list_dir {}`）
