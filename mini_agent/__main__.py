"""CLI 入口：python -m mini_agent "任务描述"

对应 Claude Code 的单次任务模式（claude -p "..."）：
一次输入 → 完整跑一轮 Agent Loop → 输出最终结果与运行统计。
（多轮 REPL 交互在迭代 5「维护上下文」中引入。）
"""

from __future__ import annotations

import argparse
import sys

from .agent import Agent, RunResult
from .config import load_config
from .llm import build_client
from .tools import default_tools

DEFAULT_SYSTEM_PROMPT = """\
你是 mini-agent，一个运行在终端里的编码助手，通过工具读写文件完成任务。

规则：
1. 调用工具前先想清楚参数；能用一次工具完成的不要拆成多次。
2. 工具返回错误时，仔细阅读错误信息、修正参数后重试，不要放弃。
3. 任务完成后，用简洁的中文总结你做了什么、改了哪些文件。
4. 所有文件路径都相对当前工作目录。
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mini-agent",
        description="迭代 1：最小 Agent Loop（DeepSeek + 工具调用）",
    )
    parser.add_argument("prompt", help="任务描述，例如：统计当前目录下有几个 .py 文件并写入 report.md")
    parser.add_argument("--max-turns", type=int, default=None, help="最大循环轮次（默认读 .env 或 15）")
    parser.add_argument("--quiet", action="store_true", help="安静模式：只输出最终答案")
    args = parser.parse_args(argv)

    cfg = load_config()
    client = build_client(cfg)
    agent = Agent(
        client=client,
        tools=default_tools(),
        system_prompt=DEFAULT_SYSTEM_PROMPT,
        max_turns=args.max_turns or cfg.max_turns,
        verbose=not args.quiet,
        model=cfg.model,
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
