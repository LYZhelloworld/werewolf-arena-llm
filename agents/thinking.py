"""CoT 的捕获与回传。

捕获 / 回传 / 记录是三件独立的事（DESIGN.md §7.4），本模块只管前两件：

- **捕获**是无条件的：多源探测，抓不到就记 ``source="none"``，绝不静默留空。
- **回传**按 ``thinking.replay`` 决定：把 CoT 留在 assistant 消息的**原字段名**里，
  **绝不拼进 ``content``**（Qwen 官方明示会掉效果）。
"""

from __future__ import annotations

from typing import Any

from config import ThinkingConfig
from engine.events import Thinking

# auto 探测顺序：reasoning_content 是 DeepSeek / Qwen / Kimi 的事实标准
AUTO_FIELDS = ("reasoning_content", "reasoning", "reasoning_text")

__all__ = ["capture", "reasoning_field", "build_assistant_message"]


def reasoning_field(config: ThinkingConfig) -> str | None:
    """该模型实际使用的 CoT 字段名；``auto`` 时在首次响应里由 :func:`capture` 定。"""
    field = config.field.strip()
    return None if field == "auto" else field


def capture(raw: dict[str, Any], config: ThinkingConfig) -> Thinking:
    """从 assistant 消息的原始字典里抓 CoT。

    返回 :class:`Thinking`，``source`` 说明它来自哪里：
    ``reasoning_content``（或配置指定的字段名）/ ``content`` / ``none``。
    """
    explicit = reasoning_field(config)
    candidates: tuple[str, ...] = (explicit,) if explicit else AUTO_FIELDS
    for name in candidates:
        value = raw.get(name)
        if isinstance(value, str) and value.strip():
            return Thinking(text=value.strip(), source=name, replayed=False)

    content = raw.get("content")
    if isinstance(content, str) and content.strip():
        return Thinking(text=content.strip(), source="content", replayed=False)

    return Thinking(text=None, source="none", replayed=False)


def build_assistant_message(
    response_text: str | None,
    thinking: Thinking,
    tool_calls: list[dict[str, Any]],
    config: ThinkingConfig,
    field: str | None = None,
) -> dict[str, Any]:
    """把一次响应拼成要留在历史里的 assistant 消息。

    CoT 放在**独立字段**里（``reasoning_content`` 或配置指定的名字），
    与 ``content`` 平级，不拼接。``replay=False`` 时历史里不出现 CoT。
    """
    message: dict[str, Any] = {"role": "assistant"}
    if response_text:
        message["content"] = response_text
    if tool_calls:
        message["tool_calls"] = tool_calls

    if thinking.text and config.replay:
        name = field or reasoning_field(config) or thinking.source
        if name and name not in ("content", "none"):
            message[name] = thinking.text
            thinking.replayed = True
    return message
