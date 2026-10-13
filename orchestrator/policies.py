"""决策策略：给定当前阶段的可选动作，产出 :class:`Action`。

M2 阶段由 :class:`RandomPolicy` 顶替 LLM，目的是在接入模型之前
先把规则引擎与编排层端到端跑通。M3 的 AgentRuntime 实现同一个
:class:`Policy` 协议，编排层不需要改动。

所有发言模板都是中文——事件流里的人工可读文本一律中文（AGENTS.md §2）。
"""

from __future__ import annotations

import random
from typing import Protocol

from engine.actions import Action, ActionType, LegalAction
from engine.engine import SPEAKING_TYPES
from engine.state import GameState, Phase

# 狼人夜间私有协商的阶段，发言内容要换成狼队口吻
WOLF_CHAT_PHASES = frozenset({Phase.NIGHT_WOLF_CHAT.value, Phase.NIGHT_WOLF_RUNOFF_SPEECH.value})

DAY_SPEECHES = (
    "我先听一轮，暂时没有明确的怀疑对象。",
    "{a} 号刚才的表态有点急，我先记一笔。",
    "我是好人，昨晚没有任何信息，这轮先跟票。",
    "{a} 号和 {b} 号互相保，这个组合值得留意。",
    "信息太少，我建议先别乱投，再看一轮。",
    "{a} 号那段逻辑我认可，我倾向跟着他走。",
    "我发言简短一点：我昨晚没有动作，身份不怕验。",
    "{a} 号一直在带节奏，但说不出理由，我怀疑他。",
)

WOLF_CHATS = (
    "今晚刀 {a} 号，他发言太正了，留着危险。",
    "我同意刀 {a} 号，他盯我盯得最紧。",
    "建议刀 {a} 号，另外白天大家别互保，容易暴露。",
    "我没什么意见，跟 {a} 号吧，尽快统一。",
    "刀 {a} 号之后我明天装平民，你们别接我的话。",
)

LAST_WORDS = (
    "我走了，{a} 号大概率是狼，好人加油。",
    "可惜了，我真不是狼，注意 {a} 号。",
    "好人加油，别被带节奏，认准 {a} 号。",
)

REFLECTS = (
    "这局我前半段判断失误，被带偏了。",
    "{a} 号那一轮发言其实已经暴露了，可惜我后来才反应过来。",
    "整体打得还行，下次我会更早亮身份。",
    "我以为自己藏得很好，结果还是被 {a} 号看穿了。",
)


class Policy(Protocol):
    """决策方协议。M3 的 AgentRuntime 也实现它。"""

    def decide(self, state: GameState, legal: list[LegalAction]) -> Action:
        """从 ``legal`` 中为某个席位挑一个动作。

        ``legal`` 只包含**一个席位**的可选动作，由编排层按席位分组传入。
        """
        ...


def _fill(template: str, seats: list[int]) -> str:
    """把模板里的 ``{a}`` / ``{b}`` 换成座位号。座位不够时用「某人」占位。"""
    out = template
    for index, key in enumerate(("a", "b")):
        value = str(seats[index]) if index < len(seats) else "某人"
        out = out.replace("{" + key + "}", value)
    return out


class RandomPolicy:
    """随机策略：从合法动作里挑一个，发言内容从中文模板里挑。

    ``prefer_target`` 控制「优先选需要指定目标的动作」的概率。
    不给它偏好的话，弃权会被抽中得太频繁，对局会变得又长又无信息。
    """

    def __init__(self, seed: int | None = None, prefer_target: float = 0.8) -> None:
        self.rng = random.Random(seed)
        self.prefer_target = prefer_target

    def decide(self, state: GameState, legal: list[LegalAction]) -> Action:
        spec = self._pick(legal)
        target = self.rng.choice(spec.allowed_targets) if spec.allowed_targets else None
        message = self._speech(state, spec) if spec.type in SPEAKING_TYPES else None
        return Action(actor=spec.seat, type=spec.type, target=target, message=message)

    def _pick(self, legal: list[LegalAction]) -> LegalAction:
        with_targets = [la for la in legal if la.allowed_targets]
        if with_targets and self.rng.random() < self.prefer_target:
            return self.rng.choice(with_targets)
        return self.rng.choice(legal)

    def _speech(self, state: GameState, spec: LegalAction) -> str:
        pool = [s for s, p in state.players.items() if p.alive and s != spec.seat]
        sample = self.rng.sample(pool, min(2, len(pool))) if pool else []

        templates: tuple[str, ...]
        if spec.type == ActionType.SPEAK:
            templates = WOLF_CHATS if state.phase.value in WOLF_CHAT_PHASES else DAY_SPEECHES
        elif spec.type == ActionType.LAST_WORDS:
            templates = LAST_WORDS
        else:
            templates = REFLECTS
        return _fill(self.rng.choice(templates), sample)


def deterministic_action(legal: list[LegalAction]) -> Action:
    """确定性兜底动作：取第一个需要目标的动作，目标取最小座位号。

    不依赖任何状态，便于在各处（编排层、LLM 策略的兜底分支）复用。
    """
    spec = next((la for la in legal if la.allowed_targets), legal[0])
    target = min(spec.allowed_targets) if spec.allowed_targets else None
    message = "我没什么可说的。" if spec.type in SPEAKING_TYPES else None
    return Action(actor=spec.seat, type=spec.type, target=target, message=message)


class FirstLegalPolicy:
    """确定性策略：永远取第一个动作、最小目标。

    用作兜底——决策方给出非法动作时（M3 里是模型乱来），
    编排层改用它保证流程能继续走下去。
    """

    def decide(self, state: GameState, legal: list[LegalAction]) -> Action:
        return deterministic_action(legal)
