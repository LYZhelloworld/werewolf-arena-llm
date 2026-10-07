"""多轮工具循环（ReAct）。

规则见 DESIGN.md §7.2：

1. 辅助工具（记忆 / 思考）执行完继续循环，不推进阶段。
2. 动作工具参数非法 → 把**具体原因**作为 tool result 回给模型重选（不是直接报错）。
3. 轮数用尽仍无合法动作 → 交给编排层兜底，并打 ``fallback`` 标记。
4. 不使用 ``tool_choice="required"``——必须先允许模型调辅助工具。

回传被 provider 拒绝（400）时的降级也在这里（§7.4.3）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field

from agents.client import ChatRequest, LLMClient, LLMError, LLMResponse
from agents.context import AgentContext
from agents.thinking import capture
from agents.tools import (
    MEMORY_READ,
    MEMORY_WRITE,
    build_tools,
    call_to_action,
    is_memory_tool,
    is_think_tool,
)
from engine.actions import Action, LegalAction
from engine.events import Event, Thinking, Visibility
from engine.state import GameState

# 回传被拒时要从历史消息里剥掉的字段（auto 探测的候选 + 已确定的字段名）
_STRIP_FIELDS = ("reasoning_content", "reasoning", "reasoning_text")


class PendingEvent(BaseModel):
    """待编号的事件原型。

    记忆读写这类事件由 agent 产出，但 ``seq`` 只有引擎知道，
    所以这里只给内容与可见性，编号交给编排层（DESIGN.md §5.3.1）。
    """

    type: str
    visibility: Visibility
    actor: int | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


@dataclass
class TurnOutcome:
    """一回合的结果。``action`` 为 None 表示要兜底。"""

    action: Action | None
    thinking: Thinking
    tool_turns: int = 0
    fallback: bool = False
    latency_ms: int = 0
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    pending_events: list[PendingEvent] = field(default_factory=list)
    rejections: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class TurnParams:
    """一次请求里不变的参数。"""

    model: str
    temperature: float = 0.8
    max_tokens: int = 1024
    extra_body: dict[str, Any] = field(default_factory=dict)
    max_tool_turns: int = 4
    require_think_tool: bool = False


async def run_turn(
    client: LLMClient,
    context: AgentContext,
    state: GameState,
    legal: list[LegalAction],
    params: TurnParams,
) -> TurnOutcome:
    """跑完一个席位的完整回合（可能多轮）。"""
    tools = build_tools(
        legal,
        memory_enabled=context.memory.enabled,
        require_think_tool=params.require_think_tool,
    )
    targets = sorted({seat for spec in legal for seat in spec.allowed_targets})
    context.begin_turn(state, targets)

    thinking = Thinking(text=None, source="none", replayed=False)
    outcome = TurnOutcome(action=None, thinking=thinking)

    for turn in range(params.max_tool_turns):
        # replay 一旦被降级就不再尝试剥离重试（没什么可剥的了）
        response = await _request(client, context, tools, params, outcome)
        if response is None:  # 回传被拒且策略是 fail，或重试也失败
            outcome.fallback = True
            return outcome

        _merge_thinking(outcome.thinking, capture(response.raw, context.thinking), context)
        outcome.tool_turns = turn + 1
        outcome.latency_ms += response.latency_ms
        outcome.prompt_tokens = (outcome.prompt_tokens or 0) + (response.prompt_tokens or 0)
        outcome.completion_tokens = (outcome.completion_tokens or 0) + (
            response.completion_tokens or 0
        )

        if not response.tool_calls:
            # 模型只是说话没有行动：明确要求它调工具，再给一轮
            context.messages.append(
                {"role": "user", "content": "你必须通过调用工具来行动，请调用一个工具。"}
            )
            continue

        context.append_assistant(
            response.content,
            capture(response.raw, context.thinking),
            [_as_tool_call_payload(call) for call in response.tool_calls],
        )

        results: list[tuple[str, str, str]] = []  # (call_id, name, content)
        for call in response.tool_calls:
            name = call.name
            if name == MEMORY_WRITE:
                ok, text = context.write_memory(
                    str(call.arguments.get("key", "")), str(call.arguments.get("content", ""))
                )
                if ok:
                    outcome.pending_events.append(
                        PendingEvent(
                            type="memory_write",
                            visibility=Visibility.SELF,
                            actor=context.seat,
                            payload={
                                "key": call.arguments.get("key"),
                                "content": call.arguments.get("content"),
                            },
                        )
                    )
                results.append((call.id, name, text))
                continue

            if name == MEMORY_READ:
                outcome.pending_events.append(
                    PendingEvent(
                        type="memory_read",
                        visibility=Visibility.SELF,
                        actor=context.seat,
                        payload={"keys": list(context.memories)},
                    )
                )
                results.append((call.id, name, context.read_memory()))
                continue

            if is_think_tool(name):
                text = str(call.arguments.get("text", "")).strip()
                if text:
                    _merge_thinking(
                        outcome.thinking, Thinking(text=text, source="think_tool"), context
                    )
                results.append((call.id, name, "已记录你的思考。"))
                continue

            if is_memory_tool(name):  # pragma: no cover - 上面已覆盖两个记忆工具
                results.append((call.id, name, "未知的记忆工具。"))
                continue

            action, reason = call_to_action(call, legal)
            if action is not None:
                results.append((call.id, name, "已执行。"))
                for call_id, tool_name, content in results:
                    context.append_tool_result(call_id, tool_name, content)
                outcome.action = action
                return outcome

            # 非法：把原因回给模型，让它下一轮重选
            outcome.rejections.append(reason or "非法动作")
            results.append((call.id, name, reason or "非法动作"))

        for call_id, tool_name, content in results:
            context.append_tool_result(call_id, tool_name, content)

    outcome.fallback = True
    return outcome


# ---------------------------------------------------------------------------
# 内部实现
# ---------------------------------------------------------------------------


async def _request(
    client: LLMClient,
    context: AgentContext,
    tools: list[dict[str, Any]],
    params: TurnParams,
    outcome: TurnOutcome,
) -> LLMResponse | None:
    """发一次请求。回传被拒时按 ``replay_on_error`` 剥离重试一次。"""
    request = ChatRequest(
        model=params.model,
        messages=context.messages,
        tools=tools,
        temperature=params.temperature,
        max_tokens=params.max_tokens,
        extra_body=params.extra_body,
    )
    try:
        return await client.achat(request)
    except LLMError as exc:
        if not _looks_like_replay_rejection(exc):
            raise
        if context.thinking.replay_on_error == "fail":
            outcome.rejections.append(f"LLM 调用失败：{exc.message}")
            return None
        if not context.thinking.replay:
            outcome.rejections.append(f"LLM 调用失败：{exc.message}")
            return None
        # 剥离历史里的 CoT 重试一次，并把本 agent 的 replay 降级（§7.4.3）
        _strip_reasoning(context.messages)
        context.thinking.replay = False
        outcome.rejections.append(f"回传思考内容被拒绝，已剥离重试：{exc.message}")
        return await client.achat(request)


def _looks_like_replay_rejection(exc: LLMError) -> bool:
    """回传被拒通常是 400 且错误信息里提到推理字段。"""
    if exc.status_code not in (None, 400):
        return False
    text = f"{exc.message} {exc}".lower()
    return any(token in text for token in ("reasoning", "thinking"))


def _strip_reasoning(messages: list[dict[str, Any]]) -> None:
    for message in messages:
        for name in _STRIP_FIELDS:
            message.pop(name, None)


def _as_tool_call_payload(call: Any) -> dict[str, Any]:
    return {
        "id": call.id,
        "type": "function",
        "function": {
            "name": call.name,
            "arguments": json.dumps(call.arguments, ensure_ascii=False),
        },
    }


def _merge_thinking(target: Thinking, incoming: Thinking, context: AgentContext) -> None:
    """多轮时把各轮的 CoT 串起来，source 取第一个有意义的。"""
    if not incoming.text:
        return
    if target.text:
        target.text = f"{target.text}\n\n{incoming.text}"
    else:
        target.text = incoming.text
        target.source = incoming.source
    if context.thinking.replay:
        target.replayed = True


def build_event(outcome: PendingEvent, state: GameState, seq: int, game_id: str) -> Event:
    """给待编号事件补上 seq / game_id / day / phase。"""
    return Event(
        seq=seq,
        game_id=game_id,
        day=state.day,
        phase=state.phase.value,
        type=outcome.type,
        visibility=outcome.visibility,
        actor=outcome.actor,
        payload=outcome.payload,
    )
