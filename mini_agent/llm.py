"""模型客户端工厂：DeepSeek 使用 OpenAI 兼容协议，直接用 openai SDK 接入。

对应 Claude Code 的 deps.callModel()（模型调用被抽象为可注入依赖，
方便测试时用假客户端替换 —— 见 tests/test_agent_loop.py）。
"""

from __future__ import annotations

from openai import OpenAI

from .config import Config

# 请求超时：单位秒。迭代 11（失败重试）会把它与退避策略一起完善。
DEFAULT_TIMEOUT_SECONDS = 60.0


def build_client(cfg: Config) -> OpenAI:
    """构造 DeepSeek 客户端（OpenAI 兼容）。"""
    return OpenAI(api_key=cfg.api_key, base_url=cfg.base_url, timeout=DEFAULT_TIMEOUT_SECONDS)
