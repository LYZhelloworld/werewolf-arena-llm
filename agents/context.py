"""每个席位一个独立的对话上下文。

硬约束（DESIGN.md §6.4）：**每个 agent 持有物理独立的 history 对象**。
这里不放任何别的玩家的 CoT——不是靠过滤，而是压根没有入口把它放进来。

公开历史用**滑动窗口**压缩（DESIGN.md §7.5，M8 实现）：单局累计的公开事件
一旦超过 ``context.max_public_events``，就丢弃最早回合的事件文本，并在 system
之后插入一行模板摘要（死亡公告 + 投票结果 + 每轮首句）。只丢事件文本，保留
每回合的局面与指令，避免破坏 user/assistant/tool 的消息结构。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agents import prompts
from agents.thinking import build_assistant_message
from config import AgentMemoryConfig, ThinkingConfig
from engine.events import Event, Thinking
from engine.state import GameState, Player
from engine.visibility import filter_events_for

# 记忆工具的执行结果文本（中文，回给模型）
MEMORY_FULL = "记忆已达上限（{n} 条），请先精简或合并再写入。"

# 摘要里值得保留的事件类型（DESIGN §7.5：死亡公告 + 投票结果 + 每轮首句）
DIGEST_TYPES = ("death", "night_deaths_announced", "vote_result", "roles_revealed")
# 摘要最多保留的行数，避免摘要本身无限增长
DIGEST_MAX_LINES = 20


@dataclass
class _Line:
    """一条渲染好的公开事件文本，``digest`` 标记它在被丢弃时是否要进摘要。"""

    text: str
    digest: bool = False


@dataclass
class _Turn:
    """一个回合：它的 user 消息、以及拆开保存的各段内容（便于压缩时重建）。"""

    message: dict[str, Any]
    situation: str
    instruction: str
    lines: list[_Line] = field(default_factory=list)


class AgentContext:
    """一个席位的完整上下文：system prompt + 增量历史 + 私有记忆。"""

    def __init__(
        self,
        seat: int,
        player: Player,
        system: str,
        *,
        thinking: ThinkingConfig,
        memory: AgentMemoryConfig,
        model: str,
        max_public_events: int = 60,
    ) -> None:
        self.seat = seat
        self.player = player
        self.model = model
        self.thinking = thinking
        self.memory = memory
        # <= 0 视为不压缩（保留全部公开历史）
        self.max_public_events = max_public_events
        self.messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
        self.memories: dict[str, str] = {}
        self._pending: list[_Line] = []
        self._turns: list[_Turn] = []
        self._digest: list[str] = []
        self._digest_message: dict[str, Any] | None = None
        self._speak_days: set[int] = set()
        # CoT 字段名在本 agent 首次拿到推理内容后固定下来（auto 探测只需做一次）
        self.reasoning_field: str | None = (
            None if thinking.field.strip() == "auto" else thinking.field.strip()
        )

    # --- 观察 -------------------------------------------------------------

    def observe(self, events: list[Event], state: GameState) -> None:
        """接收一批新事件，只保留本席位能看到的，渲染成中文待用。

        出局玩家的私有频道在此永久关闭（``filter_events_for`` 已按 ``alive`` 处理）。
        """
        visible = filter_events_for(events, self.seat, self.player.role, self.player.alive)
        for event in visible:
            text = prompts.render_event(event)
            if not text:
                continue
            digest = event.type in DIGEST_TYPES
            # 「每轮首句」：某天第一条公开发言也进摘要（压缩后保留叙事骨架）
            if event.type == "speech" and event.day not in self._speak_days:
                self._speak_days.add(event.day)
                digest = True
            self._pending.append(_Line(text, digest=digest))

    # --- 回合 -------------------------------------------------------------

    def begin_turn(self, state: GameState, targets: list[int]) -> str:
        """组装本回合的 user 消息并追加进历史，返回其内容（便于测试断言）。"""
        situation = prompts.situation_text(state, self.seat)
        parts = [situation]
        if self._pending:
            parts.append("自上次行动以来：\n" + "\n".join(line.text for line in self._pending))
        instruction = prompts.phase_instruction(state, self.seat, targets)
        parts.append(instruction)
        content = "\n\n".join(parts)
        message = {"role": "user", "content": content}
        self.messages.append(message)
        self._turns.append(
            _Turn(
                message=message,
                situation=situation,
                instruction=instruction,
                lines=self._pending,
            )
        )
        self._pending = []
        self._compact()
        return content

    def append_assistant(
        self, text: str | None, thinking: Thinking, tool_calls: list[dict[str, Any]]
    ) -> None:
        """把一次响应写进历史。CoT 按 ``replay`` 决定是否保留、放在独立字段里。

        ``thinking`` 必须是 :func:`agents.thinking.capture` 的产物——它的 ``source``
        决定了回传时该用哪个字段名（``auto`` 探测只在首次拿到推理内容时做一次）。
        """
        message = build_assistant_message(
            text, thinking, tool_calls, self.thinking, self.reasoning_field
        )
        if self.reasoning_field is None and thinking.replayed:
            self.reasoning_field = thinking.source
        self.messages.append(message)

    def append_tool_result(self, call_id: str, name: str, content: str) -> None:
        self.messages.append(
            {"role": "tool", "tool_call_id": call_id, "name": name, "content": content}
        )

    # --- 压缩 -------------------------------------------------------------

    def _compact(self) -> None:
        """公开历史超过窗口时，丢弃最早回合的事件文本并累积摘要（DESIGN §7.5）。"""
        if self.max_public_events <= 0:
            return
        total = sum(len(turn.lines) for turn in self._turns)
        if total <= self.max_public_events:
            return
        # 逐个丢弃最早回合（保留当前回合，即最后一个），直到降到窗口以内
        for turn in self._turns[:-1]:
            if total <= self.max_public_events:
                break
            if not turn.lines:
                continue
            self._digest.extend(line.text for line in turn.lines if line.digest)
            total -= len(turn.lines)
            turn.lines = []
            turn.message["content"] = f"{turn.situation}\n\n{turn.instruction}"
        self._render_digest()

    def _render_digest(self) -> None:
        """把累积的摘要渲染成一条消息，插在 system 之后（只建一次，之后原地更新）。"""
        if not self._digest:
            return
        kept = self._digest[-DIGEST_MAX_LINES:]
        content = "（前情提要：" + "；".join(kept) + "）"
        if self._digest_message is None:
            self._digest_message = {"role": "user", "content": content}
            self.messages.insert(1, self._digest_message)
        else:
            self._digest_message["content"] = content

    # --- 记忆 -------------------------------------------------------------

    def write_memory(self, key: str, content: str) -> tuple[bool, str]:
        """写入一条记忆，返回 ``(是否成功, 回给模型的说明)``。"""
        if len(self.memories) >= self.memory.max_entries and key not in self.memories:
            return False, MEMORY_FULL.format(n=self.memory.max_entries)
        if len(content) > self.memory.max_chars_per_entry:
            return False, (
                f"这条记忆超过 {self.memory.max_chars_per_entry} 字上限，请压缩后再写入。"
            )
        self.memories[key] = content
        return True, f"已记下：{key}"

    def read_memory(self) -> str:
        if not self.memories:
            return "你还没有记下任何内容。"
        return "\n".join(f"- {key}：{value}" for key, value in self.memories.items())

    # --- 导出 -------------------------------------------------------------

    def snapshot(self) -> list[dict[str, Any]]:
        """落盘用的消息快照（``contexts/{seat}.json``）。

        压缩是**原地**改写 ``messages`` 的，故快照即「模型实际看到的内容」。
        """
        return self.messages


def make_context(
    seat: int,
    player: Player,
    state: GameState,
    *,
    model: str,
    max_public_events: int = 60,
    **kwargs: Any,
) -> AgentContext:
    system = prompts.system_prompt(seat, player, state)
    return AgentContext(
        seat, player, system, model=model, max_public_events=max_public_events, **kwargs
    )
