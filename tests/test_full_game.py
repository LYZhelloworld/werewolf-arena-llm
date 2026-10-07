"""整局压力测试：跑大量对局，断言引擎不卡死、状态不自相矛盾。

随机策略会做出各种愚蠢的选择（自刀、毒队友、乱投票），正好用来压边界。
"""

from __future__ import annotations

import random

from config import BoardConfig
from engine.actions import Action, ActionType, LegalAction
from engine.engine import RuleEngine
from engine.state import GameState, Phase, Role
from tests.helpers import run

GAME_COUNT = 60


def random_policy(rng: random.Random):
    def policy(state: GameState, legal: list[LegalAction]) -> LegalAction:
        spec = legal[rng.randrange(len(legal))]
        if spec.allowed_targets:
            return spec.model_copy(update={"allowed_targets": [rng.choice(spec.allowed_targets)]})
        return spec

    return policy


def test_many_games_terminate_with_a_winner() -> None:
    for seed in range(GAME_COUNT):
        engine = RuleEngine(BoardConfig(), seed=seed)
        state, events = engine.new_game()
        state, more = run(engine, state, policy=random_policy(random.Random(seed * 7919)))
        events = events + more

        assert state.phase == Phase.GAME_OVER, f"seed={seed} 没有正常终局"
        assert state.winner is not None, f"seed={seed} 终局却没有胜方"
        assert state.day >= 1

        # 事件序号连续递增
        assert [e.seq for e in events] == list(range(1, len(events) + 1))

        # 死亡人数与存活人数对得上
        dead = [p for p in state.players.values() if not p.alive]
        assert len(dead) == 9 - len([p for p in state.players.values() if p.alive]), (
            f"seed={seed} 存活统计不一致"
        )

        # 终局必然满足某个胜负条件
        alive_wolves = [s for s, p in state.players.items() if p.role == Role.WEREWOLF and p.alive]
        alive_gods = [
            s
            for s, p in state.players.items()
            if p.role in (Role.SEER, Role.WITCH, Role.HUNTER) and p.alive
        ]
        alive_villagers = [
            s for s, p in state.players.items() if p.role == Role.VILLAGER and p.alive
        ]
        if state.winner.value == "good":
            assert not alive_wolves, f"seed={seed} 好人获胜但狼人未全灭"
        else:
            assert not alive_gods or not alive_villagers, f"seed={seed} 狼人获胜但未屠边"


def test_apply_does_not_mutate_the_input_state() -> None:
    """随手乱来的动作不会污染状态——引擎要么拒绝、要么本就合法。"""
    engine = RuleEngine(BoardConfig(), seed=99)
    state, _ = engine.new_game()
    state = run(engine, state, until=lambda s: s.phase == Phase.DAY_VOTE)[0]
    before = state.model_dump()
    try:
        engine.apply(state, Action(actor=1, type=ActionType.WITCH_POISON, target=2))
    except Exception as exc:  # noqa: BLE001
        assert "不能执行" in str(exc) or "无需行动" in str(exc)
    assert state.model_dump() == before  # 输入状态没有被改动
