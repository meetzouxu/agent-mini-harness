"""工具层：buildTool 式 Tool 抽象 + 7 个内置工具 + 并发安全判定 + 参数校验。

迭代 2 的核心升级（参考 claude-code src/Tool.ts 的工具类型设计）：

Tool 2.0 —— 对应 claude-code 的核心字段：
    name / description / parameters(JSON Schema) / handler  → 迭代 1 已有
    is_concurrency_safe  → 本轮新增：相同输入可否并行（FileRead/Glob/ls 为真，
                            rm/Bash/Edit 为假），是「全并发或全串行」分派依据
    is_read_only         → 本轮新增：只读标记（迭代 3 权限管线会用到，先埋点）
    max_result_chars     → 本轮新增：工具级结果预算（对应 CC 的 maxResultSizeChars，
                            Bash 30_000 / Read Infinity，每工具各不相同）

调用链路（对应 CC 的 10 步，本迭代实现加粗部分）：
    API 返回 tool_use → 查表 findToolByName → **validateInput（参数校验）**
    → [迭代 3：canUseTool/checkPermissions] → **call（执行）** → **ToolResult（截断回填）**

安全底线（迭代 3 才有完整权限管线，这里保留两道粗闸）：
- 工作目录围栏 safe_path()（迭代 1 已有）
- Bash 危险命令黑名单（本轮新增的临时闸，迭代 3 用三道闸门权限管线替换）
"""

from __future__ import annotations

import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

# ---- 常量 ----
DEFAULT_MAX_RESULT_CHARS = 8000   # 工具结果预算默认值（迭代 6 压缩会细化它）
MAX_GREP_MATCHES = 200            # grep 最多返回的匹配行数
BASH_TIMEOUT_DEFAULT = 30         # bash 默认超时（秒）
BASH_TIMEOUT_MAX = 120            # bash 超时上限（秒）


class ToolError(Exception):
    """工具级错误：不中断循环，错误文本会作为观察结果回填给模型。"""


# ---------------------------------------------------------------------------
# Tool 抽象（buildTool 风格）
# ---------------------------------------------------------------------------

# JSON Schema type → 校验函数（注意：必须是 isinstance 检查，不能直接用类型类，
# 否则 checker(value) 会变成构造函数调用；bool 是 int 子类，integer 要排除 bool）
_TYPE_MAP = {
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
}


@dataclass
class Tool:
    """一个可被模型调用的工具。

    对应 claude-code buildTool() 创建的 Tool 类型（35 字段的精简教学版）：
    核心四要素 + 并发/只读标记 + 工具级结果预算。
    """

    name: str
    description: str
    parameters: dict  # JSON Schema，描述参数结构
    handler: Callable[[dict], str]

    # ---- 迭代 2 新增标记 ----
    is_concurrency_safe: bool = False  # 只读无副作用 → 同轮可并行执行
    is_read_only: bool = False         # 只读操作（迭代 3 权限模式会读取它）
    max_result_chars: int = DEFAULT_MAX_RESULT_CHARS  # 工具级结果预算

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

    def validate_arguments(self, arguments: dict) -> None:
        """轻量参数校验（对应 CC validateInput，在执行前、权限检查之前）。

        只做两件事：required 字段存在 + 声明类型的类型检查。
        刻意不引入 jsonschema 依赖 —— 教学版够用，错误也回填给模型自纠。
        """
        props = self.parameters.get("properties", {})
        for key in self.parameters.get("required", []):
            if key not in arguments:
                raise ToolError(f"缺少必填参数: {key}")
        for key, value in arguments.items():
            spec = props.get(key)
            if spec is None:
                continue  # 未声明的参数放行（宽容策略，CC 同样允许附加字段）
            declared = spec.get("type")
            checker = _TYPE_MAP.get(declared)
            if checker and not checker(value):
                raise ToolError(f"参数 {key} 类型错误：期望 {declared}，收到 {type(value).__name__}")

    def run(self, arguments: dict) -> str:
        """校验 → 执行 → 截断。"""
        self.validate_arguments(arguments)
        output = self.handler(arguments)
        if len(output) > self.max_result_chars:
            output = output[: self.max_result_chars] + (
                f"\n...(已截断，完整长度 {len(output)} 字符)"
            )
        return output


# ---------------------------------------------------------------------------
# 工作目录围栏（迭代 1 已有，保持不变）
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
# 内置工具实现
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


# ---- 迭代 2 新增：bash ----

# 临时危险命令黑名单（迭代 3 的三道闸门权限管线将替换它）
_BLOCKED_BASH_PATTERNS = (
    "rm -rf /", "rm -rf ~", "sudo ", "mkfs", "shutdown", "reboot",
    "dd if=", ":(){", " > /dev/",
)


def _bash(args: dict) -> str:
    command = args["command"]
    for pattern in _BLOCKED_BASH_PATTERNS:
        if pattern in command:
            raise ToolError(
                f"命令被安全黑名单拦截: {command!r}（命中 {pattern!r}；"
                f"迭代 3 将引入完整权限管线）"
            )
    timeout = min(int(args.get("timeout", BASH_TIMEOUT_DEFAULT)), BASH_TIMEOUT_MAX)
    started = time.perf_counter()
    try:
        proc = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=Path.cwd(),
        )
    except subprocess.TimeoutExpired:
        raise ToolError(f"命令超时（>{timeout}s）: {command}") from None
    duration_ms = (time.perf_counter() - started) * 1000
    parts = []
    if proc.stdout:
        parts.append(proc.stdout.rstrip())
    if proc.stderr:
        parts.append(f"[stderr]\n{proc.stderr.rstrip()}")
    output = "\n".join(parts) or "(无输出)"
    return f"exit_code={proc.returncode} ({duration_ms:.0f} ms)\n{output}"


# ---- 迭代 2 新增：edit_file ----

def _edit_file(args: dict) -> str:
    """精确文本替换 —— 复刻 claude-code Edit 工具的核心语义。

    old_string 必须在文件中恰好出现一次；出现多次会报错并要求提供
    更长的上下文（含空行）或改用 replace_all —— 这个「报错教学」设计
    能引导模型学会精准编辑，而不是盲目替换第一个匹配。
    """
    path = safe_path(args["path"])
    if not path.exists():
        raise ToolError(f"文件不存在: {args['path']}")
    old, new = args["old_string"], args["new_string"]
    replace_all = bool(args.get("replace_all", False))
    if not old:
        raise ToolError("old_string 不能为空（请提供要匹配的上下文）")

    text = path.read_text(encoding="utf-8", errors="replace")
    count = text.count(old)
    if count == 0:
        raise ToolError(f"old_string 在 {args['path']} 中未找到（注意空白与缩进需完全一致）")
    if count > 1 and not replace_all:
        raise ToolError(
            f"old_string 出现了 {count} 次，不唯一。"
            f"请扩大上下文（包含前后空行或更多行）使其唯一，或设置 replace_all=true"
        )
    new_text = text.replace(old, new) if replace_all else text.replace(old, new, 1)
    path.write_text(new_text, encoding="utf-8")
    replaced = count if replace_all else 1
    return f"已替换 {replaced} 处，文件 {args['path']} 更新完成"


# ---- 迭代 2 新增：grep ----

_GREP_IGNORE_DIRS = {".git", ".venv", "__pycache__", "node_modules",
                     ".pytest_cache", ".mypy_cache", "dist", "build"}


def _grep(args: dict) -> str:
    pattern = args["pattern"]
    try:
        regex = re.compile(pattern)
    except re.error as e:
        raise ToolError(f"无效正则: {e}") from None
    glob_filter = args.get("glob", "**/*")
    root = safe_path(args.get("path", "."))

    # glob 过滤语义（对齐 ripgrep -g 的直觉）：
    # 不含 '/' 和 '**' 的模式（如 '*.py'）→ 任意深度按 basename 匹配；
    # 含路径分隔符的模式（如 'src/*.py'）→ 按字面路径匹配
    if "/" in glob_filter or "**" in glob_filter:
        candidates = root.glob(glob_filter)
    else:
        candidates = root.rglob(glob_filter)

    matches: list[str] = []
    truncated = False
    for f in sorted(candidates):
        if not f.is_file():
            continue
        if any(part in _GREP_IGNORE_DIRS for part in f.parts):
            continue
        try:
            lines = f.read_text(encoding="utf-8", errors="strict").splitlines()
        except (UnicodeDecodeError, OSError):
            continue  # 二进制/不可读文件直接跳过
        for line_no, line in enumerate(lines, 1):
            if regex.search(line):
                matches.append(f"{f.relative_to(Path.cwd())}:{line_no}:{line.strip()}")
                if len(matches) >= MAX_GREP_MATCHES:
                    truncated = True
                    break
        if truncated:
            break
    if not matches:
        return f"没有匹配 /{pattern}/ 的行"
    suffix = f"\n...(达到 {MAX_GREP_MATCHES} 条上限，已截断)" if truncated else ""
    return "\n".join(matches) + f"\n共 {len(matches)} 处匹配" + suffix


# ---------------------------------------------------------------------------
# 工具集装配（对应 CC getAllBaseTools() 的分层组装，教学版为平铺列表）
# ---------------------------------------------------------------------------

def default_tools() -> list[Tool]:
    """内置工具集：7 个。

    并发安全标记一览（对应 CC isConcurrencySafe 判定）：
        只读（read/list/glob/grep）→ True，同轮可并行
        有副作用（write/edit/bash）→ False，必须串行保序
    """
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
            is_concurrency_safe=True,
            is_read_only=True,
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
            name="edit_file",
            description=(
                "精确文本替换：old_string 必须在文件中唯一出现（否则报错），"
                "用 new_string 替换。适合小幅修改而非整文件重写。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "目标文件路径"},
                    "old_string": {"type": "string", "description": "要被替换的原文（需唯一）"},
                    "new_string": {"type": "string", "description": "替换后的新文本"},
                    "replace_all": {"type": "boolean", "description": "替换全部出现处，默认 false"},
                },
                "required": ["path", "old_string", "new_string"],
            },
            handler=_edit_file,
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
            is_concurrency_safe=True,
            is_read_only=True,
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
            is_concurrency_safe=True,
            is_read_only=True,
        ),
        Tool(
            name="grep",
            description="按正则搜索文件内容，返回 '文件:行号:内容'。可用 glob 过滤文件类型（如 '*.py'，任意深度）。",
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "正则表达式"},
                    "glob": {"type": "string", "description": "文件过滤通配符，如 '*.py'，默认全部"},
                    "path": {"type": "string", "description": "搜索根目录，默认当前目录"},
                },
                "required": ["pattern"],
            },
            handler=_grep,
            is_concurrency_safe=True,
            is_read_only=True,
        ),
        Tool(
            name="bash",
            description=(
                "在 shell 中执行命令并返回输出、退出码与耗时。"
                "适合运行测试、git 操作、统计命令等。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "要执行的 shell 命令"},
                    "timeout": {"type": "integer", "description": "超时秒数，默认 30，上限 120"},
                },
                "required": ["command"],
            },
            handler=_bash,
            max_result_chars=12000,  # 工具级预算：命令输出往往比文件读取大（CC Bash 为 30_000）
        ),
    ]
