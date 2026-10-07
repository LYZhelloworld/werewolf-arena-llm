"""规则分支：合法性、可选目标集合、胜负判定。

纯函数式的辅助逻辑，全部只读 :class:`GameState`，除 :func:`kill_player` 外不修改状态
（``kill_player`` 也只在 ``apply()`` 深拷贝出的新状态上调用）。
"""

from __future__ import annotations

from engine.state import (
    FACTION_OF,
    GOD_ROLES,
    DeathCause,
    Faction,
    GameState,
    Player,
    Role,
    Winner,
)


def alive_seats(state: GameState) -> list[int]:
    """存活座位号，升序。"""
    return sorted(seat for seat, p in state.players.items() if p.alive)


def alive_players(state: GameState) -> list[Player]:
    return [p for p in state.players.values() if p.alive]


def wolves(state: GameState) -> list[int]:
    """全部狼人座位（含已出局）。"""
    return sorted(s for s, p in state.players.items() if p.role == Role.WEREWOLF)


def alive_wolves(state: GameState) -> list[int]:
    return sorted(s for s, p in state.players.items() if p.role == Role.WEREWOLF and p.alive)


def seat_of_role(state: GameState, role: Role) -> int | None:
    for seat, p in state.players.items():
        if p.role == role:
            return seat
    return None


def is_god(role: Role) -> bool:
    return role in GOD_ROLES


def faction_of(role: Role) -> Faction:
    return FACTION_OF[role]


def killable_targets(state: GameState, wolf_seat: int) -> list[int]:
    """狼人可以刀的目标。默认不可刀队友、不可刀自己。"""
    b = state.board.werewolf
    out: list[int] = []
    for seat, p in state.players.items():
        if not p.alive:
            continue
        if p.role == Role.WEREWOLF and not b.can_kill_teammate:
            continue
        if seat == wolf_seat and not b.can_kill_self:
            continue
        out.append(seat)
    return sorted(out)


def checkable_targets(state: GameState) -> list[int]:
    """预言家可以查验的目标。"""
    b = state.board.seer
    seer = seat_of_role(state, Role.SEER)
    out: list[int] = []
    for seat, p in state.players.items():
        if p.alive:
            if seat == seer and not b.can_check_self:
                continue
            out.append(seat)
        elif b.can_check_dead:
            out.append(seat)
    return sorted(out)


def poisonable_targets(state: GameState) -> list[int]:
    """女巫可以毒的目标。同晚已用解药且不允许两药齐用时为空。"""
    b = state.board.witch
    if not state.potions.poison_left or state.night.poison_target is not None:
        return []
    if state.night.saved and not b.same_night_both_potions:
        return []
    witch = seat_of_role(state, Role.WITCH)
    out: list[int] = []
    for seat, p in state.players.items():
        if not p.alive:
            continue
        if seat == witch and not b.self_poison:
            continue
        out.append(seat)
    return sorted(out)


def can_witch_save(state: GameState) -> bool:
    """女巫本夜能否用解药。

    解药的作用对象固定为「今夜刀口」——女巫只能决定救或不救，不能另选目标。
    ``self_save`` 系列开关只在刀口恰好是女巫本人时才生效。
    """
    b = state.board.witch
    night = state.night
    if not state.potions.antidote_left or night.saved:
        return False
    if night.kill_target is None:
        return False  # 空刀，无从可救
    if night.poison_target is not None and not b.same_night_both_potions:
        return False
    if night.kill_target == seat_of_role(state, Role.WITCH):
        if not b.self_save:
            return False
        if b.self_save_first_night_only and state.day != 1:
            return False
    return True


def shootable_targets(state: GameState) -> list[int]:
    """猎人可以开枪的目标：全部存活者（猎人自己此时已出局）。"""
    return alive_seats(state)


def can_hunter_shoot(state: GameState, cause: DeathCause) -> bool:
    """猎人死于该死因时能否开枪。被女巫毒死默认不能开枪。"""
    h = state.board.hunter
    if cause == DeathCause.KILL:
        return h.shoot_on_kill
    if cause == DeathCause.POISON:
        return h.shoot_on_poison
    if cause == DeathCause.LYNCH:
        return h.shoot_on_lynch
    return False  # 被猎人开枪打死的人不再触发技能


def tally(votes: dict[int, int | None]) -> dict[int, int]:
    """计票。弃权（None）不计入任何候选。"""
    counts: dict[int, int] = {}
    for target in votes.values():
        if target is None:
            continue
        counts[target] = counts.get(target, 0) + 1
    return counts


def top_candidates(counts: dict[int, int]) -> list[int]:
    """得票最高的候选（可能多个，即平票）。升序返回。"""
    if not counts:
        return []
    best = max(counts.values())
    return sorted(seat for seat, count in counts.items() if count == best)


def kill_player(state: GameState, seat: int, cause: DeathCause) -> None:
    """把某座位标记为出局。只能在 ``apply()`` 的深拷贝上调用。"""
    state.players[seat] = state.players[seat].model_copy(
        update={"alive": False, "death_cause": cause, "death_day": state.day}
    )


def check_winner(state: GameState) -> Winner | None:
    """胜负判定。默认屠边：神职全灭或平民全灭即狼人胜。"""
    alive = alive_players(state)
    if not any(p.role == Role.WEREWOLF for p in alive):
        return Winner.GOOD
    if state.board.win.eliminate_one_faction:
        gods = [p for p in alive if is_god(p.role)]
        villagers = [p for p in alive if p.role == Role.VILLAGER]
        if not gods or not villagers:
            return Winner.WOLF
    return None
