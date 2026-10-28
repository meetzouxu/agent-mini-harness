# 迭代 2：工具编排 Tool Use —— 查表分发、参数校验与并发执行

> 代码：`mini_agent/tools.py`（Tool 2.0 + 7 个内置工具）｜`mini_agent/agent.py`（并发分派）
> 测试：`tests/test_tool_use.py`（24 个新单测）
> 参考资料：[claude-code-best/claude-code](https://github.com/claude-code-best/claude-code) `docs/tools/what-are-tools.mdx`（Tool 类型 35 字段 + 调用链路 10 步）

## 0. 这一迭代要解决什么问题

迭代 1 的 agent 只有一双「慢手」：4 个工具串行执行，一轮调两个只读工具也要排队；
参数没有任何校验，模型传错类型会直接让工具内部崩出 Python 栈；
bash、编辑、搜索这些编码 Agent 的看家本领全部缺席。

这一轮把工具系统升级为 Claude Code 的形态：

1. **Tool 2.0 抽象**：每个工具携带并发安全/只读标记 + 工具级结果预算（对应 CC 的 35 字段类型）
2. **7 个内置工具**：read / write / edit / list / glob / **grep** / **bash**
3. **参数校验**：required 存在性 + 类型检查，失败回填让模型自纠（对应 CC validateInput）
4. **并发分派**：全只读批次走线程池并发，混写批次全串行保序（对应 CC isConcurrencySafe）

## 1. 环境（无新增依赖）

沿用迭代 1 的环境：Python 3.12 + `.venv`，依赖仍只有 openai 与 python-dotenv。
并发用的是标准库 `concurrent.futures`，bash 用 `subprocess`，零新增依赖。

```bash
cd /Users/nian/CatPaw/agent-mini-harness
.venv/bin/pip install -r requirements.txt   # 无变化，可跳过
```

## 2. 代码结构拆解

```
mini_agent/
├── tools.py    # Tool 2.0：buildTool 式抽象 + 参数校验 + 7 工具 + bash 黑名单
└── agent.py    # _dispatch_tool_calls：并发分派 + 打印锁
tests/
└── test_tool_use.py   # 24 个新单测
```

### 2.1 Tool 2.0：从「四要素」到「带标记的类型」

迭代 1 的 Tool 只有 name/description/parameters/handler 四要素。本轮新增三个字段，
全部为下一迭代埋点：

```python
@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    handler: Callable[[dict], str]
    # ---- 迭代 2 新增 ----
    is_concurrency_safe: bool = False   # 只读无副作用 → 同轮可并行
    is_read_only: bool = False          # 只读标记（迭代 3 权限模式读取它）
    max_result_chars: int = 8000        # 工具级结果预算（CC：Bash 30_000 / Read ∞）
```

对应关系（claude-code `src/Tool.ts` → mini_agent）：

| CC 字段 | mini_agent | 说明 |
|---------|-----------|------|
| `isConcurrencySafe()` | `is_concurrency_safe` | 相同输入可否并行 |
| `isReadOnly()` | `is_read_only` | 影响权限模式（迭代 3 用） |
| `maxResultSizeChars` | `max_result_chars` | 超限持久化到磁盘（CC），教学版直接截断 |
| `validateInput()` | `validate_arguments()` | 执行前校验，在权限检查之前 |
| `buildTool()` | `Tool(...)` 构造 | 类型安全的工厂（CC 用 TS satisfies，Python 用 dataclass） |

### 2.2 参数校验：第一道防线（在权限之前）

CC 的调用链路是 `validateInput → canUseTool → checkPermissions → call()`。
校验放在权限之前的原因：**非法参数根本轮不到问权限**。

教学版校验刻意轻量（不引入 jsonschema 依赖）：

- required 字段存在性
- 声明类型的 isinstance 检查（注意两个坑：`str` 类型不能直接当 checker 用——
  `str(123)` 是构造调用恒为真；`bool` 是 `int` 子类——integer 必须排除 bool）

校验失败抛 `ToolError` → 错误文本回填 → 模型自纠，循环永不崩。测试验证了这条链路
（`test_validation_error_fed_back_to_model`）。

### 2.3 三个新工具的语义设计

**bash**（对应 CC Bash）：

- `subprocess.run(shell=True, capture_output=True)` + cwd 围栏
- 输出格式 `exit_code=N (耗时 ms)\n stdout [stderr]`——退出码是模型判断
  「命令成功了吗」的第一信号
- 超时：默认 30s，上限 120s（防止模型挂个 `sleep 9999`）
- 临时危险命令黑名单（`sudo`/`rm -rf /`/`mkfs`…）——迭代 3 的三道闸门权限管线会替换它
- 结果预算 12000 字符（命令输出普遍大于文件读取）

**edit_file**（对应 CC Edit）—— 本轮最值得学的语义：

```
old_string 必须在文件中「恰好出现一次」：
  0 次   → 报错「未找到」（提示空白缩进需完全一致）
  多次   → 报错「不唯一」，引导模型扩大上下文或用 replace_all
```

这个「报错教学」设计直接复刻 CC：它逼模型学会**精准编辑**而不是盲目替换第一个
匹配——替换错地方比不替换更危险。24 个单测里 5 个专测这套语义。

**grep**（对应 CC Grep）：

- `re` 逐行匹配，输出 `文件:行号:内容`
- 忽略目录（.git/.venv/\_\_pycache\_\_/node_modules…）
- glob 过滤对齐 ripgrep 直觉：不含 `/` 的模式（`*.py`）任意深度按 basename 匹配；
  含路径分隔符的按字面匹配
- 200 条匹配上限（防止把整个 .venv 刷进上下文）

### 2.4 并发分派：全或无策略

`_dispatch_tool_calls()` 是本轮的调度核心：

```python
if len(tool_calls) > 1 and all(只读安全 for tc in tool_calls):
    → ThreadPoolExecutor 并发执行
else:
    → 全部串行，保持原始顺序
```

**为什么是「全或无」而不是「只读并发 + 写串行」？** 这是最值得讲的取舍：
若同一轮里 read 和 write 指向同一个文件，重排序会改变 read 观察到的内容
（读到写前还是写后）。全或无保证：**只要可能出现写，行为与纯串行完全一致**——
正确性优先，收益打折也认。

Claude Code 的真实实现更精细（per-tool isConcurrencySafe + StreamingToolExecutor
在模型流式输出时就启动只读工具），教学版先落地核心判定。

三个工程细节：

- **结果保序**：`futures` 按提交序排列，`[fut.result() for fut in futures]` 天然保序；
  API 协议要求每个 `tool_call_id` 都有配对的 tool 消息，按原序回填最稳
- **打印锁**：`_say()` 加 `threading.Lock()`，避免并发时多线程输出在终端交错撕裂
- **未知工具降级**：批次里出现未知工具名 → 不满足 all(safe) → 走串行安全路径

### 2.5 内置工具全景（7 个）

| 工具 | 只读 | 并发安全 | 结果预算 | 对应 CC 工具 |
|------|------|---------|---------|-------------|
| read_file | ✅ | ✅ | 8000 | FileRead |
| list_dir | ✅ | ✅ | 8000 | — |
| glob_files | ✅ | ✅ | 8000 | Glob |
| grep | ✅ | ✅ | 8000 | Grep |
| write_file | ❌ | ❌ | 8000 | FileWrite |
| edit_file | ❌ | ❌ | 8000 | FileEdit |
| bash | ❌ | ❌ | 12000 | Bash |

## 3. 运行指令

```bash
cd /Users/nian/CatPaw/agent-mini-harness

# 单元测试（34 个，无需 key；2.2s 跑完）
.venv/bin/python -m unittest discover -s tests -v

# 真实运行：并发分派可直接观察
.venv/bin/python -m mini_agent "用 grep 找出 mini_agent 里所有 raise ToolError 的行，再读 agent.py 前 30 行"
# 终端会出现：⚡ 2 个只读工具 → 并发执行

.venv/bin/python -m mini_agent "把 requirements.txt 里 python-dotenv 的版本要求改成 >=1.1.0，然后跑 python -c 'import dotenv' 验证"
# 终端走串行路径（edit_file 不安全）：无 ⚡ 标记，逐个执行

.venv/bin/python -m mini_agent "数一下 tests 目录有多少个测试方法" --quiet
```

## 4. 测试如何验证（24 个新单测）

| 组 | 验证点 |
|----|--------|
| ValidationTests ×4 | 缺参数、类型错、bool 冒充 int、校验错误回填自纠 |
| BashToolTests ×6 | 正常执行、非零退出码、stderr、超时、黑名单拦截、cwd 生效 |
| EditFileToolTests ×5 | 唯一替换、不唯一拒绝（引导扩大上下文）、replace_all、未找到、文件不存在 |
| GrepToolTests ×4 | 正则、glob 过滤、无匹配、无效正则 |
| ConcurrencyDispatchTests ×5 | **并发计时**（2×0.3s 并发 <0.5s）、**混合串行**（≥0.58s）、内置标记、**结果保序**（慢工具先完成不乱序）、未知工具降级 |

并发正确性无法用断言「看到」——用**计时**说话：`test_all_safe_tools_run_concurrently`
断言总耗时 < 0.5s（串行下限 0.6s），`test_mixed_safe_unsafe_run_sequentially`
断言 ≥ 0.58s。这是并发测试的标准手法（比 mock 线程池更诚实）。

## 5. 本迭代刻意不做的事

- 不做权限系统（迭代 3：三道闸门 + 四态 PermissionResult，替换 bash 黑名单）
- 不做流式输出（迭代 12：StreamingToolExecutor 的真形态）
- 不做 bash 危险命令的 LLM 分类器（CC 的 bash-classifier，属权限迭代）
- 工具结果超限仍直接截断（CC 持久化到磁盘 + 路径回传，归迭代 6 压缩管线）

## 6. 已知局限

- 「全或无」并发策略在「3 读 + 1 写」批次下放弃全部并发收益——细粒度分组（读写依赖分析）留给迭代 12
- grep 的 `*.py` 用 `rglob` 实现，超大树（如 node_modules）无性能保障——忽略目录表已缓解
- bash 黑名单是子串匹配，`"";sudo` 这类拼接绕过存在（教学版已知，迭代 3 权限管线统一解决）
