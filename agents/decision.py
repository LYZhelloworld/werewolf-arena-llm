"""跨层的数据契约：一次决策的完整结果。

编排层需要的不只是 :class:`Action`——还有 CoT、token 用量、是否走了兜底、
以及记忆读写这些副作用事件（DESIGN.md §5.3.1）。它们都装在这里。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from agents.loop import PendingEvent
from engine.actions import Action
from engine.events import LlmMeta, Thinking

__all__ = ["Decision", "LlmMeta"]


class Decision(BaseModel):
    """一个席位一回合的完整产出。"""

    action: Action
    thinking: Thinking | None = None
    llm: LlmMeta | None = None
    side_events: list[PendingEvent] = Field(default_factory=list)
