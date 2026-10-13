"""信息隔离：可见性过滤与 CoT 不泄漏。

这是本项目最严重的潜在 bug 所在，测试必须钉死。
"""

from __future__ import annotations

import json

import pytest

from config import BoardConfig
from engine.engine import RuleEngine
from engine.events import Event, Thinking, Visibility
from engine.state import GameState, Role
from engine.visibility import filter_events_for, is_visible_to, view_for
from tests.helpers import run


def _event(visibility: Visibility, type_: str = "speech", actor: int = 1) -> Event:
    return Event(
        seq=1,
        game_id="g",
        day=1,
        phase="day_discussion",
        type=type_,
        visibility=visibility,
        actor=actor,
    )


# ---------------------------------------------------------------------------
# 逐条可见性
# ---------------------------------------------------------------------------


def test_public_is_visible_to_everyone_including_dead() -> None:
    event = _event(Visibility.PUBLIC)
    assert is_visible_to(event, seat=7, role=Role.VILLAGER, alive=True)
    assert is_visible_to(event, seat=7, role=Role.VILLAGER, alive=False)


def test_spectator_is_visible_to_no_agent() -> None:
    event = _event(Visibility.SPECTATOR)
    for role in Role:
        assert not is_visible_to(event, seat=1, role=role, alive=True)


def test_wolf_channel_only_for_live_wolves() -> None:
    event = _event(Visibility.WOLF, type_="wolf_chat")
    assert is_visible_to(event, seat=1, role=Role.WEREWOLF, alive=True)
    assert not is_visible_to(event, seat=7, role=Role.VILLAGER, alive=True)
    # 出局后私有频道永久关闭，包括他生前所属阵营的
    assert not is_visible_to(event, seat=1, role=Role.WEREWOLF, alive=False)


def test_self_channel_only_for_actor() -> None:
    event = _event(Visibility.SELF, type_="memory_write", actor=3)
    assert is_visible_to(event, seat=3, role=Role.SEER, alive=True)
    assert not is_visible_to(event, seat=4, role=Role.SEER, alive=True)


# ---------------------------------------------------------------------------
# 整局过滤
# ---------------------------------------------------------------------------


def _full_game(seed: int) -> tuple[RuleEngine, GameState, list[Event]]:
    engine = RuleEngine(BoardConfig(), seed=seed)
    state, events = engine.new_game()
    state, more = run(engine, state)
    return engine, state, events + more


def test_good_players_never_see_wolf_chat() -> None:
    _, state, events = _full_game(seed=42)
    for seat, player in state.players.items():
        visible = view_for(state, events, seat)
        if player.role == Role.WEREWOLF:
            continue
        assert [e for e in visible if e.type == "wolf_chat"] == []


def test_non_wolves_never_see_role_visibility_events() -> None:
    _, state, events = _full_game(seed=3)
    for seat, player in state.players.items():
        visible = view_for(state, events, seat)
        assert [e for e in visible if e.visibility == Visibility.SPECTATOR] == []
        if player.role != Role.WEREWOLF:
            assert [e for e in visible if e.visibility == Visibility.WOLF] == []
        if player.role != Role.SEER:
            assert [e for e in visible if e.visibility == Visibility.SEER] == []
        if player.role != Role.WITCH:
            assert [e for e in visible if e.visibility == Visibility.WITCH] == []


def test_wolves_see_wolf_chat_but_not_seer_or_witch() -> None:
    # 狼人全灭的对局里没有存活狼可测，挑一个狼人活到最后的 seed
    for seed in range(1, 30):
        _, state, events = _full_game(seed)
        wolves = [s for s, p in state.players.items() if p.role == Role.WEREWOLF and p.alive]
        if wolves:
            wolf_seat = wolves[0]
            break
    else:
        pytest.fail("找不到狼人存活到终局的对局")

    visible = view_for(state, events, wolf_seat)
    assert any(e.type == "wolf_chat" for e in visible)
    assert [e for e in visible if e.visibility in (Visibility.SEER, Visibility.WITCH)] == []


# ---------------------------------------------------------------------------
# CoT 不泄漏
# ---------------------------------------------------------------------------


def test_thinking_is_stripped_from_every_agent_view() -> None:
    event = _event(Visibility.PUBLIC)
    event = event.model_copy(
        update={"thinking": Thinking(text="我的秘密思考", source="reasoning_content")}
    )
    visible = filter_events_for([event], seat=2, role=Role.VILLAGER, alive=True)
    assert visible[0].thinking is None


def test_cot_never_appears_in_any_agent_context() -> None:
    """给每条事件都挂上带标记的 CoT，断言任何 agent 的上下文里都搜不到。"""
    _, state, events = _full_game(seed=11)
    marked = [
        e.model_copy(
            update={"thinking": Thinking(text=f"秘密思考-{e.seq}", source="reasoning_content")}
        )
        for e in events
    ]
    assert marked, "事件流不应为空"

    for seat, player in state.players.items():
        visible = filter_events_for(marked, seat, player.role, player.alive)
        blob = json.dumps([e.model_dump(mode="json") for e in visible], ensure_ascii=False)
        assert "秘密思考" not in blob, f"{seat} 号的上下文里出现了他人的 CoT"
