"""CLI 入口：python -m mini_agent "任务描述"

对应 Claude Code 的单次任务模式（claude -p "..."）：
一次输入 → 完整跑一轮 Agent Loop → 输出最终结果与运行统计。
（多轮 REPL 交互在迭代 5「维护上下文」中引入。）

迭代 3：权限系统接入。
- 默认模式 default：只读自动放行，写操作终端交互确认（y/a/n）
- --permission-mode plan / acceptEdits / bypass
- 项目级规则 .mini-agent/settings.json 自动加载
- --yes：非交互场景下 ask 一律拒绝（安全默认）
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .agent import Agent, RunResult
from .config import load_config
from .hooks import EVENT_POST_TOOL_USE, EVENT_PRE_TOOL_USE, Hook, HookRegistry, HookResult
from .llm import build_client
from .permissions import (
    ANSWER_ALWAYS,
    ANSWER_NO,
    ANSWER_YES,
    PermissionManager,
    load_settings_rules,
)
from .prompt import default_registry
from .session import QueryEngine
from .memory import UserMemory
from .tools import default_tools

DEFAULT_SYSTEM_PROMPT = """\
你是 mini-agent，一个运行在终端里的编码助手，通过工具读写文件、搜索代码、执行命令来完成任务。

规则：
1. 调用工具前先想清楚参数；能用一次工具完成的不要拆成多次。
2. 工具返回错误时，仔细阅读错误信息、修正参数后重试，不要放弃。
   特别是 edit_file：old_string 必须唯一，报错不唯一时扩大上下文重试。
3. 被权限拒绝的操作不要原样重试 —— 阅读拒绝理由，改变策略或换一条路。
4. 任务完成后，用简洁的中文总结你做了什么、改了哪些文件。
5. 所有文件路径都相对当前工作目录。
"""

_VALID_MODES = ("default", "plan", "acceptEdits", "bypass")

# 工具结果超过该字符数时 PostToolUse hook 发出告警（迭代 15 日志监控的伏笔）
LARGE_OUTPUT_WARN_CHARS = 6000


def _builtin_hooks() -> HookRegistry:
    """内置示例 hooks（迭代 4）：两个有实际价值的示范。

    1. PreToolUse 守卫 .env —— 密钥文件不允许任何工具读写（除 read 白名单外
       全 deny）：密钥安全是 Agent 工程的硬边界示范
    2. PostToolUse 大输出告警 —— 结果超阈值时 system_message 提醒用户，
       为迭代 6 压缩与迭代 15 日志监控埋伏笔
    """
    registry = HookRegistry()

    def guard_env_file(payload: dict) -> HookResult:
        path = str(payload.get("arguments", {}).get("path", ""))
        if path == ".env" or path.endswith("/.env"):
            return HookResult.deny(f"禁止访问密钥文件 {path}（.env 存放 API key）")
        return HookResult()

    def warn_large_output(payload: dict) -> HookResult:
        result = str(payload.get("result", ""))
        if len(result) > LARGE_OUTPUT_WARN_CHARS:
            return HookResult(
                system_message=(
                    f"工具 {payload.get('tool_name')} 输出 {len(result)} 字符，"
                    f"超过 {LARGE_OUTPUT_WARN_CHARS}——建议缩小范围或用更精确的模式"
                )
            )
        return HookResult()

    registry.register(
        Hook(
            event=EVENT_PRE_TOOL_USE,
            name="guard-env-file",
            handler=guard_env_file,
            matcher="write_file|edit_file",
            source="builtin",
        )
    )
    registry.register(
        Hook(
            event=EVENT_POST_TOOL_USE,
            name="warn-large-output",
            handler=warn_large_output,
            matcher="*",
            source="builtin",
        )
    )
    return registry


def _terminal_ask(tool_name: str, arguments: dict, reason: str) -> str:
    """终端交互确认：y=本次允许 a=总是允许 n=拒绝。"""
    print()
    print(f"🔒 需要确认 —— {tool_name}")
    for k, v in arguments.items():
        text = str(v)
        print(f"   {k} = {text[:80]}{'...' if len(text) > 80 else ''}")
    print(f"   原因: {reason}")
    try:
        answer = input("   允许吗？ [y]允许 / [a]总是允许 / [n]拒绝 > ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return ANSWER_NO
    if answer in ("a", "always"):
        return ANSWER_ALWAYS
    if answer in ("y", "yes", ""):
        return ANSWER_YES
    return ANSWER_NO


def _auto_deny_ask(tool_name: str, arguments: dict, reason: str) -> str:
    """--yes/--quiet 场景：无交互通道，ask 一律拒绝（安全默认）。"""
    return ANSWER_NO


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mini-agent",
        description="迭代 5：多轮会话 + System Prompt 分段组装（REPL / 单次）",
    )
    parser.add_argument(
        "prompt",
        nargs="?",
        default=None,
        help="任务描述（省略则进入 REPL 多轮会话）",
    )
    parser.add_argument("--max-turns", type=int, default=None, help="最大循环轮次（默认读 .env 或 15）")
    parser.add_argument("--quiet", action="store_true", help="安静模式：只输出最终答案（ask 一律拒绝）")
    parser.add_argument(
        "--permission-mode",
        default="default",
        choices=_VALID_MODES,
        help="权限模式（默认 default：只读放行、写操作交互确认）",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="非交互自动放行写操作（危险；等价于 acceptEdits+所有工具放行的 bypass 语义弱化版）",
    )
    args = parser.parse_args(argv)

    cfg = load_config()
    client = build_client(cfg)

    # 权限装配：settings 规则 + 模式 + 交互回调
    rules = load_settings_rules(Path.cwd())
    if args.yes:
        mode = "bypass"
        on_ask = _auto_deny_ask
    elif args.quiet:
        # quiet 模式无交互通道，ask 一律拒绝（安全默认）
        mode = args.permission_mode
        on_ask = _auto_deny_ask
    else:
        mode = args.permission_mode
        on_ask = _terminal_ask
    # 迭代 7：跨会话记忆（.memory/ 纯文件存储，gitignored）
    memory = UserMemory(Path(".memory"))

    permissions = PermissionManager(mode=mode, rules=rules, on_ask=on_ask)
    hooks = _builtin_hooks()
    tools = default_tools()

    # 迭代 5：system prompt 由 SectionRegistry 组装（identity 静态 + tools/workspace 动态）
    # Agent 不再直接携带 system_prompt，由 QueryEngine 每轮组装并维护替换
    agent = Agent(
        client=client,
        tools=tools,
        system_prompt=None,
        max_turns=args.max_turns or cfg.max_turns,
        verbose=not args.quiet,
        model=cfg.model,
        permission_manager=permissions,
        hooks=hooks,
    )
    registry = default_registry(tools, user_memory=memory)  # 捕获 tools/memory 引用：变化 → 下轮自动反映
    engine = QueryEngine(agent, registry, memory=memory)

    if args.prompt is None:
        return _run_repl(engine)

    result: RunResult = engine.submit_message(args.prompt)

    print()
    print("=" * 60)
    print(result.final_text or "(模型未输出文本)")
    print("=" * 60)
    print(
        f"[stats] stop={result.stop_reason} turns={result.turn_count} "
        f"tool_calls={result.tool_calls_count} "
        f"tokens(in/out)={result.input_tokens}/{result.output_tokens}"
    )
    return 0 if result.stop_reason == "completed" else 1


def _run_repl(engine: QueryEngine) -> int:
    """REPL 多轮会话（对应 claude 终端交互模式：上下文跨轮保留）。"""
    print("mini-agent REPL —— 迭代 7 记忆系统")
    print("命令：/clear 清空 · /compact 压缩 · /memory 记忆 · /stats 统计 · exit 退出")
    while True:
        try:
            text = input("\nmini-agent> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not text:
            continue
        if text in ("exit", "quit", "/exit", "/quit"):
            break
        if text == "/clear":
            engine.clear()
            print("（已清空会话与 section 缓存）")
            continue
        if text == "/compact" or text.startswith("/compact "):
            instruction = text[len("/compact"):].strip()
            if not engine.messages:
                print("（会话为空，无需压缩）")
                continue
            report = engine.compact_now(instruction)
            print(
                f"（手动压缩完成：消息 {len(engine.messages)} 条，"
                f"估算 token {engine.compaction_config.context_window_tokens} 窗口内）"
            )
            continue
        if text == "/memory":
            entries = engine.memory.list_all() if engine.memory else []
            if not entries:
                print("（记忆为空——对话中说出「记住 …」或表达偏好即可沉淀）")
            for e in entries:
                print(f"  [{e.category}] {e.content}")
            continue
        if text == "/stats":
            s = engine.stats
            print(
                f"[session] turns={s.turns} messages={engine.message_count()} "
                f"tokens(in/out)={s.input_tokens}/{s.output_tokens} "
                f"tool_calls={s.tool_calls} errors={s.tool_errors} "
                f"prompt-cache-hit={engine.cache_hit_rate():.0%} "
                f"compactions={s.compactions} tokens_saved~{s.tokens_saved}"
            )
            continue
        result = engine.submit_message(text)
        print()
        print("=" * 60)
        print(result.final_text or "(模型未输出文本)")
        print("=" * 60)
        s = engine.stats
        print(
            f"[session] turn={s.turns} messages={engine.message_count()} "
            f"tokens(in/out)={s.input_tokens}/{s.output_tokens} "
            f"prompt-cache-hit={engine.cache_hit_rate():.0%}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
