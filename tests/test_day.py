"""白天规则：投票、平票加时、遗言、猎人开枪。"""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from config import BoardConfig
from engine.actions import Action, ActionType, IllegalActionError
from engine.engine import RuleEngine
from engine.events import Visibility
from engine.state import DeathCause, GameState, Phase
from tests.helpers import enter, make_state

ENGINE = RuleEngine(BoardConfig())

TIE_VOTES = {1: 7, 2: 7, 3: 7, 4: 8, 5: 8, 6: 8, 7: 9, 8: 9, 9: 9}


def at_day_vote() -> GameState:
    return enter(make_state(phase=Phase.DAY_VOTE))


def cast(state: GameState, votes: Mapping[int, int | None]) -> GameState:
    for seat in sorted(votes):
        target = votes[seat]
        kind = ActionType.VOTE if target is not None else ActionType.ABSTAIN
        state, _ = ENGINE.apply(state, Action(actor=seat, type=kind, target=target))
    return state


def speak(state: GameState, seat: int) -> GameState:
    state, _ = ENGINE.apply(state, Action(actor=seat, type=ActionType.SPEAK, message="我发言"))
    return state


def test_majority_vote_lynches_and_grants_last_words() -> None:
    votes: dict[int, int | None] = {1: 2}
    votes.update({seat: 1 for seat in range(2, 10)})
    state = cast(at_day_vote(), votes)
    assert state.phase == Phase.LAST_WORDS
    assert not state.players[1].alive
    assert state.players[1].death_cause == DeathCause.LYNCH
    assert state.pending_last_words == [1]


def test_vote_is_secret_until_tallied() -> None:
    """投票期间互不看见：单张票只有观众能看到。"""
    state = at_day_vote()
    _, events = ENGINE.apply(state, Action(actor=1, type=ActionType.VOTE, target=2))
    cast_event = next(e for e in events if e.type == "vote_cast")
    assert cast_event.visibility == Visibility.SPECTATOR


def test_tie_goes_to_runoff_speech() -> None:
    state = cast(at_day_vote(), TIE_VOTES)
    assert state.phase == Phase.DAY_RUNOFF_SPEECH
    assert state.tie_candidates == [7, 8, 9]
    assert state.pending_actors == [7, 8, 9]


def test_runoff_vote_is_restricted_to_tied_candidates() -> None:
    state = cast(at_day_vote(), TIE_VOTES)
    for seat in (7, 8, 9):
        state = speak(state, seat)
    assert state.phase == Phase.DAY_RUNOFF_VOTE
    spec = next(la for la in ENGINE.legal_actions(state) if la.seat == 1)
    assert spec.allowed_targets == [7, 8, 9]


def test_second_tie_means_nobody_is_lynched() -> None:
    state = cast(at_day_vote(), TIE_VOTES)
    for seat in (7, 8, 9):
        state = speak(state, seat)
    state = cast(state, TIE_VOTES)
    assert state.day == 2
    assert state.phase == Phase.NIGHT_WOLF_CHAT
    assert all(p.alive for p in state.players.values())


def test_runoff_can_still_decide() -> None:
    state = cast(at_day_vote(), TIE_VOTES)
    for seat in (7, 8, 9):
        state = speak(state, seat)
    decided = {1: 8, 2: 8, 3: 8, 4: 8, 5: 8, 6: 7, 7: 8, 8: 8, 9: 8}
    state = cast(state, decided)
    assert not state.players[8].alive
    assert state.players[8].death_cause == DeathCause.LYNCH


def test_all_abstain_means_nobody_is_lynched() -> None:
    state = cast(at_day_vote(), {seat: None for seat in range(1, 10)})
    assert state.day == 2
    assert all(p.alive for p in state.players.values())


def test_lynched_hunter_shoots_after_last_words() -> None:
    votes: dict[int, int | None] = {6: 1}
    votes.update({seat: 6 for seat in range(1, 10) if seat != 6})
    state = cast(at_day_vote(), votes)
    assert state.phase == Phase.LAST_WORDS

    state, _ = ENGINE.apply(state, Action(actor=6, type=ActionType.LAST_WORDS, message="我是猎人"))
    assert state.phase == Phase.HUNTER_SHOOT
    assert state.pending_hunter == 6

    state, _ = ENGINE.apply(state, Action(actor=6, type=ActionType.HUNTER_SHOOT, target=1))
    assert not state.players[1].alive
    assert state.players[1].death_cause == DeathCause.SHOT
    assert state.day == 2


def test_hunter_can_hold_fire() -> None:
    votes: dict[int, int | None] = {6: 1}
    votes.update({seat: 6 for seat in range(1, 10) if seat != 6})
    state = cast(at_day_vote(), votes)
    state, _ = ENGINE.apply(state, Action(actor=6, type=ActionType.LAST_WORDS, message="我放弃"))
    state, _ = ENGINE.apply(state, Action(actor=6, type=ActionType.HUNTER_HOLD))
    assert state.players[1].alive
    assert state.day == 2


def test_speech_length_limit_is_enforced() -> None:
    state = enter(make_state(phase=Phase.DAY_DISCUSSION))
    with pytest.raises(IllegalActionError) as exc:
        ENGINE.apply(state, Action(actor=1, type=ActionType.SPEAK, message="字" * 301))
    assert "超过上限" in exc.value.reason


def test_empty_speech_is_rejected() -> None:
    state = enter(make_state(phase=Phase.DAY_DISCUSSION))
    with pytest.raises(IllegalActionError):
        ENGINE.apply(state, Action(actor=1, type=ActionType.SPEAK, message="   "))


def test_advance_is_rejected_while_actors_pending() -> None:
    state = at_day_vote()
    with pytest.raises(IllegalActionError):
        ENGINE.apply(state, Action(actor=None, type=ActionType.ADVANCE))
