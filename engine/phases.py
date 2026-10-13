"""阶段状态机：进入阶段时的初始化，以及阶段结算与切换。

两个函数都只做「改状态 + 发事件」，不碰 LLM、不碰 I/O。
``resolve_phase`` 结算当前阶段并返回下一阶段；无人行动的阶段在 :func:`advance`
的循环里会被连续结算掉，不会卡住。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from engine import rules
from engine.events import Event, RenderText, Visibility
from engine.state import (
    Death,
    DeathCause,
    GameState,
    NightState,
    Phase,
    Role,
    Winner,
    phase_label,
)

MAX_ADVANCE_STEPS = 64


def _counts_text(counts: Mapping[int, int]) -> str:
    """计票明细 → 「3 号：2 票、5 号：1 票」。空计票说明全员弃权。"""
    if not counts:
        return "全部弃权"
    return "、".join(f"{seat} 号：{count} 票" for seat, count in sorted(counts.items()))


def _seats_text(seats: list[int]) -> str:
    return "、".join(f"{s} 号" for s in seats)


class EmitFn(Protocol):
    """发事件回调。由 RuleEngine 提供，负责分配 seq 并追加到事件列表。"""

    def __call__(
        self,
        type_: str,
        visibility: Visibility,
        actor: int | None = None,
        payload: dict | None = None,
        render: RenderText | None = None,
    ) -> Event: ...


def advance(state: GameState, emit: EmitFn) -> None:
    """连续结算直到某个阶段需要 LLM 行动，或游戏结束。"""
    for _ in range(MAX_ADVANCE_STEPS):
        if state.phase == Phase.GAME_OVER:
            return
        nxt = resolve_phase(state, emit)
        state.phase = nxt
        setup_phase(state)
        emit(
            "phase_enter",
            Visibility.PUBLIC,
            payload={"phase": state.phase.value, "day": state.day},
            render=RenderText(text=f"进入阶段：{phase_label(state.phase)}"),
        )
        if state.pending_actors:
            return
    raise RuntimeError(f"阶段推进超过 {MAX_ADVANCE_STEPS} 步仍未停下，疑似死循环：{state.phase}")


def setup_phase(state: GameState) -> None:
    """进入某阶段时初始化待行动席位与轮次。"""
    phase = state.phase
    if phase in (
        Phase.NIGHT_WOLF_CHAT,
        Phase.NIGHT_WOLF_RUNOFF_SPEECH,
        Phase.DAY_DISCUSSION,
        Phase.DAY_RUNOFF_SPEECH,
    ):
        state.round_index = 0
    if phase in (
        Phase.NIGHT_WOLF_VOTE,
        Phase.NIGHT_WOLF_RUNOFF_VOTE,
        Phase.DAY_VOTE,
        Phase.DAY_RUNOFF_VOTE,
    ):
        state.votes = {}
    state.pending_actors = actors_for(state, phase)


def actors_for(state: GameState, phase: Phase) -> list[int]:
    """某阶段需要哪些座位行动。返回空列表表示该阶段无需 LLM 行动。"""
    b = state.board

    if phase == Phase.NIGHT_WOLF_CHAT:
        return rules.alive_wolves(state) if b.werewolf.chat_rounds > 0 else []
    if phase in (
        Phase.NIGHT_WOLF_VOTE,
        Phase.NIGHT_WOLF_RUNOFF_SPEECH,
        Phase.NIGHT_WOLF_RUNOFF_VOTE,
    ):
        return rules.alive_wolves(state)
    if phase == Phase.NIGHT_SEER:
        if not b.seer.check_every_night:
            return []
        seat = rules.seat_of_role(state, Role.SEER)
        return [seat] if seat is not None and state.players[seat].alive else []
    if phase == Phase.NIGHT_WITCH:
        seat = rules.seat_of_role(state, Role.WITCH)
        if seat is None or not state.players[seat].alive:
            return []
        # 两瓶药都用光了就没她的事了
        if not (state.potions.antidote_left or state.potions.poison_left):
            return []
        return [seat]
    if phase in (Phase.NIGHT_RESOLVE, Phase.DAY_ANNOUNCE, Phase.DAY_RESOLVE, Phase.GAME_OVER):
        return []
    if phase in (Phase.NIGHT_HUNTER_SHOOT, Phase.HUNTER_SHOOT):
        return [state.pending_hunter] if state.pending_hunter is not None else []
    if phase == Phase.DAY_DISCUSSION:
        return rules.alive_seats(state) if b.day.discussion_rounds > 0 else []
    if phase in (Phase.DAY_VOTE, Phase.DAY_RUNOFF_VOTE):
        return rules.alive_seats(state)
    if phase == Phase.DAY_RUNOFF_SPEECH:
        return [s for s in state.tie_candidates if state.players[s].alive]
    if phase == Phase.LAST_WORDS:
        return list(state.pending_last_words)
    if phase == Phase.POSTGAME:
        return sorted(state.players)
    return []


def resolve_phase(state: GameState, emit: EmitFn) -> Phase:
    """结算当前阶段，返回下一阶段。"""
    phase = state.phase

    if phase == Phase.NIGHT_WOLF_CHAT:
        return Phase.NIGHT_WOLF_VOTE
    if phase == Phase.NIGHT_WOLF_VOTE:
        return _resolve_wolf_vote(state, emit, runoff=False)
    if phase == Phase.NIGHT_WOLF_RUNOFF_SPEECH:
        return Phase.NIGHT_WOLF_RUNOFF_VOTE
    if phase == Phase.NIGHT_WOLF_RUNOFF_VOTE:
        return _resolve_wolf_vote(state, emit, runoff=True)
    if phase == Phase.NIGHT_SEER:
        return Phase.NIGHT_WITCH
    if phase == Phase.NIGHT_WITCH:
        return Phase.NIGHT_RESOLVE
    if phase == Phase.NIGHT_RESOLVE:
        return _resolve_night(state, emit)
    if phase == Phase.NIGHT_HUNTER_SHOOT:
        _apply_hunter_shot(state, emit, night=True)
        return _check_win_or(state, emit, Phase.DAY_ANNOUNCE)
    if phase == Phase.DAY_ANNOUNCE:
        return _resolve_day_announce(state, emit)
    if phase == Phase.DAY_DISCUSSION:
        return Phase.DAY_VOTE
    if phase == Phase.DAY_VOTE:
        return _resolve_day_vote(state, emit, runoff=False)
    if phase == Phase.DAY_RUNOFF_SPEECH:
        return Phase.DAY_RUNOFF_VOTE
    if phase == Phase.DAY_RUNOFF_VOTE:
        return _resolve_day_vote(state, emit, runoff=True)
    if phase == Phase.DAY_RESOLVE:
        return _resolve_day(state, emit)
    if phase == Phase.LAST_WORDS:
        if state.pending_last_words:
            return Phase.LAST_WORDS
        if state.pending_hunter is not None:
            return Phase.HUNTER_SHOOT
        return _end_day(state, emit)
    if phase == Phase.HUNTER_SHOOT:
        _apply_hunter_shot(state, emit, night=False)
        return _end_day(state, emit)
    if phase == Phase.POSTGAME:
        return Phase.GAME_OVER
    return Phase.GAME_OVER


# ---------------------------------------------------------------------------
# 夜间
# ---------------------------------------------------------------------------


def _emit_witch_kill_target(state: GameState, emit: EmitFn) -> None:
    """把今夜刀口告诉女巫（设计 §4.5：女巫每晚得知刀口，才能决定救不救）。

    必须在女巫行动（NIGHT_WITCH）之前发出，可见性 private:witch。
    此时 save 尚未发生，故不带 saved 字段；女巫只得知「被袭击的是几号」。
    """
    target = state.night.kill_target
    if target is None:
        emit(
            "wolf_kill",
            Visibility.WITCH,
            payload={"seat": None},
            render=RenderText(text="今夜空刀（狼人未选出刀口），无人被袭击。"),
        )
    else:
        emit(
            "wolf_kill",
            Visibility.WITCH,
            payload={"seat": target},
            render=RenderText(text=f"今夜刀口：{target} 号"),
        )


def _resolve_wolf_vote(state: GameState, emit: EmitFn, runoff: bool) -> Phase:
    counts = rules.tally(state.votes)
    tops = rules.top_candidates(counts)
    payload_counts = {str(k): v for k, v in counts.items()}

    if not tops:
        state.night.kill_target = None
        emit(
            "vote_result",
            Visibility.WOLF,
            payload={"target": None, "counts": payload_counts, "tied": []},
            render=RenderText(text=f"狼人没有选出刀口，今夜不刀人（{_counts_text(counts)}）"),
        )
        _emit_witch_kill_target(state, emit)
        return Phase.NIGHT_SEER

    if len(tops) == 1:
        state.night.kill_target = tops[0]
        emit(
            "vote_result",
            Visibility.WOLF,
            payload={"target": tops[0], "counts": payload_counts, "tied": []},
            render=RenderText(
                text=f"狼人决定刀 {tops[0]} 号（{_counts_text(counts)}）",
            ),
        )
        _emit_witch_kill_target(state, emit)
        return Phase.NIGHT_SEER

    emit(
        "vote_tie",
        Visibility.WOLF,
        payload={"tied": tops, "counts": payload_counts},
        render=RenderText(
            text=f"狼人出刀平票：{_seats_text(tops)}（各 {counts[tops[0]]} 票），进入加时协商",
        ),
    )

    if runoff:
        # 二次平票：确定性兜底，不再追加第三轮
        target = min(tops) if state.board.werewolf.tie_break_by_lowest_seat else None
        state.night.kill_target = target
        text = (
            f"狼人两次平票，板子未开座位号兜底，今夜不刀人（{_counts_text(counts)}）"
            if target is None
            else f"狼人两次平票，按座位号最小定为 {target} 号（{_counts_text(counts)}）"
        )
        emit(
            "vote_result",
            Visibility.WOLF,
            payload={
                "target": target,
                "counts": payload_counts,
                "tied": tops,
                "tie_broken": True,
            },
            render=RenderText(text=text),
        )
        _emit_witch_kill_target(state, emit)
        return Phase.NIGHT_SEER

    state.tie_candidates = tops
    return Phase.NIGHT_WOLF_RUNOFF_SPEECH


def _resolve_night(state: GameState, emit: EmitFn) -> Phase:
    night = state.night
    planned: list[Death] = []
    if night.kill_target is not None and not night.saved:
        planned.append(Death(seat=night.kill_target, cause=DeathCause.KILL))
    if night.poison_target is not None:
        planned.append(Death(seat=night.poison_target, cause=DeathCause.POISON))

    applied: list[Death] = []
    for death in planned:
        if not state.players[death.seat].alive:
            continue
        rules.kill_player(state, death.seat, death.cause)
        applied.append(death)
        emit(
            "death",
            Visibility.SPECTATOR,  # 夜里没人该知道，白天才公告
            actor=None,
            payload={"seat": death.seat, "cause": death.cause.value},
            render=RenderText(text=f"{death.seat} 号夜间出局"),
        )
    night.deaths = applied
    state.pending_deaths = applied

    if night.kill_target is not None:
        saved_note = "（被女巫救下）" if night.saved else ""
        emit(
            "wolf_kill",
            Visibility.WOLF,
            payload={"seat": night.kill_target, "saved": night.saved},
            render=RenderText(text=f"狼队刀口：{night.kill_target} 号{saved_note}"),
        )

    hunter = rules.seat_of_role(state, Role.HUNTER)
    if hunter is not None:
        hp = state.players[hunter]
        # 只对本夜的死亡触发；死于之前的白天不算
        if not hp.alive and hp.death_cause is not None and hp.death_day == state.day:
            if rules.can_hunter_shoot(state, hp.death_cause):
                state.pending_hunter = hunter
                return Phase.NIGHT_HUNTER_SHOOT

    return _check_win_or(state, emit, Phase.DAY_ANNOUNCE)


def _resolve_day_announce(state: GameState, emit: EmitFn) -> Phase:
    seats = [d.seat for d in state.pending_deaths]
    if seats:
        text = "昨夜出局：" + "、".join(f"{s} 号" for s in seats)
    else:
        text = "昨夜是平安夜，无人出局"
    emit(
        "night_deaths_announced",
        Visibility.PUBLIC,
        payload={"seats": seats},  # 不公布死因
        render=RenderText(text=text),
    )
    state.pending_deaths = []
    return Phase.DAY_DISCUSSION


# ---------------------------------------------------------------------------
# 白天
# ---------------------------------------------------------------------------


def _resolve_day_vote(state: GameState, emit: EmitFn, runoff: bool) -> Phase:
    counts = rules.tally(state.votes)
    tops = rules.top_candidates(counts)
    payload_counts = {str(k): v for k, v in counts.items()}

    if not tops:
        state.pending_lynch = None
        emit(
            "vote_result",
            Visibility.PUBLIC,
            payload={"target": None, "counts": payload_counts, "tied": []},
            render=RenderText(text=f"本日无人出局（{_counts_text(counts)}）"),
        )
        return Phase.DAY_RESOLVE

    if len(tops) == 1:
        state.pending_lynch = tops[0]
        emit(
            "vote_result",
            Visibility.PUBLIC,
            payload={"target": tops[0], "counts": payload_counts, "tied": []},
            render=RenderText(text=f"{tops[0]} 号被投票出局（{_counts_text(counts)}）"),
        )
        return Phase.DAY_RESOLVE

    emit(
        "vote_tie",
        Visibility.PUBLIC,
        payload={"tied": tops, "counts": payload_counts},
        render=RenderText(
            text=f"投票平票：{_seats_text(tops)}（各 {counts[tops[0]]} 票），进入加时发言",
        ),
    )

    if runoff:
        if state.board.vote.no_lynch_on_second_tie:
            target = None
        else:
            target = min(tops)  # 兜底：取座位号最小者
        state.pending_lynch = target
        emit(
            "vote_result",
            Visibility.PUBLIC,
            payload={
                "target": target,
                "counts": payload_counts,
                "tied": tops,
                "second_tie": True,
            },
            render=RenderText(text=f"二次平票，本日无人出局（{_counts_text(counts)}）"),
        )
        return Phase.DAY_RESOLVE

    if state.board.vote.runoff_on_tie:
        state.tie_candidates = tops
        return Phase.DAY_RUNOFF_SPEECH

    state.pending_lynch = None
    return Phase.DAY_RESOLVE


def _resolve_day(state: GameState, emit: EmitFn) -> Phase:
    b = state.board
    seat = state.pending_lynch
    if seat is not None and state.players[seat].alive:
        rules.kill_player(state, seat, DeathCause.LYNCH)
        emit(
            "death",
            Visibility.PUBLIC,
            actor=None,
            payload={"seat": seat, "cause": DeathCause.LYNCH.value},
            render=RenderText(text=f"{seat} 号被投票出局"),
        )
    state.pending_lynch = None

    if seat is not None:
        if b.death.last_words_on_lynch:
            state.pending_last_words = [seat]
        if state.players[seat].role == Role.HUNTER and b.hunter.shoot_on_lynch:
            state.pending_hunter = seat

    if state.pending_last_words:
        return Phase.LAST_WORDS
    if state.pending_hunter is not None:
        return Phase.HUNTER_SHOOT
    return _end_day(state, emit)


def _apply_hunter_shot(state: GameState, emit: EmitFn, night: bool) -> None:
    seat = state.pending_hunter
    target = state.pending_hunter_target
    if seat is not None and target is not None and state.players[target].alive:
        rules.kill_player(state, target, DeathCause.SHOT)
        if night:
            # 夜里开的枪也要计入「昨夜死亡名单」，随白天公告一起公布
            state.pending_deaths.append(Death(seat=target, cause=DeathCause.SHOT))
        emit(
            "death",
            Visibility.PUBLIC,
            actor=seat,
            payload={"seat": target, "cause": DeathCause.SHOT.value},
            render=RenderText(text=f"{seat} 号开枪带走了 {target} 号"),
        )
    state.pending_hunter = None
    state.pending_hunter_target = None


def _end_day(state: GameState, emit: EmitFn) -> Phase:
    winner = rules.check_winner(state)
    if winner is not None:
        return _finish(state, emit, winner)

    state.day += 1
    state.night = NightState()
    state.pending_deaths = []
    state.pending_last_words = []
    state.pending_hunter = None
    state.pending_hunter_target = None
    state.tie_candidates = []
    state.votes = {}
    state.round_index = 0
    return Phase.NIGHT_WOLF_CHAT


def _check_win_or(state: GameState, emit: EmitFn, default: Phase) -> Phase:
    winner = rules.check_winner(state)
    if winner is None:
        return default
    return _finish(state, emit, winner)


def _finish(state: GameState, emit: EmitFn, winner: Winner) -> Phase:
    state.winner = winner
    text = "狼人阵营获胜" if winner == Winner.WOLF else "好人阵营获胜"
    emit(
        "game_over",
        Visibility.PUBLIC,
        payload={"winner": winner.value},
        render=RenderText(text=f"游戏结束：{text}"),
    )
    if not state.board.postgame.enabled:
        return Phase.GAME_OVER
    if state.board.postgame.reveal_all_roles:
        emit(
            "roles_revealed",
            Visibility.PUBLIC,
            payload={"roles": {str(s): p.role.value for s, p in sorted(state.players.items())}},
            render=RenderText(text="全部身份已揭示"),
        )
    return Phase.POSTGAME
