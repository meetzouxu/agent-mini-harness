# 迭代 3：权限管线 Permission —— 三道闸门、四态裁决与死循环防护

> 代码：`mini_agent/permissions.py`（权限核心）｜`mini_agent/tools.py`（闸门 0 钩子）｜`mini_agent/agent.py`（裁决集成）｜`mini_agent/__main__.py`（CLI 模式与交互）
> 测试：`tests/test_permissions.py`（29 个新单测）
> 参考资料：[claude-code-best/claude-code](https://github.com/claude-code-best/claude-code) `docs/safety/permission-model.mdx`（Allow/Ask/Deny 体系 + 8 层规则来源 + Denial Tracking）

## 0. 这一迭代要解决什么问题

迭代 2 的 agent 是个「愣头青」：`bash("rm -rf build")` 和 `bash("git status")`
被同等对待——黑名单只有一条子串匹配，文件写入从不询问，`--quiet` 跑批时
更是无差别放行。真实世界的编码 Agent 必须回答一个问题：

> **模型想执行一个有副作用的操作，凭什么放行？**

Claude Code 的答案是三层裁决体系（Allow / Ask / Deny）+ 多层规则来源 +
死循环防护。本轮把这套管线完整搬进 mini-agent。

## 1. 环境（无新增依赖）

零新增依赖（fnmatch/json 均为标准库）。

```bash
cd /Users/nian/CatPaw/agent-mini-harness
.venv/bin/pip install -r requirements.txt   # 无变化，可跳过
```

## 2. 代码结构拆解

```
mini_agent/
├── permissions.py   # ★ 本轮核心：Behavior 枚举 + PermissionManager + 规则匹配 + settings 加载
├── tools.py         # Tool.check_permissions 钩子（闸门 0：黑名单/路径围栏前移）
├── agent.py         # _execute_tool_call 里插入 decide()；并发批次 ask 预检串行化
└── __main__.py      # --permission-mode / --yes / 终端 y/a/n 交互 / settings 自动加载
tests/
└── test_permissions.py   # 29 个新单测
```

### 2.1 四态裁决（Behavior）

| 行为 | 含义 | 触发后 |
|------|------|--------|
| `ALLOW` | 放行 | 执行工具 |
| `ASK` | 需确认 | 调 on_ask 回调（y/a/n）→ yes 放行 / always 写入会话规则 / no 拒绝 |
| `DENY` | 拒绝 | `[权限拒绝] 原因` 作为观察回填，模型读后改变策略 |

第四态是 `check_permissions` 钩子返回 `None`——「本层不裁决，交给通用闸门」，
对应 CC PermissionResult 的 `passthrough` 语义。

### 2.2 三道闸门（顺序就是语义）

```python
def check(self, tool, arguments):      # 纯函数，可安全并发调用
    # 闸门 0：工具自带硬边界（黑名单/路径围栏）→ deny 或 None
    # 闸门 1：deny 规则（最高优先级——deny 永远压过 allow）
    # 闸门 2：allow 规则 → ask 规则
    # 闸门 3：默认行为（按 permission_mode）
```

三个关键设计：

- **硬边界优先于一切**：bash 黑名单和路径围栏从 handler **前移**到闸门 0
  （`_bash_permission_check` / `_path_fence_check`）。连 `bypass` 模式都拦不住黑名单
  ——系统边界不随用户信任级别浮动。handler 里的 `safe_path` 保留为纵深防御。
- **deny > allow**：规则匹配顺序先 deny 再 allow，一条 deny 能压过一百条 allow
  （安全默认：宁可多问，不可错放）。
- **check() 与 decide() 分离**：`check()` 纯函数无副作用（并发批次预检用 `peek()`）；
  `decide()` 才处理交互与拒绝记录——交互回调只能串行调用。

### 2.3 规则匹配三维度（对应 CC toolMatchesRule）

```python
PermissionRule(tool="read_file", behavior=ALLOW)                  # 整工具
PermissionRule(tool="bash", behavior=ALLOW, content="git *")      # 命令前缀
PermissionRule(tool="write_file", behavior=ALLOW, content="src/**")  # 路径 glob
```

一个开发中实测踩到的坑：`git *` 的前缀匹配若把尾随空格剥掉（`rstrip()`），
`github-cli` 也会误命中——**前缀必须保留 `git ` 带空格**。单测
`test_bash_command_prefix_match` 专门锁住这个行为。

### 2.4 权限模式表（对应 CC PermissionMode）

| 模式 | 只读 | 写文件 | bash |
|------|------|--------|------|
| `default` | allow | ask | ask |
| `plan` | allow | **deny** | **deny**（探索阶段只读） |
| `acceptEdits` | allow | allow（工作区内编辑放行） | ask |
| `bypass` | allow | allow | allow（黑名单仍拦截！） |

### 2.5 Denial Tracking：死循环防护

对应 CC `denialTracking.ts`（maxConsecutive=3 / maxTotal=20）：

- 同一工具**连续 3 次拒绝** → 拒绝理由追加「请改变策略，不要重复请求同一操作」
- 总拒绝 **20 次** → 追加「建议终止任务」
- 工具成功执行一次 → 连续计数**清零**

没有这个机制，模型会无限重试被拒命令烧 token——「错误回填」给了模型改错的
机会，但也给了它撞死墙的机会。

### 2.6 分层配置（CC 8 层的教学精简）

CC 有 8 层规则来源（userSettings → … → session，后者覆盖前者）。教学版 3 层：

```
.mini-agent/settings.json   → 项目级规则（团队共享，git 提交）
--permission-mode / --yes   → CLI 参数
「总是允许」交互            → 会话级规则（内存，进程结束即失效）
```

settings 格式（对应 CC permissions 配置段）：

```json
{
  "permissions": {
    "allow": ["read_file", "bash:git *"],
    "ask":   ["bash:rm *"],
    "deny":  ["bash:sudo *"]
  }
}
```

文件损坏 → 警告并返回空规则（权限系统必须永不崩循环）。

### 2.7 Agent 集成：权限遇到并发

两个集成点：

1. **裁决插入点**：`_execute_tool_call` 里，参数校验之后、执行之前。deny →
   `[权限拒绝]` 回填（与工具错误同一通道，模型统一「读错误改策略」）。
2. **ask 串行化**：并发批次预检（`peek()`）发现任何一个工具可能 ask → 整批
   串行。理由：交互回调（终端 input）不能并发调用，多个确认弹窗交错是灾难。
   全 allow 的只读批次照旧并发——权限不拖慢只读路径（有计时单测锁定）。

### 2.8 CLI 交互

```
🔒 需要确认 —— write_file
   path = files.txt
   content = README.md docs/ROADMAP.md ...
   原因: default 模式：写操作需确认
   允许吗？ [y]允许 / [a]总是允许 / [n]拒绝 >
```

`--quiet`（无交互通道）时 ask 一律**拒绝**——安全默认。`--yes` 等价 bypass。

## 3. 运行指令

```bash
cd /Users/nian/CatPaw/agent-mini-harness

# 单测（63 个，无需 key）
.venv/bin/python -m unittest discover -s tests -v

# 真实体感：默认模式跑一个会写文件的任务，终端会弹 y/a/n 确认
.venv/bin/python -m mini_agent "把 ROADMAP.md 里「当前状态」一节复制到 status.txt"
# 选 a → 后续 write_file 全自动放行；选 n → 模型收到拒绝理由改变策略

# plan 模式：只读探索（写操作直接 deny）
.venv/bin/python -m mini_agent "分析 mini_agent/agent.py 的循环结构" --permission-mode plan

# 项目级规则：允许 git 只读命令
mkdir -p .mini-agent && cat > .mini-agent/settings.json <<'EOF'
{"permissions": {"allow": ["bash:git status", "bash:git diff *", "bash:git log *"]}}
EOF
.venv/bin/python -m mini_agent "看看最近的 git 提交历史并总结"
```

## 4. 测试如何验证（29 个新单测）

| 组 | 验证点 |
|----|--------|
| GateOrderTests ×5 | 黑名单/围栏优先于 bypass、deny 压过 allow、allow/ask 规则短路 |
| RuleMatchTests ×5 | 工具名精确/通配、bash 前缀（含空格陷阱）与精确、路径 glob |
| ModeTableTests ×4 | 四种模式 × 三类工具的完整行为矩阵 |
| DecideTests ×6 | yes/always/no、always 持久化为会话规则、无回调安全拒绝、Denial Tracking 注入与重置 |
| SettingsLoadTests ×3 | 缺失/完整/损坏 settings 的加载行为 |
| AgentIntegrationTests ×6 | deny 回填不崩、无权限管理器向后兼容、plan 拒写、ask 批次串行化、allow 批次仍并发（计时） |

## 5. 本迭代刻意不做的事

- PreToolUse hook 覆盖权限决策（迭代 4：hooks 注册表）
- `updatedInput`（权限层改写工具参数，CC PermissionResult 字段）
- 企业策略层/用户级 settings（教学版 3 层够讲清优先级语义）
- bash 命令 AST 解析（CC 用 tree-sitter 解析管道与子命令，教学版用前缀匹配）

## 6. 已知局限

- bash 黑名单仍是子串匹配，`"";sudo` 拼接可绕过（已知；AST 解析在路线图外，迭代 4 的 PreToolUse hook 可部分缓解）
- 会话级规则无 TTL，长会话里「总是允许」累积（CC 会写入 settings 持久层，教学版进程结束即失效，反而更安全）
- plan 模式对 bash 一刀切 deny，不区分只读命令（CC 用 readOnlyValidation 判定）
