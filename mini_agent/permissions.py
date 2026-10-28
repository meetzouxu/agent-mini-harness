"""权限管线 —— 迭代 3 的核心。

复刻 Claude Code 的三级权限裁决（allow / ask / deny）与闸门顺序：

    闸门 0：工具自带 check_permissions()   → bash 黑名单 / 路径围栏（硬边界）
    闸门 1：deny 规则（含分层配置）         → 命中直接拒绝
    闸门 2：allow / ask 规则匹配            → 命中放行或询问
    闸门 3：默认行为（按 permission_mode）  → default / plan / acceptEdits / bypass

对应 claude-code（src/utils/permissions/）：

| CC 概念 | 本模块 |
|---------|--------|
| PermissionResult（types/permissions.ts:241-266） | PermissionResult + Behavior 枚举 |
| canUseTool() → checkPermissions() 管线 | PermissionManager.check()（纯函数）/ decide()（带交互） |
| 8 层规则来源（settings→policy→cli→session） | 教学版 3 层：项目 settings / CLI 参数 / 会话 session |
| Denial Tracking（denialTracking.ts，3 次/20 次） | _record_denial：同一工具连续 3 次拒绝 → 注入「改变策略」提示 |
| 「Always allow」写回规则层 | decide() 的 always 分支 → session 规则 |
| 权限模式（default/plan/acceptEdits/bypass） | PermissionManager.mode |

匹配三维度（对应 CC toolMatchesRule）：
- 工具名：精确或 fnmatch 通配（"bash" / "mcp__server__*" / "*"）
- bash 命令：ruleContent "git *" → 前缀匹配
- 文件路径：ruleContent "src/**" → fnmatch（* 跨目录，恰好等价 glob 的 **）
"""

from __future__ import annotations

import fnmatch
import json
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from .tools import Tool

# ---- Denial Tracking 常量（对应 CC DENIAL_LIMITS） ----
MAX_CONSECUTIVE_DENIALS = 3   # 同一工具连续拒绝上限 → 注入「改变策略」提示
MAX_TOTAL_DENIALS = 20        # 总拒绝上限

# acceptEdits 模式下自动放行的文件编辑类工具
FILE_EDIT_TOOLS = frozenset({"write_file", "edit_file"})

# on_ask 回调的合法返回值
ANSWER_YES = "yes"
ANSWER_ALWAYS = "always"
ANSWER_NO = "no"

AskCallback = Callable[[str, dict, str], str]  # (tool_name, arguments, reason) -> "yes"/"always"/"no"


class Behavior(str, Enum):
    """权限裁决的四种行为。

    allow / deny / ask 是最终裁决（对应 CC PermissionResult 的 behavior）；
    check_permissions 钩子返回 None 表示「本层不裁决，交给通用闸门」
    —— 这就是 CC 里 passthrough 的语义。
    """

    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


@dataclass
class PermissionResult:
    """一次权限裁决的结果（对应 CC PermissionResult 的教学精简版）。

    CC 还有 updatedInput（权限层可改写参数）与 suggestions 等字段，
    留给后续 hook 迭代。
    """

    behavior: Behavior
    reason: str = ""


@dataclass
class PermissionRule:
    """一条权限规则（对应 CC PermissionRule）。

    content 语义按工具分维度：
      None            → 整工具匹配（"read_file" 匹配所有 read_file 调用）
      bash + "git *"  → 命令前缀匹配
      文件工具 + "src/**" → 路径 glob 匹配
    """

    tool: str
    behavior: Behavior
    content: str | None = None
    source: str = "session"  # settings / cli / session

    def describe(self) -> str:
        return self.tool if self.content is None else f"{self.tool}:{self.content}"


# ---------------------------------------------------------------------------
# 规则匹配（对应 CC toolMatchesRule 的三维度）
# ---------------------------------------------------------------------------

def _rule_matches(rule: PermissionRule, tool_name: str, arguments: dict) -> bool:
    """判断一条规则是否命中当前工具调用。"""
    # 维度 1：工具名（支持通配符，对应 CC 的 "mcp__server__*"）
    if rule.tool != "*" and not fnmatch.fnmatchcase(tool_name, rule.tool):
        return False
    if rule.content is None:
        return True  # 无 content → 整工具匹配

    content = rule.content
    # 维度 2：bash 命令（前缀或精确）
    # 注意：前缀保留尾随空格 —— "git *" 的前缀是 "git "，
    # 这样 "github-cli" 才不会误匹配
    if tool_name == "bash":
        command = str(arguments.get("command", ""))
        if content.endswith("*"):
            return command.startswith(content[:-1])
        return command == content
    # 维度 3：文件路径 glob（fnmatch 的 * 跨目录匹配，恰好等价 glob 的 **）
    path = str(arguments.get("path", ""))
    if not path:
        return False
    return fnmatch.fnmatch(path, content)


# ---------------------------------------------------------------------------
# 管理器：三道闸门 + 交互裁决 + Denial Tracking
# ---------------------------------------------------------------------------

class PermissionManager:
    """权限管理器：check() 纯函数判定，decide() 处理交互与记录。"""

    def __init__(
        self,
        mode: str = "default",
        rules: list[PermissionRule] | None = None,
        on_ask: AskCallback | None = None,
    ) -> None:
        self.mode = mode  # default / plan / acceptEdits / bypass
        self.rules: list[PermissionRule] = list(rules or [])
        self.on_ask = on_ask
        self._consecutive: dict[str, int] = {}  # 工具名 → 连续拒绝次数
        self.total_denials = 0

    # ---- 闸门（纯函数，无副作用；并发预检也用它） ----

    def check(self, tool: "Tool", arguments: dict) -> PermissionResult:
        """走完三道闸门，给出裁决。不触发交互、不记录拒绝 —— 可安全并发调用。"""
        # 闸门 0：工具自带硬边界（黑名单/越界路径）
        if tool.check_permissions is not None:
            result = tool.check_permissions(arguments)
            if result is not None:
                return result
        # 闸门 1：deny 规则（最高优先级 —— deny 永远压过 allow）
        hit = self._match_rule(Behavior.DENY, tool.name, arguments)
        if hit is not None:
            return PermissionResult(Behavior.DENY, f"命中 deny 规则[{hit.source}]: {hit.describe()}")
        # 闸门 2：allow → ask 规则
        hit = self._match_rule(Behavior.ALLOW, tool.name, arguments)
        if hit is not None:
            return PermissionResult(Behavior.ALLOW, f"命中 allow 规则[{hit.source}]: {hit.describe()}")
        hit = self._match_rule(Behavior.ASK, tool.name, arguments)
        if hit is not None:
            return PermissionResult(Behavior.ASK, f"命中 ask 规则[{hit.source}]: {hit.describe()}")
        # 闸门 3：默认行为（按权限模式）
        return self._default_behavior(tool)

    def peek(self, tool: "Tool", arguments: dict) -> PermissionResult:
        """并发预检专用：与 check() 相同（本来就是纯函数），语义上强调只看不动。"""
        return self.check(tool, arguments)

    # ---- 裁决（带交互与 Denial Tracking，只应在串行路径调用） ----

    def decide(self, tool: "Tool", arguments: dict) -> PermissionResult:
        """check() → 处理 ask 交互 → 记录结果。agent 循环的统一入口。"""
        result = self.check(tool, arguments)
        if result.behavior is Behavior.ALLOW:
            self._record_success(tool.name)
            return result
        if result.behavior is Behavior.DENY:
            return self._record_denial(tool.name, result)
        # ---- ASK：交给用户 ----
        if self.on_ask is None:
            # 无交互通道（quiet 模式/测试）→ 安全默认：拒绝
            return self._record_denial(
                tool.name,
                PermissionResult(Behavior.DENY, f"{result.reason}；当前无交互通道，默认拒绝"),
            )
        answer = self.on_ask(tool.name, arguments, result.reason)
        if answer == ANSWER_ALWAYS:
            # 「总是允许」→ 写入会话级规则（对应 CC persistPermissionUpdates）
            self.rules.append(
                PermissionRule(tool=tool.name, behavior=Behavior.ALLOW, source="session")
            )
            self._record_success(tool.name)
            return PermissionResult(Behavior.ALLOW, "用户选择「总是允许」，已写入会话级规则")
        if answer == ANSWER_YES:
            self._record_success(tool.name)
            return PermissionResult(Behavior.ALLOW, "用户本次允许")
        return self._record_denial(
            tool.name, PermissionResult(Behavior.DENY, "用户拒绝了本次操作")
        )

    # ---- 默认行为（对应 CC 的 permissionMode 表） ----

    def _default_behavior(self, tool: "Tool") -> PermissionResult:
        if self.mode == "bypass":
            return PermissionResult(Behavior.ALLOW, "bypass 模式：全部放行")
        if self.mode == "plan":
            if tool.is_read_only:
                return PermissionResult(Behavior.ALLOW, "plan 模式：只读放行")
            return PermissionResult(Behavior.DENY, "plan 模式：写操作拒绝（只读模式）")
        if self.mode == "acceptEdits":
            if tool.is_read_only:
                return PermissionResult(Behavior.ALLOW, "acceptEdits 模式：只读放行")
            if tool.name in FILE_EDIT_TOOLS:
                return PermissionResult(Behavior.ALLOW, "acceptEdits 模式：工作区内编辑放行")
            return PermissionResult(Behavior.ASK, "acceptEdits 模式：非编辑操作需确认")
        # default：读放行，写询问
        if tool.is_read_only:
            return PermissionResult(Behavior.ALLOW, "default 模式：只读放行")
        return PermissionResult(Behavior.ASK, "default 模式：写操作需确认")

    # ---- 内部 ----

    def _match_rule(
        self, behavior: Behavior, tool_name: str, arguments: dict
    ) -> PermissionRule | None:
        for rule in self.rules:
            if rule.behavior is behavior and _rule_matches(rule, tool_name, arguments):
                return rule
        return None

    def _record_success(self, tool_name: str) -> None:
        self._consecutive.pop(tool_name, None)  # 成功 → 重置连续拒绝计数

    def _record_denial(self, tool_name: str, result: PermissionResult) -> PermissionResult:
        self._consecutive[tool_name] = self._consecutive.get(tool_name, 0) + 1
        self.total_denials += 1
        reason = result.reason
        # 死循环防护（对应 CC shouldFallbackToPrompting）
        if self._consecutive[tool_name] >= MAX_CONSECUTIVE_DENIALS:
            reason += (
                f"（已连续 {self._consecutive[tool_name]} 次拒绝同一工具——"
                f"请改变策略，不要重复请求同一操作）"
            )
        if self.total_denials >= MAX_TOTAL_DENIALS:
            reason += "（总拒绝次数达到上限，建议终止任务）"
        return PermissionResult(Behavior.DENY, reason)


# ---------------------------------------------------------------------------
# 分层配置：项目级 settings.json（教学版 3 层中最低层）
# ---------------------------------------------------------------------------

SETTINGS_FILENAME = ".mini-agent/settings.json"


def parse_rule_entry(entry: str, behavior: Behavior, source: str) -> PermissionRule:
    """解析一条规则字符串："bash:git *" → (tool=bash, content=git *)。"""
    if ":" in entry:
        tool, _, content = entry.partition(":")
        return PermissionRule(tool=tool.strip(), behavior=behavior, content=content.strip(), source=source)
    return PermissionRule(tool=entry.strip(), behavior=behavior, source=source)


def load_settings_rules(root: Path) -> list[PermissionRule]:
    """加载项目级规则（.mini-agent/settings.json）。

    格式（对应 CC 的 permissions 配置段）：
    {
      "permissions": {
        "allow": ["read_file", "bash:git status"],
        "ask":   ["bash:rm *"],
        "deny":  ["bash:sudo *"]
      }
    }
    文件不存在 → 空规则；格式错误 → 警告并返回空（权限系统必须永不崩循环）。
    """
    path = root / SETTINGS_FILENAME
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        section = data.get("permissions", {})
    except (json.JSONDecodeError, OSError) as e:
        print(f"[permissions] 警告：忽略无法解析的 {path}: {e}", file=sys.stderr)
        return []
    rules: list[PermissionRule] = []
    for key, behavior in (("deny", Behavior.DENY), ("allow", Behavior.ALLOW), ("ask", Behavior.ASK)):
        for entry in section.get(key, []):
            rules.append(parse_rule_entry(str(entry), behavior, source="settings"))
    return rules
