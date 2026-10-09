"""夜间规则：狼人出刀、女巫细则、预言家查验、猎人夜间开枪。"""

from __future__ import annotations

import pytest

from config import BoardConfig
from engine.actions import Action, ActionType, IllegalActionError
from engine.engine import RuleEngine
from engine.events import Event, Visibility
from engine.state import DeathCause, Faction, GameState, Phase
from engine.visibility import is_visible_to, view_for

ENGINE = RuleEngine(BoardConfig())


def at_wolf_vote(day: int = 1, board: BoardConfig | None = None) -> GameState:
    from tests.helpers import enter, make_state

    return enter(make_state(day=day, phase=Phase.NIGHT_WOLF_VOTE, board=board))


def wolf_vote(state: GameState, votes: dict[int, int]) -> GameState:
    for seat, target in votes.items():
        state, _ = ENGINE.apply(
            state, Action(actor=seat, type=ActionType.WOLF_VOTE_KILL, target=target)
        )
    return state


def seer_check(state: GameState, target: int) -> GameState:
    state, _ = ENGINE.apply(state, Action(actor=4, type=ActionType.SEER_CHECK, target=target))
    return state


def witch_act(state: GameState, kind: ActionType, target: int | None = None) -> GameState:
    state, _ = ENGINE.apply(state, Action(actor=5, type=kind, target=target))
    return state


def unanimous(target: int) -> dict[int, int]:
    return {1: target, 2: target, 3: target}


# ---------------------------------------------------------------------------
# 狼人
# ---------------------------------------------------------------------------


def test_wolves_can_not_target_teammates_or_self() -> None:
    state = at_wolf_vote()
    spec = next(
        la
        for la in ENGINE.legal_actions(state)
        if la.seat == 1 and la.type == ActionType.WOLF_VOTE_KILL
    )
    assert spec.allowed_targets == [4, 5, 6, 7, 8, 9]


def test_unanimous_kill_sets_target() -> None:
    state = wolf_vote(at_wolf_vote(), unanimous(7))
    assert state.phase == Phase.NIGHT_SEER
    assert state.night.kill_target == 7


def test_all_abstain_is_empty_kill() -> None:
    state = at_wolf_vote()
    for seat in (1, 2, 3):
        state, _ = ENGINE.apply(state, Action(actor=seat, type=ActionType.ABSTAIN))
    assert state.night.kill_target is None


def test_kill_tie_goes_to_runoff_then_lowest_seat() -> None:
    state = wolf_vote(at_wolf_vote(), {1: 7, 2: 8, 3: 9})
    assert state.phase == Phase.NIGHT_WOLF_RUNOFF_SPEECH
    assert state.tie_candidates == [7, 8, 9]

    for seat in (1, 2, 3):
        state, _ = ENGINE.apply(
            state, Action(actor=seat, type=ActionType.SPEAK, message="再商量一下")
        )
    assert state.phase == Phase.NIGHT_WOLF_RUNOFF_VOTE

    # 重投只能选平票候选
    spec = next(
        la
        for la in ENGINE.legal_actions(state)
        if la.seat == 1 and la.type == ActionType.WOLF_VOTE_KILL
    )
    assert spec.allowed_targets == [7, 8, 9]

    state = wolf_vote(state, {1: 9, 2: 8, 3: 7})  # 再次平票
    assert state.night.kill_target == 7  # 平票候选中座位号最小者
    assert state.phase == Phase.NIGHT_SEER


# ---------------------------------------------------------------------------
# 预言家
# ---------------------------------------------------------------------------


def test_seer_check_reveals_faction() -> None:
    state = wolf_vote(at_wolf_vote(), unanimous(7))
    assert state.phase == Phase.NIGHT_SEER
    state = seer_check(state, target=1)
    assert state.night.seer_result == Faction.WOLF
    assert state.phase == Phase.NIGHT_WITCH


def test_seer_can_not_check_self() -> None:
    state = wolf_vote(at_wolf_vote(), unanimous(7))
    with pytest.raises(IllegalActionError):
        ENGINE.apply(state, Action(actor=4, type=ActionType.SEER_CHECK, target=4))


def test_seer_can_not_check_dead() -> None:
    state = wolf_vote(at_wolf_vote(), unanimous(7))
    state = seer_check(state, target=1)
    state = witch_act(state, ActionType.WITCH_PASS)
    assert not state.players[7].alive
    # 第二天夜里，7 号已出局，不能再查验
    from tests.helpers import enter

    state.phase = Phase.NIGHT_SEER
    enter(state)
    with pytest.raises(IllegalActionError):
        ENGINE.apply(state, Action(actor=4, type=ActionType.SEER_CHECK, target=7))


# ---------------------------------------------------------------------------
# 女巫
# ---------------------------------------------------------------------------


def test_witch_saves_kill_target() -> None:
    state = wolf_vote(at_wolf_vote(), unanimous(7))
    state = seer_check(state, target=1)
    state = witch_act(state, ActionType.WITCH_SAVE)
    assert state.players[7].alive
    assert state.phase == Phase.DAY_DISCUSSION


def _drive(events: list[Event], state: GameState, action: Action) -> GameState:
    state, batch = ENGINE.apply(state, action)
    events.extend(batch)
    return state


def test_witch_is_told_kill_target_before_her_turn() -> None:
    """设计 §4.5：女巫每晚得知刀口，且必须在她行动（NIGHT_WITCH）之前收到。

    回归：wolf_kill 曾经只在 NIGHT_RESOLVE 才以 private:wolf 发出，女巫根本
    看不到被袭击的是谁，只能盲过——模型也确实在真实对局里报告「我不知道狼人袭击了谁」。
    """
    state = at_wolf_vote()
    last: list[Event] = []
    for seat, target in unanimous(7).items():
        state, last = ENGINE.apply(
            state, Action(actor=seat, type=ActionType.WOLF_VOTE_KILL, target=target)
        )
    # 狼票结算后立刻进入预言家阶段，但刀口通知应在此时（女巫行动前）发出
    assert state.phase == Phase.NIGHT_SEER
    witch_kills = [e for e in last if e.type == "wolf_kill" and e.visibility == Visibility.WITCH]
    assert witch_kills, "女巫应在狼票结算后立即收到刀口通知"
    assert witch_kills[0].payload["seat"] == 7

    # 女巫（5 号）可见，狼人（1 号）不可见（那是给女巫的专属频道）
    witch = state.players[5]
    assert is_visible_to(witch_kills[0], 5, witch.role, witch.alive)
    assert not is_visible_to(witch_kills[0], 1, state.players[1].role, True)


def test_witch_sees_kill_target_in_her_view() -> None:
    """女巫进入 NIGHT_WITCH 时，她能看到的事件里必须包含今夜刀口。"""
    state = at_wolf_vote()
    events: list[Event] = []
    for seat, target in unanimous(7).items():
        state = _drive(
            events, state, Action(actor=seat, type=ActionType.WOLF_VOTE_KILL, target=target)
        )
    state = _drive(events, state, Action(actor=4, type=ActionType.SEER_CHECK, target=1))
    assert state.phase == Phase.NIGHT_WITCH

    witch_view = view_for(state, events, 5)
    witch_kills = [e for e in witch_view if e.type == "wolf_kill"]
    assert len(witch_kills) == 1
    assert witch_kills[0].payload["seat"] == 7


def test_witch_told_when_night_is_empty() -> None:
    """空刀时女巫也要被告知无人被袭击（解药无从使用，§4.5）。"""
    state = at_wolf_vote()
    events: list[Event] = []
    for seat in (1, 2, 3):
        state = _drive(events, state, Action(actor=seat, type=ActionType.ABSTAIN))
    assert state.night.kill_target is None
    witch_kills = [e for e in events if e.type == "wolf_kill" and e.visibility == Visibility.WITCH]
    assert witch_kills, "空刀也要通知女巫"
    assert witch_kills[0].payload["seat"] is None


def test_witch_can_not_save_when_no_kill_target() -> None:
    state = at_wolf_vote()
    for seat in (1, 2, 3):
        state, _ = ENGINE.apply(state, Action(actor=seat, type=ActionType.ABSTAIN))
    state = seer_check(state, target=1)
    spec = [la.type for la in ENGINE.legal_actions(state) if la.seat == 5]
    assert ActionType.WITCH_SAVE not in spec


def test_self_save_allowed_on_first_night_only() -> None:
    # 首夜：刀口是女巫本人（5 号），可以自救
    state = wolf_vote(at_wolf_vote(day=1), unanimous(5))
    state = seer_check(state, target=1)
    state = witch_act(state, ActionType.WITCH_SAVE)
    assert state.players[5].alive

    # 第二夜：同样情形不再允许自救
    state = wolf_vote(at_wolf_vote(day=2), unanimous(5))
    state = seer_check(state, target=1)
    with pytest.raises(IllegalActionError):
        ENGINE.apply(state, Action(actor=5, type=ActionType.WITCH_SAVE))


def test_can_not_poison_self() -> None:
    state = wolf_vote(at_wolf_vote(), unanimous(7))
    state = seer_check(state, target=1)
    spec = next(la for la in ENGINE.legal_actions(state) if la.type == ActionType.WITCH_POISON)
    assert 5 not in spec.allowed_targets


def test_can_not_use_both_potions_same_night() -> None:
    state = wolf_vote(at_wolf_vote(), unanimous(7))
    state = seer_check(state, target=1)
    state = witch_act(state, ActionType.WITCH_POISON, target=8)
    with pytest.raises(IllegalActionError):
        ENGINE.apply(state, Action(actor=5, type=ActionType.WITCH_SAVE))


def test_potion_is_consumed_across_nights() -> None:
    state = wolf_vote(at_wolf_vote(), unanimous(7))
    state = seer_check(state, target=1)
    state = witch_act(state, ActionType.WITCH_SAVE)
    assert state.potions.antidote_left is False


# ---------------------------------------------------------------------------
# 猎人
# ---------------------------------------------------------------------------


def test_hunter_killed_at_night_triggers_night_shot() -> None:
    state = wolf_vote(at_wolf_vote(), unanimous(6))
    state = seer_check(state, target=1)
    state = witch_act(state, ActionType.WITCH_PASS)
    assert not state.players[6].alive
    assert state.players[6].death_cause == DeathCause.KILL
    assert state.phase == Phase.NIGHT_HUNTER_SHOOT
    assert state.pending_hunter == 6


def test_hunter_poisoned_at_night_can_not_shoot() -> None:
    state = wolf_vote(at_wolf_vote(), unanimous(7))
    state = seer_check(state, target=1)
    state = witch_act(state, ActionType.WITCH_POISON, target=6)
    assert state.players[6].death_cause == DeathCause.POISON
    assert state.phase == Phase.DAY_DISCUSSION  # 没有触发开枪阶段


def test_night_shot_victim_is_announced_next_morning() -> None:
    state = wolf_vote(at_wolf_vote(), unanimous(6))
    state = seer_check(state, target=1)
    state = witch_act(state, ActionType.WITCH_PASS)
    state, events = ENGINE.apply(state, Action(actor=6, type=ActionType.HUNTER_SHOOT, target=9))
    assert not state.players[9].alive
    announce = next(e for e in events if e.type == "night_deaths_announced")
    assert announce.payload["seats"] == [6, 9]


def test_night_death_has_no_last_words() -> None:
    state = wolf_vote(at_wolf_vote(), unanimous(7))
    state = seer_check(state, target=1)
    state = witch_act(state, ActionType.WITCH_PASS)
    assert not state.players[7].alive
    assert state.phase == Phase.DAY_DISCUSSION  # 直接进入讨论，没有遗言阶段
