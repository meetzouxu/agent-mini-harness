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
from .llm import build_client
from .permissions import (
    ANSWER_ALWAYS,
    ANSWER_NO,
    ANSWER_YES,
    PermissionManager,
    load_settings_rules,
)
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
        description="迭代 3：权限管线（三道闸门 + 四态裁决 + Denial Tracking）",
    )
    parser.add_argument("prompt", help="任务描述，例如：统计当前目录下有几个 .py 文件并写入 report.md")
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
    permissions = PermissionManager(mode=mode, rules=rules, on_ask=on_ask)

    agent = Agent(
        client=client,
        tools=default_tools(),
        system_prompt=DEFAULT_SYSTEM_PROMPT,
        max_turns=args.max_turns or cfg.max_turns,
        verbose=not args.quiet,
        model=cfg.model,
        permission_manager=permissions,
    )

    result: RunResult = agent.run(args.prompt)

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


if __name__ == "__main__":
    sys.exit(main())
