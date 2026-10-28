"""工具层：Tool 抽象 + 内置文件工具（read_file / write_file / list_dir / glob）。

对应 Claude Code 的 Tool 接口（name / description / JSON Schema / 执行函数）。

安全底线（迭代 3 才有完整权限管线，这里先做一道「工作目录围栏」）：
- 所有路径参数都会被解析为绝对路径，且必须落在当前工作目录内；
- 越界路径（如 ../../、/etc/passwd、~/.ssh）直接抛 ToolError，不触碰文件系统。

工具结果统一为字符串，超长会截断 —— 对应 Claude Code 的
applyToolResultBudget()（工具结果预算截断）的最小版。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

# ---- 常量：工具输出预算（迭代 6 压缩会细化它） ----
MAX_RESULT_CHARS = 8000


class ToolError(Exception):
    """工具级错误：不中断循环，错误文本会作为观察结果回填给模型。"""


# ---------------------------------------------------------------------------
# Tool 抽象
# ---------------------------------------------------------------------------

@dataclass
class Tool:
    """一个可被模型调用的工具 = 元信息 + JSON Schema + 执行函数。"""

    name: str
    description: str
    parameters: dict  # JSON Schema，描述参数结构
    handler: Callable[[dict], str]

    def to_api_schema(self) -> dict:
        """转成 OpenAI/DeepSeek tools 参数要求的格式。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def run(self, arguments: dict) -> str:
        """执行工具并返回（截断后的）字符串结果。"""
        output = self.handler(arguments)
        if len(output) > MAX_RESULT_CHARS:
            output = output[:MAX_RESULT_CHARS] + f"\n...(已截断，完整长度 {len(output)} 字符)"
        return output


# ---------------------------------------------------------------------------
# 工作目录围栏
# ---------------------------------------------------------------------------

def safe_path(raw: str) -> Path:
    """把工具参数里的路径解析为 cwd 内的绝对路径，越界即抛 ToolError。"""
    if not raw or not raw.strip():
        raise ToolError("path 不能为空")
    root = Path.cwd().resolve()
    p = (root / raw).resolve() if not Path(raw).is_absolute() else Path(raw).resolve()
    if not p.is_relative_to(root):
        raise ToolError(f"路径越界：{raw} 不在工作目录 {root} 内（迭代 3 将引入完整权限管线）")
    return p


# ---------------------------------------------------------------------------
# 内置工具实现（每个返回字符串）
# ---------------------------------------------------------------------------

def _read_file(args: dict) -> str:
    path = safe_path(args["path"])
    if not path.exists():
        raise ToolError(f"文件不存在: {args['path']}")
    if path.is_dir():
        raise ToolError(f"是目录不是文件: {args['path']}")
    text = path.read_text(encoding="utf-8", errors="replace")
    limit = int(args.get("limit", 400))
    lines = text.splitlines()
    if len(lines) > limit:
        kept = lines[:limit]
        return "\n".join(f"{i + 1}\t{line}" for i, line in enumerate(kept)) + (
            f"\n...(共 {len(lines)} 行，仅返回前 {limit} 行，可用 offset 翻页)"
        )
    return "\n".join(f"{i + 1}\t{line}" for i, line in enumerate(lines))


def _write_file(args: dict) -> str:
    path = safe_path(args["path"])
    content = args.get("content", "")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return f"已写入 {args['path']}（{len(content)} 字符，{content.count(chr(10)) + 1} 行）"


def _list_dir(args: dict) -> str:
    path = safe_path(args.get("path", "."))
    if not path.is_dir():
        raise ToolError(f"不是目录: {args['path']}")
    entries = sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name))
    if not entries:
        return "(空目录)"
    lines = [f"{'[dir] ' if e.is_dir() else '[file]'}{e.name}" for e in entries]
    return "\n".join(lines) + f"\n共 {len(lines)} 项"


def _glob_files(args: dict) -> str:
    pattern = args["pattern"]
    matches = sorted(Path.cwd().glob(pattern))
    if not matches:
        return f"没有匹配 {pattern} 的文件"
    lines = []
    for m in matches[:200]:
        rel = m.relative_to(Path.cwd())
        lines.append(f"{'[dir] ' if m.is_dir() else ''}{rel}")
    return "\n".join(lines) + f"\n共 {len(matches)} 项（最多展示 200）"


# ---------------------------------------------------------------------------
# 工具集装配
# ---------------------------------------------------------------------------

def default_tools() -> list[Tool]:
    """迭代 1 的默认工具集：刻意最小（4 个），后续迭代逐步扩展。"""
    return [
        Tool(
            name="read_file",
            description="读取文本文件内容，带行号返回。文件较长时可用 limit 控制返回行数。",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对当前工作目录的文件路径"},
                    "limit": {"type": "integer", "description": "最多返回的行数，默认 400"},
                },
                "required": ["path"],
            },
            handler=_read_file,
        ),
        Tool(
            name="write_file",
            description="将文本内容写入文件（覆盖写入），父目录不存在时自动创建。",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对当前工作目录的文件路径"},
                    "content": {"type": "string", "description": "要写入的完整文本内容"},
                },
                "required": ["path", "content"],
            },
            handler=_write_file,
        ),
        Tool(
            name="list_dir",
            description="列出目录下的文件与子目录。",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "目录路径，默认当前目录"},
                },
                "required": [],
            },
            handler=_list_dir,
        ),
        Tool(
            name="glob_files",
            description="按通配符模式搜索文件，例如 '**/*.py' 或 'docs/*.md'。",
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "glob 通配符模式"},
                },
                "required": ["pattern"],
            },
            handler=_glob_files,
        ),
    ]
