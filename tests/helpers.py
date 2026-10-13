"""测试辅助：构造引擎状态、用确定性策略驱动引擎跑完整局。"""

from __future__ import annotations

from collections.abc import Callable

from config import BoardConfig
from engine import phases
from engine.actions import Action, ActionType, LegalAction
from engine.engine import RuleEngine
from engine.events import Event
from engine.state import GameState, Phase, Player, PotionState, Role

MESSAGE = "测试发言"


def canonical_roles() -> dict[int, Role]:
    """测试用的固定身份布局：1-3 狼、4 预言家、5 女巫、6 猎人、7-9 平民。"""
    return {
        1: Role.WEREWOLF,
        2: Role.WEREWOLF,
        3: Role.WEREWOLF,
        4: Role.SEER,
        5: Role.WITCH,
        6: Role.HUNTER,
        7: Role.VILLAGER,
        8: Role.VILLAGER,
        9: Role.VILLAGER,
    }


def make_state(
    roles: dict[int, Role] | None = None,
    board: BoardConfig | None = None,
    day: int = 1,
    phase: Phase = Phase.NIGHT_WOLF_VOTE,
) -> GameState:
    """直接构造一个引擎状态，跳过发牌。``pending_actors`` 需自行 setup。"""
    roles = roles or canonical_roles()
    board = board or BoardConfig()
    return GameState(
        game_id="test",
        day=day,
        phase=phase,
        players={seat: Player(seat=seat, role=role) for seat, role in roles.items()},
        board=board,
        potions=PotionState(
            antidote_left=board.witch.has_antidote,
            poison_left=board.witch.has_poison,
        ),
    )


def enter(state: GameState) -> GameState:
    """把状态切换到它自己声明的 phase 上（填充 pending_actors）。"""
    phases.setup_phase(state)
    return state


def default_policy(state: GameState, legal: list[LegalAction]) -> LegalAction:
    """确定性策略：优先做「需要选目标」的动作，目标取允许集合里的最小值。"""
    with_targets = [la for la in legal if la.allowed_targets]
    return with_targets[0] if with_targets else legal[0]


def run(
    engine: RuleEngine,
    state: GameState,
    policy: Callable[[GameState, list[LegalAction]], LegalAction] | None = None,
    until: Callable[[GameState], bool] | None = None,
    max_steps: int = 5000,
) -> tuple[GameState, list[Event]]:
    """驱动引擎直到终局或满足 ``until``。返回最终状态与全部事件。"""
    policy = policy or default_policy
    events: list[Event] = []
    for _ in range(max_steps):
        if until is not None and until(state):
            break
        if state.phase == Phase.GAME_OVER:
            break
        legal = engine.legal_actions(state)
        if not legal:
            state, batch = engine.apply(state, Action(actor=None, type=ActionType.ADVANCE))
            events.extend(batch)
            continue
        spec = policy(state, legal)
        target = min(spec.allowed_targets) if spec.allowed_targets else None
        state, batch = engine.apply(
            state,
            Action(actor=spec.seat, type=spec.type, target=target, message=MESSAGE),
        )
        events.extend(batch)
    return state, events


def types_of(events: list[Event]) -> list[str]:
    return [e.type for e in events]
