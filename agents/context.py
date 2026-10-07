"""每个席位一个独立的对话上下文。

硬约束（DESIGN.md §6.4）：**每个 agent 持有物理独立的 history 对象**。
这里不放任何别的玩家的 CoT——不是靠过滤，而是压根没有入口把它放进来。
"""

from __future__ import annotations

from typing import Any

from agents import prompts
from agents.thinking import build_assistant_message
from config import AgentMemoryConfig, ThinkingConfig
from engine.events import Event, Thinking
from engine.state import GameState, Player
from engine.visibility import filter_events_for

# 记忆工具的执行结果文本（中文，回给模型）
MEMORY_FULL = "记忆已达上限（{n} 条），请先精简或合并再写入。"


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
    ) -> None:
        self.seat = seat
        self.player = player
        self.model = model
        self.thinking = thinking
        self.memory = memory
        self.messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
        self.memories: dict[str, str] = {}
        self._pending: list[str] = []
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
            if text:
                self._pending.append(text)

    # --- 回合 -------------------------------------------------------------

    def begin_turn(self, state: GameState, targets: list[int]) -> str:
        """组装本回合的 user 消息并追加进历史，返回其内容（便于测试断言）。"""
        parts = [prompts.situation_text(state, self.seat)]
        if self._pending:
            parts.append("自上次行动以来：\n" + "\n".join(self._pending))
            self._pending.clear()
        parts.append(prompts.phase_instruction(state, self.seat, targets))
        content = "\n\n".join(parts)
        self.messages.append({"role": "user", "content": content})
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
        """落盘用的消息快照（``contexts/{seat}.json``）。"""
        return self.messages


def make_context(
    seat: int, player: Player, state: GameState, *, model: str, **kwargs: Any
) -> AgentContext:
    system = prompts.system_prompt(seat, player, state)
    return AgentContext(seat, player, system, model=model, **kwargs)
