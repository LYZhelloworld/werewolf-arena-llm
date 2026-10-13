"""动作定义。

LLM 通过工具产出 :class:`Action`，交给规则引擎结算。
引擎侧只认这一种输入，不关心它来自工具调用还是别的地方。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class ActionType(StrEnum):
    """动作类型。英文枚举值——它要出现在 tool schema 与事件流里，不能本地化。"""

    SPEAK = "speak"
    WOLF_VOTE_KILL = "wolf_vote_kill"
    ABSTAIN = "abstain"
    SEER_CHECK = "seer_check"
    WITCH_SAVE = "witch_save"
    WITCH_POISON = "witch_poison"
    WITCH_PASS = "witch_pass"
    HUNTER_SHOOT = "hunter_shoot"
    HUNTER_HOLD = "hunter_hold"
    VOTE = "vote"
    LAST_WORDS = "last_words"
    REFLECT = "reflect"
    # 系统内部动作：当前阶段无人需要行动时，由编排层驱动引擎自动结算推进
    ADVANCE = "advance"


class Action(BaseModel):
    """一次动作。``target`` / ``message`` 按 type 取用，未用到则为 None。"""

    actor: int | None = None  # 座位号；ADVANCE 为 None
    type: ActionType
    target: int | None = None
    message: str | None = None


class LegalAction(BaseModel):
    """当前某席位可以做的一个合法动作，含允许的目标集合。

    ``allowed_targets`` 为空表示该动作不需要目标（发言、弃权、放弃开枪等）。
    引擎只暴露非空目标集合的动作——没有任何可毒目标时就不会暴露 ``witch_poison``。
    """

    seat: int
    type: ActionType
    allowed_targets: list[int] = Field(default_factory=list)


class IllegalActionError(ValueError):
    """动作非法。``reason`` 会原样回给模型，让它在下一轮工具循环里重新选择。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason
