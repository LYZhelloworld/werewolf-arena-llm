"""发牌。"""

from __future__ import annotations

from collections import Counter

from config import BoardConfig
from engine.engine import RuleEngine
from engine.events import Visibility
from engine.state import Phase, Role


def test_nine_seats_with_expected_role_counts() -> None:
    engine = RuleEngine(BoardConfig(), seed=7)
    state, _ = engine.new_game()
    assert sorted(state.players) == list(range(1, 10))

    counts = Counter(p.role for p in state.players.values())
    assert counts[Role.WEREWOLF] == 3
    assert counts[Role.SEER] == 1
    assert counts[Role.WITCH] == 1
    assert counts[Role.HUNTER] == 1
    assert counts[Role.VILLAGER] == 3


def test_same_seed_deals_identically() -> None:
    a, _ = RuleEngine(BoardConfig(), seed=123).new_game()
    b, _ = RuleEngine(BoardConfig(), seed=123).new_game()
    assert {s: p.role for s, p in a.players.items()} == {s: p.role for s, p in b.players.items()}


def test_different_seed_deals_differently() -> None:
    deals = {
        tuple(
            sorted(
                (s, p.role.value)
                for s, p in RuleEngine(BoardConfig(), seed=seed).new_game()[0].players.items()
            )
        )
        for seed in (1, 2, 3, 4, 5)
    }
    assert len(deals) > 1


def test_game_starts_in_wolf_chat_with_wolves_pending() -> None:
    engine = RuleEngine(BoardConfig(), seed=7)
    state, events = engine.new_game()
    assert state.phase == Phase.NIGHT_WOLF_CHAT
    assert state.pending_actors == sorted(
        s for s, p in state.players.items() if p.role == Role.WEREWOLF
    )

    # 底牌只有观众能看到
    assigned = next(e for e in events if e.type == "role_assigned")
    assert assigned.visibility == Visibility.SPECTATOR
