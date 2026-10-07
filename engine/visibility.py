"""信息隔离：按可见性过滤事件。

这是整个项目最容易出错、后果最严重的部分。两条硬规则：

1. agent 只能拿到过滤后的视图，不存在「先给全部再让它自己不看」的实现方式。
2. ``thinking``（CoT）**物理剔除**——不是渲染后打码，是根本不读进来。
"""

from __future__ import annotations

from engine.events import Event, Visibility
from engine.state import GameState, Role

__all__ = ["is_visible_to", "filter_events_for", "view_for", "replay_view"]


def is_visible_to(event: Event, seat: int, role: Role, alive: bool) -> bool:
    """判断一条事件对某个座位是否可见。

    参数 ``alive`` 是玩家**当前**是否存活：出局玩家的私有频道永久关闭，
    但仍继续旁观全部公开信息（DESIGN.md §6.5）。
    """
    v = event.visibility
    if v == Visibility.PUBLIC:
        return True
    # CoT、底牌、投票中间过程——任何 agent 都看不到
    if v == Visibility.SPECTATOR:
        return False
    if not alive:
        # 出局者：私有频道永久关闭，包括他生前所属阵营的
        return False
    if v == Visibility.WOLF:
        return role == Role.WEREWOLF
    if v == Visibility.SEER:
        return role == Role.SEER
    if v == Visibility.WITCH:
        return role == Role.WITCH
    if v == Visibility.SELF:
        return event.actor == seat
    return False


def filter_events_for(events: list[Event], seat: int, role: Role, alive: bool) -> list[Event]:
    """按可见性过滤事件，并**剔除 thinking 字段**。

    返回的事件对象里 ``thinking`` 恒为 ``None``。这是 CoT 不泄漏的物理保证：
    下游无论怎么拼装上下文，都拿不到思考内容。

    生产环境应按事件增量调用（agent 上下文是逐步追加的），这样已出局玩家
    出局前看到的私有内容仍留在他自己的历史里，只是不再新增。
    """
    out: list[Event] = []
    for event in events:
        if not is_visible_to(event, seat, role, alive):
            continue
        if event.thinking is not None:
            event = event.model_copy(update={"thinking": None})
        out.append(event)
    return out


def view_for(state: GameState, events: list[Event], seat: int) -> list[Event]:
    """某个座位能看到的全部事件（已剔除 CoT）。供上下文构建与回放视角切换使用。"""
    player = state.players[seat]
    return filter_events_for(events, seat, player.role, player.alive)


class ViewFilter:
    """按某个座位的视角，增量地过滤事件流。

    维护一份随事件推进的 ``alive`` 状态：玩家活着时该看的私有事件照看，
    一旦 ``death`` 事件落到自己头上，之后的私有频道永久关闭（但已看到的保留）。
    这正是 WebSocket 实时推送需要的——用同一个过滤器实例，先喂历史、再喂实时，
    绝不重复也不会漏。返回的 ``thinking`` 恒为 ``None``（CoT 物理剔除）。
    """

    def __init__(self, seat: int, role: Role) -> None:
        self.seat = seat
        self.role = role
        self.alive = True

    def apply(self, events: list[Event]) -> list[Event]:
        out: list[Event] = []
        for event in events:
            if is_visible_to(event, self.seat, self.role, self.alive):
                if event.thinking is not None:
                    event = event.model_copy(update={"thinking": None})
                out.append(event)
            if event.type == "death" and event.actor == self.seat:
                self.alive = False
        return out


def replay_view(events: list[Event], seat: int, role: Role) -> list[Event]:
    """回放视角的一次性过滤（等价于 ``ViewFilter(seat, role).apply(events)``）。

    ``filter_events_for`` 用的是玩家**最终**存活状态，对中途出局的玩家会把
    他生前看到的私有内容一并抹掉——回放视角不能这么干，所以用 ``ViewFilter``
    按事件顺序逐条判断、随死亡事件实时关闭私有频道。
    """
    return ViewFilter(seat, role).apply(events)
