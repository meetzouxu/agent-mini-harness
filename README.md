# agent-mini-harness

渐进式复刻 Agent 能力：用 Python 从零复刻 Claude Code，从 Agent Loop 开始，
一步步迭代到拥有维护上下文、编排工具、保存状态、控制并发、异步执行、
失败重试、权限校验、日志监控等能力的完备 Agent。

**路线图**：[docs/ROADMAP.md](docs/ROADMAP.md) —— 8 阶段 23 次迭代；
能力全景：[docs/references/agent-capability-map.md](docs/references/agent-capability-map.md)

**当前进度**：迭代 1 ✅ Agent Loop（[拆解](docs/iterations/01-agent-loop.md)）｜迭代 2 ✅ 工具编排（[拆解](docs/iterations/02-tool-use.md)）｜迭代 3 ✅ 权限管线（[拆解](docs/iterations/03-permissions.md)）

## 快速开始

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env   # 填入 DEEPSEEK_API_KEY（.env 已 gitignore，不会入库）

.venv/bin/python -m unittest discover -s tests        # 单测（无需 key）
.venv/bin/python -m mini_agent "你的任务描述"           # 真实运行
```

## 项目结构

```
mini_agent/       # 核心包：agent loop / tools / config / llm / cli
tests/            # 假客户端单测，不依赖真实 API
docs/
├── ROADMAP.md                          # 迭代路线图（8 阶段 23 迭代）
├── iterations/                         # 每迭代拆解文档
└── references/                         # Agent 能力地图（概念与机制参考）
.env              # 密钥（gitignored，永不入库）
```

## 参考来源

- [claude-code-best/claude-code](https://github.com/claude-code-best/claude-code) —— Claude Code CLI 的逆向开源仓库（完整源码 `src/` + 白皮书 `docs/`：the-loop / multi-turn / compaction 等章节），本项目的唯一能力参考
- 模型接入：DeepSeek API（OpenAI 兼容协议，见 [.env.example](.env.example)）
