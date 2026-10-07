"""编排主循环：驱动规则引擎，把产出的事件交给下游（渲染 / 落盘）。

编排层是唯一同时认识引擎与策略的地方。它刻意**不知道**事件的含义，
只负责搬运——渲染和持久化都由回调决定，这样直播与回放才能共用同一条路径。
"""

from __future__ import annotations

from collections.abc import Callable

from pydantic import BaseModel

from engine.actions import Action, ActionType, IllegalActionError, LegalAction
from engine.engine import RuleEngine
from engine.events import Event
from engine.state import GameState, Phase
from orchestrator.policies import FirstLegalPolicy, Policy

DEFAULT_MAX_STEPS = 5000

# 一批事件 + 该批结算后的最新状态。视角过滤需要状态里的存活信息。
OnEvents = Callable[[list[Event], GameState], None] | None


class Rejection(BaseModel):
    """一次被拒绝的动作。M3 接 LLM 后，它会变成事件流里的 ``action_rejected``。"""

    seat: int
    action: str
    reason: str


class GameRunner:
    """把「引擎 + 策略」接到一起，跑完一局。"""

    def __init__(
        self,
        engine: RuleEngine,
        policy: Policy,
        on_events: OnEvents = None,
        fallback: Policy | None = None,
        max_steps: int = DEFAULT_MAX_STEPS,
    ) -> None:
        self.engine = engine
        self.policy = policy
        self.on_events = on_events
        self.fallback = fallback or FirstLegalPolicy()
        self.max_steps = max_steps
        self.rejections: list[Rejection] = []

    def run(self, state: GameState) -> tuple[GameState, list[Event]]:
        """跑到终局为止，返回终局状态与全部事件。"""
        collected: list[Event] = []

        for _ in range(self.max_steps):
            if state.phase == Phase.GAME_OVER:
                return state, collected

            legal = self.engine.legal_actions(state)
            state, batch = self._advance(state) if not legal else self._act_all(state, legal)
            collected.extend(batch)
            if self.on_events is not None:
                self.on_events(batch, state)

        raise RuntimeError(f"对局超过 {self.max_steps} 步仍未结束，疑似死循环：{state.phase}")

    # ------------------------------------------------------------------

    def _advance(self, state: GameState) -> tuple[GameState, list[Event]]:
        """当前阶段无人需要行动，交给引擎自动结算。"""
        return self.engine.apply(state, Action(actor=None, type=ActionType.ADVANCE))

    def _act_all(self, state: GameState, legal: list[LegalAction]) -> tuple[GameState, list[Event]]:
        """让本阶段所有待行动席位各出一个动作。

        关键点：所有席位都基于**同一份状态快照**做决策，先决策者的选择
        不会进入后决策者的上下文。同时投票 / 同时出刀的「互不看见」就是
        靠这一条保证的（AGENTS.md §9）。
        """
        snapshot = state
        events: list[Event] = []

        for seat in sorted({la.seat for la in legal}):
            mine = [la for la in legal if la.seat == seat]
            action = self.policy.decide(snapshot, mine)
            try:
                state, batch = self.engine.apply(state, action)
            except IllegalActionError as exc:
                self.rejections.append(
                    Rejection(seat=seat, action=action.type.value, reason=exc.reason)
                )
                state, batch = self.engine.apply(state, self.fallback.decide(snapshot, mine))
            events.extend(batch)

        return state, events
