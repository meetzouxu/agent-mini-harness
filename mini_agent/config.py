"""配置加载：从环境变量与 .env 文件读取运行配置。

密钥不进代码、不进 git —— 统一放在 .env（已被 .gitignore 排除）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

# 默认加载「当前工作目录」下的 .env；迭代 1 的约定是项目根目录运行
load_dotenv()


@dataclass
class Config:
    """Agent 运行配置。"""

    api_key: str
    base_url: str
    model: str
    max_turns: int


def load_config() -> Config:
    """读取环境变量构造配置，缺关键项时给出可操作的报错。"""
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key or api_key.startswith("sk-请替换"):
        raise SystemExit(
            "未检测到有效的 DEEPSEEK_API_KEY。\n"
            "请执行: cp .env.example .env 然后编辑 .env 填入真实 key。\n"
            "(.env 已被 .gitignore 排除，不会进入版本库)"
        )
    return Config(
        api_key=api_key,
        base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").strip(),
        model=os.environ.get("MINI_AGENT_MODEL", "deepseek-chat").strip(),
        max_turns=int(os.environ.get("MINI_AGENT_MAX_TURNS", "15")),
    )
