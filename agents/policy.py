"""LLMPolicy：把 LLM 接进 :class:`Policy` 这条缝。

编排层主循环是同步的（M2 就定下了），并发只在 ``decide_all`` 内部用一次
``asyncio.run`` + ``gather`` 完成（DESIGN.md §5.3.2）。这样同步策略与 LLM 策略
共用同一个 ``GameRunner``，M5 再整体 async 化时只需把这里改成 ``await``。
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, Field

from agents.client import LLMClient
from agents.context import AgentContext, make_context
from agents.decision import Decision
from agents.loop import PendingEvent, TurnOutcome, TurnParams, run_turn
from config import DefaultsConfig, GameConfig, ModelsConfig, ThinkingConfig
from engine.actions import Action, LegalAction
from engine.events import Event, LlmMeta, Visibility
from engine.state import GameState
from orchestrator.policies import deterministic_action


class SeatSpec(BaseModel):
    """某个席位用哪个 provider 的哪个模型，以及它的 CoT 策略。"""

    provider: str
    model: str
    thinking: ThinkingConfig = Field(default_factory=ThinkingConfig)
    extra_body: dict[str, Any] = Field(default_factory=dict)
    max_concurrency: int | None = None


class LLMPolicy:
    """实现 ``Policy``（单席位同步）+ ``decide_all``（批量并发）+ ``observe``。"""

    def __init__(
        self,
        clients: Mapping[str, LLMClient],
        seats: dict[int, SeatSpec],
        game: GameConfig,
        defaults: DefaultsConfig | None = None,
    ) -> None:
        self.clients = clients
        self.seats = seats
        self.game = game
        self.defaults = defaults or DefaultsConfig()
        self.contexts: dict[int, AgentContext] = {}

    # --- 上下文 -----------------------------------------------------------

    def observe(self, events: list[Event], state: GameState) -> None:
        """把新事件喂给每个 agent（各自过滤，各自渲染）。"""
        self._ensure_contexts(state)
        for context in self.contexts.values():
            context.observe(events, state)

    def _ensure_contexts(self, state: GameState) -> None:
        for seat, spec in self.seats.items():
            if seat in self.contexts:
                continue
            self.contexts[seat] = make_context(
                seat,
                state.players[seat],
                state,
                model=spec.model,
                thinking=spec.thinking.model_copy(deep=True),
                memory=self.game.agent.memory.model_copy(deep=True),
            )

    # --- 决策 -------------------------------------------------------------

    def decide(self, state: GameState, legal: list[LegalAction]) -> Action:
        """单席位同步决策（``Policy`` 协议）。测试与兜底路径用。"""
        return asyncio.run(self._decide_batch(state, legal))[0].action

    def decide_all(self, state: GameState, legal: list[LegalAction]) -> list[Decision]:
        """本阶段所有待行动席位**并发**决策。编排层优先调用它。"""
        return asyncio.run(self._decide_batch(state, legal))

    async def _decide_batch(self, state: GameState, legal: list[LegalAction]) -> list[Decision]:
        self._ensure_contexts(state)
        by_seat: dict[int, list[LegalAction]] = {}
        for spec in legal:
            by_seat.setdefault(spec.seat, []).append(spec)

        semaphores = self._semaphores()
        tasks = [self._one_seat(state, by_seat[seat], semaphores) for seat in sorted(by_seat)]
        return list(await asyncio.gather(*tasks))

    async def _one_seat(
        self,
        state: GameState,
        legal: list[LegalAction],
        semaphores: dict[str, asyncio.Semaphore],
    ) -> Decision:
        spec = self.seats[legal[0].seat]
        semaphore = semaphores[spec.provider]
        async with semaphore:
            outcome = await run_turn(
                self.clients[spec.provider],
                self.contexts[legal[0].seat],
                state,
                legal,
                TurnParams(
                    model=spec.model,
                    temperature=self.defaults.temperature,
                    max_tokens=self.defaults.max_tokens,
                    extra_body=spec.extra_body,
                    max_tool_turns=self.game.agent.max_tool_turns,
                    require_think_tool=self.game.thinking.require_think_tool,
                ),
            )
        return self._to_decision(legal, spec, outcome)

    def _to_decision(
        self, legal: list[LegalAction], spec: SeatSpec, outcome: TurnOutcome
    ) -> Decision:
        meta = LlmMeta(
            provider=spec.provider,
            model=spec.model,
            latency_ms=outcome.latency_ms,
            prompt_tokens=outcome.prompt_tokens,
            completion_tokens=outcome.completion_tokens,
            tool_turns=outcome.tool_turns,
            fallback=outcome.fallback,
        )
        if outcome.action is not None:
            return Decision(
                action=outcome.action,
                thinking=outcome.thinking if outcome.thinking.text else None,
                llm=meta,
                side_events=outcome.pending_events,
            )

        # 轮数用尽 / 调用失败 → 确定性兜底，留痕（§8.5 静默兜底等于埋雷）
        reason = "; ".join(outcome.rejections) or "模型未在限定轮数内给出合法动作"
        events = list(outcome.pending_events)
        events.append(
            PendingEvent(
                type="action_fallback",
                visibility=Visibility.SPECTATOR,
                actor=legal[0].seat,
                payload={"reason": reason, "phase_actor": legal[0].seat},
            )
        )
        return Decision(
            action=deterministic_action(legal),
            thinking=outcome.thinking if outcome.thinking.text else None,
            llm=meta,
            side_events=events,
        )

    def _semaphores(self) -> dict[str, asyncio.Semaphore]:
        """全局并发 + provider 级覆盖（§7.7）。"""
        out: dict[str, asyncio.Semaphore] = {}
        for seat_spec in self.seats.values():
            if seat_spec.provider in out:
                continue
            limit = seat_spec.max_concurrency or self.game.agent.max_concurrency
            out[seat_spec.provider] = asyncio.Semaphore(max(1, limit))
        return out


def specs_from_models(
    models: ModelsConfig, assignment: dict[int, str], **thinking_overrides: Any
) -> dict[int, SeatSpec]:
    """把「席位 → 模型名」翻译成 SeatSpec（含三层合并后的 CoT 策略）。

    ``assignment`` 形如 ``{1: "deepseek-chat", ...}``；模型名必须在 ``models.yaml`` 里存在。
    """
    from config import resolve_thinking

    specs: dict[int, SeatSpec] = {}
    for seat, model_name in assignment.items():
        found = None
        for provider_name, provider in models.providers.items():
            for entry in provider.models:
                if entry.name == model_name:
                    found = (provider_name, provider, entry)
                    break
            if found:
                break
        if found is None:
            raise KeyError(f"models.yaml 里找不到模型 {model_name}")

        provider_name, provider, entry = found
        from config import resolve_extra_body

        specs[seat] = SeatSpec(
            provider=provider_name,
            model=model_name,
            thinking=resolve_thinking(models.defaults, provider, entry),
            extra_body=resolve_extra_body(provider, entry),
            max_concurrency=provider.max_concurrency,
        )
    return specs
