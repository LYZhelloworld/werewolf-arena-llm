"""胜负判定：屠边。"""

from __future__ import annotations

from config import BoardConfig, WinConfig
from engine import rules
from engine.state import DeathCause, GameState, Winner
from tests.helpers import make_state


def eliminate(state: GameState, seats: list[int]) -> GameState:
    for seat in seats:
        rules.kill_player(state, seat, DeathCause.KILL)
    return state


def test_no_winner_at_start() -> None:
    assert rules.check_winner(make_state()) is None


def test_gods_eliminated_wolves_win() -> None:
    # 4 预言家 / 5 女巫 / 6 猎人
    assert rules.check_winner(eliminate(make_state(), [4, 5, 6])) == Winner.WOLF


def test_villagers_eliminated_wolves_win() -> None:
    assert rules.check_winner(eliminate(make_state(), [7, 8, 9])) == Winner.WOLF


def test_wolves_eliminated_good_win() -> None:
    assert rules.check_winner(eliminate(make_state(), [1, 2, 3])) == Winner.GOOD


def test_without_eliminate_one_faction_only_wolf_death_counts() -> None:
    board = BoardConfig(win=WinConfig(eliminate_one_faction=False))
    state = eliminate(make_state(board=board), [4, 5, 6])
    assert rules.check_winner(state) is None
    assert rules.check_winner(eliminate(state, [1, 2, 3])) == Winner.GOOD
