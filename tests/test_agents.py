"""Agent 运行时单测：多轮工具循环、CoT 捕获与回传、记忆、兜底、并发。

全部用脚本化客户端，**不碰真实 API key**。
"""

from __future__ import annotations

import asyncio
import json
import re
from types import SimpleNamespace
from typing import Any

import pytest

from agents.client import ChatRequest, LLMError, LLMResponse, OpenAICompatClient, ToolCall
from agents.context import DIGEST_TYPES, AgentContext
from agents.loop import TurnParams, run_turn
from agents.mock import MockClient, auto_responder, speech, tool_call
from agents.policy import LLMPolicy, SeatSpec, specs_from_models
from agents.prompts import phase_instruction, render_event, situation_text, system_prompt
from agents.tools import build_tools, call_to_action
from config import (
    AgentMemoryConfig,
    BoardConfig,
    ConfigError,
    GameConfig,
    ModelsConfig,
    ThinkingConfig,
)
from engine.actions import Action, ActionType, LegalAction
from engine.engine import RuleEngine
from engine.events import Event, Visibility
from engine.state import GameState, Phase, Player, Role
from orchestrator.game_loop import GameRunner

PARAMS = TurnParams(model="mock")


def make_state(roles: dict[int, Role] | None = None) -> GameState:
    """一个最小局面：默认 3 人全是平民。"""
    roles = roles or {1: Role.VILLAGER, 2: Role.VILLAGER, 3: Role.VILLAGER}
    return GameState(
        game_id="t",
        players={seat: Player(seat=seat, role=role) for seat, role in roles.items()},
        board=BoardConfig(),
    )


def make_ctx(
    seat: int = 1,
    role: Role = Role.VILLAGER,
    state: GameState | None = None,
    *,
    thinking: ThinkingConfig | None = None,
    memory: AgentMemoryConfig | None = None,
    max_public_events: int = 60,
) -> AgentContext:
    state = state or make_state()
    return AgentContext(
        seat,
        state.players[seat] if role is None else Player(seat=seat, role=role),
        system_prompt(seat, Player(seat=seat, role=role), state),
        thinking=thinking or ThinkingConfig(),
        memory=memory or AgentMemoryConfig(),
        model="mock",
        max_public_events=max_public_events,
    )


def run(client: MockClient, ctx: AgentContext, state: GameState, legal: list[LegalAction]):
    return asyncio.run(run_turn(client, ctx, state, legal, PARAMS))


# --- 工具协议 ---------------------------------------------------------------


def test_action_tools_only_expose_legal_targets() -> None:
    legal = [LegalAction(seat=1, type=ActionType.VOTE, allowed_targets=[2, 3])]
    tools = build_tools(legal, memory_enabled=False)
    names = [t["function"]["name"] for t in tools]
    assert names == ["vote"]
    vote = tools[0]["function"]
    assert vote["parameters"]["properties"]["seat"]["enum"] == [2, 3]


def test_call_to_action_rejects_target_outside_allowed() -> None:
    legal = [LegalAction(seat=1, type=ActionType.VOTE, allowed_targets=[2, 3])]
    action, reason = call_to_action(ToolCall(id="c", name="vote", arguments={"seat": 9}), legal)
    assert action is None
    assert reason is not None and "2、3" in reason


def test_tools_include_memory_when_enabled() -> None:
    legal = [LegalAction(seat=1, type=ActionType.SPEAK)]
    assert any(t["function"]["name"] == "memory_write" for t in build_tools(legal))
    assert not any(
        t["function"]["name"] == "memory_write" for t in build_tools(legal, memory_enabled=False)
    )


# --- 多轮循环 ---------------------------------------------------------------


def test_single_turn_action() -> None:
    legal = [LegalAction(seat=1, type=ActionType.VOTE, allowed_targets=[2, 3])]
    ctx = make_ctx()
    outcome = run(MockClient([tool_call("vote", seat=3)]), ctx, make_state(), legal)
    assert outcome.action == Action(actor=1, type=ActionType.VOTE, target=3)
    assert outcome.tool_turns == 1
    assert outcome.fallback is False


def test_illegal_target_is_returned_to_model_and_retried() -> None:
    """非法参数不能静默吞掉：原因要回给模型，让它在下一轮自己改。"""
    legal = [LegalAction(seat=1, type=ActionType.VOTE, allowed_targets=[2, 3])]
    client = MockClient([tool_call("vote", seat=9), tool_call("vote", seat=2)])
    outcome = run(client, ctx := make_ctx(), make_state(), legal)

    assert outcome.action == Action(actor=1, type=ActionType.VOTE, target=2)
    assert outcome.tool_turns == 2
    assert outcome.rejections and "9 号不是合法目标" in outcome.rejections[0]
    # 原因确实进了第二轮的请求里
    assert any(
        "9 号不是合法目标" in json.dumps(m, ensure_ascii=False) for m in client.calls[1].messages
    )
    assert ctx.messages[-1]["role"] == "tool"


def test_memory_write_then_action_is_two_turns() -> None:
    legal = [LegalAction(seat=1, type=ActionType.SPEAK)]
    client = MockClient(
        [
            tool_call("memory_write", key="怀疑", content="2 号发言很急"),
            speech("我先记一笔。"),
        ]
    )
    ctx = make_ctx()
    outcome = run(client, ctx, make_state(), legal)

    assert outcome.action == Action(actor=1, type=ActionType.SPEAK, message="我先记一笔。")
    assert outcome.tool_turns == 2
    assert ctx.memories == {"怀疑": "2 号发言很急"}
    assert [e.type for e in outcome.pending_events] == ["memory_write"]
    # agent 侧事件同样要有展示文本，否则前端只能显示裸 JSON
    assert outcome.pending_events[0].render is not None
    assert "记下「怀疑」" in outcome.pending_events[0].render.text
    assert "1 号" in outcome.pending_events[0].render.text


def test_memory_read_returns_written_entries() -> None:
    legal = [LegalAction(seat=1, type=ActionType.SPEAK)]
    client = MockClient(
        [
            tool_call("memory_write", key="k", content="v"),
            tool_call("memory_read"),
            speech("想起来了。"),
        ]
    )
    ctx = make_ctx()
    outcome = run(client, ctx, make_state(), legal)
    assert outcome.tool_turns == 3
    assert [e.type for e in outcome.pending_events] == ["memory_write", "memory_read"]
    assert any("k：v" in json.dumps(m, ensure_ascii=False) for m in ctx.messages)
    assert outcome.pending_events[1].render is not None
    assert "回忆：k" in outcome.pending_events[1].render.text


def test_memory_limit_is_enforced() -> None:
    legal = [LegalAction(seat=1, type=ActionType.SPEAK)]
    client = MockClient(
        [
            tool_call("memory_write", key="a", content="1"),
            tool_call("memory_write", key="b", content="2"),
            speech("满了。"),
        ]
    )
    ctx = make_ctx(memory=AgentMemoryConfig(max_entries=1))
    run(client, ctx, make_state(), legal)
    assert list(ctx.memories) == ["a"]
    assert any("记忆已达上限" in json.dumps(m, ensure_ascii=False) for m in ctx.messages)


def test_no_action_within_max_turns_falls_back() -> None:
    legal = [LegalAction(seat=1, type=ActionType.VOTE, allowed_targets=[2, 3])]
    client = MockClient([tool_call("vote", seat=9) for _ in range(4)])
    outcome = run(client, make_ctx(), make_state(), legal)
    assert outcome.fallback is True
    assert outcome.action is None
    assert outcome.tool_turns == PARAMS.max_tool_turns


def test_speaking_phase_turns_plain_text_into_the_speech_action() -> None:
    """发言阶段不再有 speak 工具：模型直接说的话就是发言动作，一轮即完成。"""
    legal = [LegalAction(seat=1, type=ActionType.SPEAK)]
    client = MockClient([LLMResponse(content="我不服")])
    ctx = make_ctx()
    outcome = run(client, ctx, make_state(), legal)

    assert outcome.action == Action(actor=1, type=ActionType.SPEAK, message="我不服")
    assert outcome.tool_turns == 1
    assert outcome.fallback is False
    # 不应再追加「请调用工具」这类纠正提示（本阶段压根没有动作工具）
    assert not any("没有调用工具" in json.dumps(m, ensure_ascii=False) for m in ctx.messages)
    # 自己的发言要留在历史里
    assert any(m["role"] == "assistant" and m.get("content") == "我不服" for m in ctx.messages)


def test_action_phase_still_asks_for_a_tool_when_model_only_talks() -> None:
    """动作类阶段（如投票）保留旧行为：只说话不算数，要点名工具催它动手。"""
    legal = [LegalAction(seat=1, type=ActionType.VOTE, allowed_targets=[2, 3])]
    client = MockClient([LLMResponse(content="我投 2 号"), tool_call("vote", seat=2)])
    ctx = make_ctx()
    outcome = run(client, ctx, make_state(), legal)

    assert outcome.action == Action(actor=1, type=ActionType.VOTE, target=2)
    second = client.calls[1].messages
    # 纠正提示要点名本回合实际可用的工具，而不是泛泛地「请调用一个工具」
    assert any("没有调用工具" in json.dumps(m, ensure_ascii=False) for m in second)
    assert any("vote" in json.dumps(m, ensure_ascii=False) for m in second)


def test_speech_after_memory_tool_keeps_assistant_history_well_formed() -> None:
    """先写记忆、再直接发言：发言进历史，且不会出现连续两条 user。"""
    legal = [LegalAction(seat=1, type=ActionType.SPEAK)]
    client = MockClient([tool_call("memory_write", key="k", content="v"), speech("我先记一笔")])
    ctx = make_ctx()
    run(client, ctx, make_state(), legal)

    roles = [m["role"] for m in ctx.messages]
    # 不再出现连续两条 user（以前「催工具」提示会一条条往上叠）
    assert all(not (roles[i] == "user" and roles[i + 1] == "user") for i in range(len(roles) - 1))
    assistant_texts = [m.get("content") for m in ctx.messages if m["role"] == "assistant"]
    assert "我先记一笔" in assistant_texts


def test_empty_replies_do_not_stack_the_same_reminder() -> None:
    """模型连回空内容时，同一句「直接发言」提示不该原样堆在历史里。"""
    legal = [LegalAction(seat=1, type=ActionType.SPEAK)]
    client = MockClient([LLMResponse() for _ in range(3)] + [speech("好吧")])
    ctx = make_ctx()
    outcome = run(client, ctx, make_state(), legal)
    assert outcome.action == Action(actor=1, type=ActionType.SPEAK, message="好吧")
    assert sum("请直接发言" in str(m.get("content")) for m in ctx.messages) == 1
    # 空回复不该写进历史，否则会生成服务端拒收的空 assistant 消息
    assert not any(
        m["role"] == "assistant" and not m.get("tool_calls") and not m.get("content")
        for m in ctx.messages
    )


# --- CoT -------------------------------------------------------------------


def test_cot_from_native_reasoning_field() -> None:
    legal = [LegalAction(seat=1, type=ActionType.SPEAK)]
    response = LLMResponse(
        content="我发言",
        raw={"role": "assistant", "content": "我发言", "reasoning_content": "我心里想的是 2 号"},
    )
    outcome = run(MockClient([response]), make_ctx(), make_state(), legal)
    assert outcome.thinking.text == "我心里想的是 2 号"
    assert outcome.thinking.source == "reasoning_content"


def test_cot_falls_back_to_content() -> None:
    """没有原生推理字段时，正文就是 CoT（对无 reasoning 能力的模型）。"""
    legal = [LegalAction(seat=1, type=ActionType.SPEAK)]
    response = LLMResponse(
        content="我先想想：2 号很可疑",
        raw={"role": "assistant", "content": "我先想想：2 号很可疑"},
    )
    outcome = run(MockClient([response]), make_ctx(), make_state(), legal)
    assert outcome.thinking.source == "content"
    assert outcome.thinking.text == "我先想想：2 号很可疑"


def test_cot_from_think_tool() -> None:
    legal = [LegalAction(seat=1, type=ActionType.SPEAK)]
    client = MockClient([tool_call("think", text="2 号保 3 号，可疑"), speech("我怀疑 2 号")])
    outcome = run(client, make_ctx(), make_state(), legal)
    assert outcome.thinking.text is not None and "可疑" in outcome.thinking.text
    assert outcome.thinking.source == "think_tool"


def test_replay_puts_cot_in_its_own_field_never_in_content() -> None:
    legal = [LegalAction(seat=1, type=ActionType.SPEAK)]
    response = LLMResponse(
        content="我发言",
        raw={"role": "assistant", "content": "我发言", "reasoning_content": "秘密思考"},
    )
    ctx = make_ctx(thinking=ThinkingConfig(enabled=True, replay=True))
    run(MockClient([response]), ctx, make_state(), legal)

    assistant = next(m for m in ctx.messages if m["role"] == "assistant")
    assert assistant["reasoning_content"] == "秘密思考"
    # 绝不能拼进 content（Qwen 官方明示会掉效果）
    assert "秘密思考" not in assistant.get("content", "")


def test_replay_disabled_keeps_cot_out_of_history_but_records_it() -> None:
    legal = [LegalAction(seat=1, type=ActionType.SPEAK)]
    response = LLMResponse(
        content="我发言",
        raw={"role": "assistant", "content": "我发言", "reasoning_content": "秘密思考"},
    )
    ctx = make_ctx(thinking=ThinkingConfig(enabled=True, replay=False))
    outcome = run(MockClient([response]), ctx, make_state(), legal)

    assistant = next(m for m in ctx.messages if m["role"] == "assistant")
    assert "reasoning_content" not in assistant
    # 回传关了，记录照常全量（§7.4 三者解耦）
    assert outcome.thinking.text == "秘密思考"
    assert outcome.thinking.replayed is False


def test_replay_rejected_by_provider_is_stripped_and_degraded() -> None:
    """回传被拒（400）时剥离重试一次，并把本 agent 的 replay 降级（§7.4.3）。"""
    legal = [LegalAction(seat=1, type=ActionType.SPEAK)]
    ok = LLMResponse(
        content="重来一次",
        raw={"role": "assistant", "content": "重来一次", "reasoning_content": "想了一遍"},
    )

    def responder(request: ChatRequest, index: int) -> LLMResponse:
        if index == 0:
            raise LLMError("Invalid reasoning_content in messages", status_code=400)
        return ok

    ctx = make_ctx(thinking=ThinkingConfig(enabled=True, replay=True, replay_on_error="fallback"))
    outcome = run(MockClient(responder), ctx, make_state(), legal)

    assert outcome.action is not None
    assert outcome.fallback is False
    assert ctx.thinking.replay is False  # 本 agent 已降级
    assert not any("reasoning_content" in m for m in ctx.messages)
    assert outcome.thinking.text == "想了一遍"


def test_replay_error_with_fail_policy_does_not_retry() -> None:
    legal = [LegalAction(seat=1, type=ActionType.SPEAK)]

    def responder(_request: ChatRequest, _index: int) -> LLMResponse:
        raise LLMError("bad reasoning", status_code=400)

    ctx = make_ctx(thinking=ThinkingConfig(enabled=True, replay=True, replay_on_error="fail"))
    outcome = run(MockClient(responder), ctx, make_state(), legal)
    assert outcome.fallback is True
    assert outcome.action is None


def test_network_error_propagates() -> None:
    legal = [LegalAction(seat=1, type=ActionType.SPEAK)]

    def responder(_request: ChatRequest, _index: int) -> LLMResponse:
        raise LLMError("connection reset", status_code=None)

    with pytest.raises(LLMError):
        run(MockClient(responder), make_ctx(), make_state(), legal)


# --- 上下文与信息隔离 -------------------------------------------------------


def seat_of(messages: list[dict]) -> int:
    """从 system prompt 里反解出这是几号玩家（MockClient 里区分席位的唯一依据）。"""
    match = re.search(r"你是 (\d+) 号玩家", messages[0]["content"])
    return int(match.group(1)) if match else 0


def test_each_agent_only_sees_its_own_cot() -> None:
    """每个 agent 的上下文里都不允许出现别人的思考内容。"""
    engine = RuleEngine(BoardConfig(), seed=7)
    state, _ = engine.new_game()
    wolves = sorted(s for s, p in state.players.items() if p.role == Role.WEREWOLF)
    legal = [la for la in engine.legal_actions(state) if la.seat in wolves]

    def responder(request: ChatRequest, _index: int) -> LLMResponse:
        seat = seat_of(request.messages)
        return LLMResponse(
            content=f"{seat} 号发言",
            raw={
                "role": "assistant",
                "content": f"{seat} 号发言",
                "reasoning_content": f"秘密-{seat}",
            },
        )

    client = MockClient(responder)
    game = GameConfig()
    policy = LLMPolicy(
        {"mock": client},
        {seat: SeatSpec(provider="mock", model="mock") for seat in wolves},
        game,
    )
    runner = GameRunner(engine, policy)
    state, events = runner._act_all_batched(state, legal)

    for seat, context in policy.contexts.items():
        blob = json.dumps(context.snapshot(), ensure_ascii=False)
        for other in wolves:
            if other != seat:
                assert f"秘密-{other}" not in blob, f"{seat} 号看到了 {other} 号的思考"
        # 自己的思考要留着（replay 默认开）
        assert f"秘密-{seat}" in blob

    # 事件流里 CoT 是观众可见，且全量记录
    thinkers = [e for e in events if e.thinking is not None and e.thinking.text]
    assert {e.thinking.text for e in thinkers if e.thinking} == {f"秘密-{s}" for s in wolves}
    assert all(e.visibility.value == "private:wolf" for e in thinkers)


def test_cot_is_attached_to_the_action_it_produced() -> None:
    engine = RuleEngine(BoardConfig(), seed=7)
    state, _ = engine.new_game()
    wolves = sorted(s for s, p in state.players.items() if p.role == Role.WEREWOLF)
    legal = [la for la in engine.legal_actions(state) if la.seat in wolves]

    def responder(request: ChatRequest, _index: int) -> LLMResponse:
        seat = seat_of(request.messages)
        return LLMResponse(
            content="刀他",
            raw={"role": "assistant", "content": "刀他", "reasoning_content": f"想-{seat}"},
        )

    policy = LLMPolicy(
        {"mock": MockClient(responder)},
        {seat: SeatSpec(provider="mock", model="mock") for seat in wolves},
        GameConfig(),
    )
    state, events = GameRunner(engine, policy)._act_all_batched(state, legal)

    speeches = [e for e in events if e.type == "wolf_chat"]
    assert len(speeches) == len(wolves)
    for event in speeches:
        assert event.thinking is not None and event.thinking.text == f"想-{event.actor}"
        assert event.llm is not None and event.llm.model == "mock"
        assert event.llm.fallback is False


def test_side_events_are_numbered_before_the_action() -> None:
    """先记下记忆、再行动——复盘时顺序才符合直觉。"""
    engine = RuleEngine(BoardConfig(), seed=7)
    state, _ = engine.new_game()
    wolves = sorted(s for s, p in state.players.items() if p.role == Role.WEREWOLF)
    legal = [la for la in engine.legal_actions(state) if la.seat in wolves]

    def responder(request: ChatRequest, _index: int) -> LLMResponse:
        # 第一轮写记忆，第二轮才行动（用是否已出现过「已记下」判断）
        last = request.messages[-1]
        done = last.get("role") == "tool" and "已记下" in last.get("content", "")
        if not done:
            return tool_call("memory_write", key="刀口", content="先刀神职")
        return speech("收到")

    policy = LLMPolicy(
        {"mock": MockClient(responder)},
        {seat: SeatSpec(provider="mock", model="mock") for seat in wolves},
        GameConfig(),
    )
    state, events = GameRunner(engine, policy)._act_all_batched(state, legal)

    seqs = [e.seq for e in events]
    assert seqs == sorted(seqs)
    first_write = next(e.seq for e in events if e.type == "memory_write")
    first_speech = next(e.seq for e in events if e.type == "wolf_chat")
    assert first_write < first_speech
    assert all(e.visibility.value == "private:self" for e in events if e.type == "memory_write")


# --- 并发 -----------------------------------------------------------------


def test_decide_all_calls_every_pending_seat_once() -> None:
    engine = RuleEngine(BoardConfig(), seed=7)
    state, _ = engine.new_game()
    wolves = sorted(s for s, p in state.players.items() if p.role == Role.WEREWOLF)
    legal = [la for la in engine.legal_actions(state) if la.seat in wolves]

    client = MockClient(auto_responder())
    policy = LLMPolicy(
        {"mock": client},
        {seat: SeatSpec(provider="mock", model="mock") for seat in wolves},
        GameConfig(),
    )
    decisions = policy.decide_all(state, legal)
    assert len(decisions) == len(wolves)
    assert client.call_count == len(wolves)
    assert all(d.action is not None for d in decisions)


def test_concurrency_limit_is_respected() -> None:
    """全局并发信号量：同时在飞的请求数不超过配置值。"""
    engine = RuleEngine(BoardConfig(), seed=7)
    state, _ = engine.new_game()
    wolves = sorted(s for s, p in state.players.items() if p.role == Role.WEREWOLF)
    legal = [la for la in engine.legal_actions(state) if la.seat in wolves]

    in_flight = 0
    peak = 0

    async def slow_chat(request: ChatRequest) -> LLMResponse:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        return auto_responder()(request, 0)

    class SlowClient:
        def __init__(self) -> None:
            self.count = 0

        async def achat(self, request: ChatRequest) -> LLMResponse:
            self.count += 1
            return await slow_chat(request)

        async def aclose(self) -> None:
            return None

    game = GameConfig()
    game.agent.max_concurrency = 2
    policy = LLMPolicy(
        {"mock": SlowClient()},  # type: ignore[dict-item]
        {seat: SeatSpec(provider="mock", model="mock") for seat in wolves},
        game,
    )
    policy.decide_all(state, legal)
    assert peak <= 2
    assert peak >= 2  # 3 个席位、上限 2，说明确实并发了而不是串行


# --- 兜底 -----------------------------------------------------------------


def test_fallback_decision_is_marked_and_recorded() -> None:
    legal = [LegalAction(seat=1, type=ActionType.VOTE, allowed_targets=[2, 3])]
    client = MockClient([tool_call("vote", seat=9) for _ in range(4)])
    policy = LLMPolicy(
        {"mock": client},
        {1: SeatSpec(provider="mock", model="mock")},
        GameConfig(),
    )
    decision = policy.decide_all(make_state(), legal)[0]
    assert decision.llm is not None and decision.llm.fallback is True
    assert decision.action.target == 2  # 确定性兜底取最小座位号
    types = [e.type for e in decision.side_events]
    assert types[-1] == "action_fallback"
    assert types.count("action_rejected") == 4  # 4 次非法动作各留一条痕
    assert decision.side_events[-1].payload["reason"]
    # 异常类事件也必须带展示文本与席位
    assert all(e.render is not None and e.render.text for e in decision.side_events)
    assert decision.side_events[-1].render is not None
    assert "1 号 走兜底" in decision.side_events[-1].render.text


def test_specs_from_models_resolves_provider_and_thinking() -> None:
    """席位 → 模型名要能解析出 provider、三层合并后的 CoT 策略与私有开关。

    这条不涉及任何网络调用，只验证配置解析。
    """
    models = ModelsConfig.model_validate(
        {
            "providers": {
                "deepseek": {
                    "base_url": "https://api.deepseek.com/v1",
                    "api_key_env": "DEEPSEEK_API_KEY",
                    "models": [
                        {"name": "deepseek-chat"},
                        {
                            "name": "deepseek-reasoner",
                            "thinking": {"enabled": True, "replay": True},
                        },
                    ],
                },
                "qwen": {
                    "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                    "api_key_env": "DASHSCOPE_API_KEY",
                    "models": [
                        {
                            "name": "qwen3.8-max",
                            "thinking": {
                                "enabled": True,
                                "extra_body": {"preserve_thinking": True},
                            },
                        }
                    ],
                },
            }
        }
    )
    assert specs_from_models(models, {1: "deepseek-chat"})[1].thinking.enabled is False
    reasoner = specs_from_models(models, {2: "deepseek-reasoner"})[2]
    assert reasoner.provider == "deepseek"
    assert reasoner.thinking.enabled is True and reasoner.thinking.replay is True
    qwen = specs_from_models(models, {3: "qwen3.8-max"})[3]
    assert qwen.provider == "qwen"
    assert qwen.extra_body == {"preserve_thinking": True}

    # 抄错模型名是最常见的手误，必须给出「可用模型」清单而不是裸的 KeyError
    with pytest.raises(ConfigError) as excinfo:
        specs_from_models(models, {1: "不存在的模型"})
    assert "不存在的模型" in str(excinfo.value)
    assert "deepseek-chat" in str(excinfo.value) and "qwen3.8-max" in str(excinfo.value)


def test_mock_client_records_requests_for_isolation_assertions() -> None:
    client = MockClient([speech("hi")])
    ctx = make_ctx()
    outcome = run(client, ctx, make_state(), [LegalAction(seat=1, type=ActionType.SPEAK)])
    assert client.call_count == 1
    # 发言阶段不再暴露 speak 工具；模型直接说话即动作。
    assert outcome.action == Action(actor=1, type=ActionType.SPEAK, message="hi")
    assert "speak" not in client.tool_names()


# --- 板子配置进入 prompt ----------------------------------------------------


def test_system_prompt_publishes_the_board() -> None:
    """本局板子的公开配置必须写进 system prompt——模型不该靠猜规则。

    实测中有模型在没有警长的板子上脑补出「警上争夺警徽」，根因就是没人告诉过它
    本局的板子长什么样。
    """
    prompt = system_prompt(1, Player(seat=1, role=Role.VILLAGER), make_state())

    assert "# 本局板子" in prompt
    assert (
        "9 人局：3 狼人、1 预言家、1 女巫、1 猎人、3 平民。当前规则下无警长和警徽竞选。" in prompt
    )
    assert "不公布死因" in prompt
    assert "座位号 1 到 9" in prompt


def test_board_brief_follows_config() -> None:
    """板子一改，prompt 里的规则必须跟着改（不能写死一套默认规则）。"""
    default_prompt = system_prompt(1, Player(seat=1, role=Role.WEREWOLF), make_state())

    board = BoardConfig()
    board.vote.secret_ballot = False
    board.day.speech_max_chars = 120
    board.roles.werewolf = 4
    tweaked = GameState(game_id="t", players=make_state().players, board=board)
    tweaked_prompt = system_prompt(1, Player(seat=1, role=Role.WEREWOLF), tweaked)

    assert "同时投票" in default_prompt and "看不到别人投给谁" in default_prompt
    assert "依次公开投票" in tweaked_prompt and "别人能看到你投给谁" in tweaked_prompt
    assert "300 字" in default_prompt and "120 字" in tweaked_prompt
    assert "9 人局" in default_prompt and "10 人局" in tweaked_prompt


def test_board_section_is_identical_for_every_seat() -> None:
    """板子是公开事实，所有席位的这一段必须一字不差（防止夹带只该某人知道的信息）。"""
    state, _ = RuleEngine(BoardConfig(), seed=3).new_game()
    sections = {
        system_prompt(seat, player, state).split("# 本局板子\n")[1].split("\n\n# 与你相关的规则")[0]
        for seat, player in state.players.items()
    }
    assert len(sections) == 1


# --- 阶段指令必须点名工具 ---------------------------------------------------


def test_speaking_phases_instruct_direct_speech_without_a_tool() -> None:
    """发言类阶段的指令要模型「直接发言」，且不再点名 speak 工具。

    speak 工具已被移除：模型直接把要说的话写进回复即视为发言动作
    （见 ``test_speaking_phase_turns_plain_text_into_the_speech_action``）。
    这几条指令若还留着「用 speak 发言」，等于让模型去调一个不存在的工具。
    """
    state = make_state(roles={1: Role.WEREWOLF, 2: Role.VILLAGER, 3: Role.VILLAGER})
    for phase in (
        Phase.NIGHT_WOLF_CHAT,
        Phase.NIGHT_WOLF_RUNOFF_SPEECH,
        Phase.DAY_DISCUSSION,
        Phase.DAY_RUNOFF_SPEECH,
    ):
        state.phase = phase
        text = phase_instruction(state, 1, [])
        assert "用 speak" not in text, f"{phase.value} 的指令还在点名 speak 工具：{text}"
        assert "发言" in text or "说出" in text, f"{phase.value} 的指令没有让模型发言：{text}"


# --- 上下文压缩（M8） -------------------------------------------------------


def _event(
    seq: int, type_: str, *, day: int = 1, actor: int | None = None, **payload: object
) -> Event:
    return Event(
        seq=seq,
        game_id="t",
        day=day,
        phase="day_discussion",
        type=type_,
        visibility=Visibility.PUBLIC,
        actor=actor,
        payload=payload,
    )


def _retained_lines(ctx: AgentContext) -> int:
    """统计仍留在历史里的「自上次行动以来」事件行数。"""
    total = 0
    for message in ctx.snapshot():
        content = message.get("content") or ""
        marker = "自上次行动以来：\n"
        if marker not in content:
            continue
        block = content.split(marker, 1)[1].split("\n\n", 1)[0]
        total += len([line for line in block.split("\n") if line])
    return total


def test_public_history_window_drops_oldest_and_keeps_digest() -> None:
    """超出窗口时：最早回合的事件文本被丢弃，死亡公告与首句进入「前情提要」。"""
    state = make_state()
    ctx = make_ctx(max_public_events=3)

    ctx.observe(
        [
            _event(1, "speech", actor=2, message="我是第一个发言的"),
            _event(2, "speech", actor=3, message="我是第二个发言的"),
            _event(3, "death", seat=3),
        ],
        state,
    )
    ctx.begin_turn(state, [2, 3])

    ctx.observe(
        [
            _event(4, "speech", actor=2, message="第二轮讨论来了"),
            _event(5, "speech", actor=1, message="我也说两句"),
        ],
        state,
    )
    ctx.begin_turn(state, [2])

    blob = json.dumps(ctx.snapshot(), ensure_ascii=False)
    assert "我是第二个发言的" not in blob  # 非摘要事件被彻底丢弃
    assert "前情提要" in blob  # 插入了模板摘要
    assert "3 号出局" in blob  # 死亡公告进摘要
    assert "我是第一个发言的" in blob  # 每轮首句进摘要
    assert "我也说两句" in blob  # 最新回合完整保留
    assert ctx.messages[0]["role"] == "system"
    assert "前情提要" in ctx.messages[1]["content"]  # 摘要紧跟 system


def test_public_history_stays_within_window() -> None:
    """无论跑多少回合，保留的公开事件行数都不超过 max_public_events。"""
    state = make_state()
    ctx = make_ctx(max_public_events=4)
    for i in range(10):
        ctx.observe([_event(100 + i, "speech", actor=2, message=f"发言{i}")], state)
        ctx.begin_turn(state, [2])
    assert 1 <= _retained_lines(ctx) <= 4


def test_public_history_window_disabled_when_non_positive() -> None:
    """max_public_events <= 0 表示不压缩，全部保留。"""
    state = make_state()
    ctx = make_ctx(max_public_events=0)
    for i in range(5):
        ctx.observe([_event(200 + i, "speech", actor=2, message=f"第{i}条")], state)
        ctx.begin_turn(state, [2])
    blob = json.dumps(ctx.snapshot(), ensure_ascii=False)
    assert "前情提要" not in blob
    for i in range(5):
        assert f"第{i}条" in blob


# --- 异常提示信号（M8） -----------------------------------------------------


def test_replay_downgrade_is_emitted_as_event() -> None:
    """CoT 回传被拒 → 剥离重试，并在事件里留一条 llm_downgrade（观众可见）。"""
    legal = [LegalAction(seat=1, type=ActionType.VOTE, allowed_targets=[2, 3])]
    seen = {"n": 0}

    def responder(_request: ChatRequest, _index: int) -> LLMResponse:
        seen["n"] += 1
        if seen["n"] == 1:
            raise LLMError("Error code: 400 - reasoning_content is not allowed", status_code=400)
        return tool_call("vote", seat=2)

    ctx = make_ctx(thinking=ThinkingConfig(enabled=True, replay=True))
    outcome = run(MockClient(responder), ctx, make_state(), legal)

    assert outcome.action is not None
    assert [e.type for e in outcome.pending_events] == ["llm_downgrade"]
    assert outcome.pending_events[0].visibility == Visibility.SPECTATOR
    assert ctx.thinking.replay is False  # 本 agent 的 replay 被降级


class _Boom(Exception):
    def __init__(self, status: int) -> None:
        super().__init__("boom")
        self.status_code = status


def _fake_sdk_response() -> SimpleNamespace:
    message = SimpleNamespace(
        content="hi",
        tool_calls=None,
        model_dump=lambda **_: {"role": "assistant", "content": "hi"},
    )
    return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=None)


def _sdk_stub(create: Any) -> SimpleNamespace:
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def _achat_with_injected_sdk(client: OpenAICompatClient, stub: SimpleNamespace) -> LLMResponse:
    """注入替身 SDK 客户端后跑一次调用。

    生产代码**按事件循环**缓存 SDK 客户端（见 ``OpenAICompatClient._sdk``），所以注入必须
    发生在 running loop 里；在 loop 外赋值会被当成「上一批留下的客户端」而被换掉。
    """

    async def go() -> LLMResponse:
        client._client = stub  # type: ignore[assignment]
        client._loop = asyncio.get_running_loop()
        return await client.achat(ChatRequest(model="m", messages=[]))

    return asyncio.run(go())


def test_network_retries_are_counted(monkeypatch: pytest.MonkeyPatch) -> None:
    """客户端自管重试：可重试错误重试后成功，并把次数记进响应（DESIGN §7.7）。"""
    monkeypatch.setattr("agents.client._backoff_seconds", lambda _n: 0.0)
    client = OpenAICompatClient(base_url="http://x", api_key="k", network_retries=3)

    class _Flaky:
        def __init__(self) -> None:
            self.attempts = 0

        async def create(self, **_kwargs: Any) -> Any:
            self.attempts += 1
            if self.attempts < 3:
                raise _Boom(429)
            return _fake_sdk_response()

    flaky = _Flaky()
    response = _achat_with_injected_sdk(client, _sdk_stub(flaky.create))
    assert response.network_retries == 2
    assert flaky.attempts == 3


def test_non_retryable_error_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """400 这类客户端错误不重试，直接抛出（避免把非法请求重发一遍）。"""
    monkeypatch.setattr("agents.client._backoff_seconds", lambda _n: 0.0)
    client = OpenAICompatClient(base_url="http://x", api_key="k", network_retries=3)

    class _AlwaysFail:
        def __init__(self) -> None:
            self.attempts = 0

        async def create(self, **_kwargs: Any) -> Any:
            self.attempts += 1
            raise _Boom(400)

    failing = _AlwaysFail()
    stub = _sdk_stub(failing.create)
    with pytest.raises(LLMError):
        _achat_with_injected_sdk(client, stub)
    assert failing.attempts == 1


def test_programming_error_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """没有状态码 ≠ 网络问题：编程错误必须立刻炸出来，不能被重试掩盖。

    真实教训：编排层每个阶段一次 ``asyncio.run``，而 SDK 客户端的连接池绑在创建它的
    loop 上，跨 loop 复用会抛 ``RuntimeError: Event loop is closed``（status 为 None）。
    旧逻辑「没状态码就重试」把它当成网络抖动，一局 9 人局刷出 25 次假重试，
    观战界面上全显示成橙色「重试」，看起来像网络差。
    """
    monkeypatch.setattr("agents.client._backoff_seconds", lambda _n: 0.0)
    client = OpenAICompatClient(base_url="http://x", api_key="k", network_retries=3)

    class _Broken:
        def __init__(self) -> None:
            self.attempts = 0

        async def create(self, **_kwargs: Any) -> Any:
            self.attempts += 1
            raise RuntimeError("Event loop is closed")

    broken = _Broken()
    stub = _sdk_stub(broken.create)
    with pytest.raises(LLMError):
        _achat_with_injected_sdk(client, stub)
    assert broken.attempts == 1  # 只试了一次，没有重试


def test_sdk_client_is_rebuilt_when_the_event_loop_changes() -> None:
    """跨事件循环换一套 SDK 客户端：httpx 连接池不能跟着 loop 一起被带走。

    这是上面那个坑的正面守卫——不是「重试成功了」，而是「根本不该失败」。
    """
    client = OpenAICompatClient(base_url="http://x", api_key="k")

    async def grab() -> tuple[int, Any]:
        return id(client._sdk()), client._loop

    first_id, first_loop = asyncio.run(grab())
    second_id, second_loop = asyncio.run(grab())
    assert first_loop is not second_loop
    assert first_id != second_id  # 换了 loop 就换了客户端

    async def twice() -> bool:
        return client._sdk() is client._sdk()

    assert asyncio.run(twice())  # 同一个 loop 内不会反复重建


class _RetryCountClient:
    """固定返回一个 network_retries=2 的响应，用于验证 meta 透传。"""

    async def achat(self, _request: ChatRequest) -> LLMResponse:
        return LLMResponse(
            tool_calls=[ToolCall(id="c", name="vote", arguments={"seat": 2})],
            raw={"role": "assistant", "content": None},
            network_retries=2,
        )

    async def aclose(self) -> None:
        return None


def test_llm_meta_records_network_retries() -> None:
    legal = [LegalAction(seat=1, type=ActionType.VOTE, allowed_targets=[2, 3])]
    policy = LLMPolicy(
        {"mock": _RetryCountClient()},  # type: ignore[dict-item]
        {1: SeatSpec(provider="mock", model="mock")},
        GameConfig(),
    )
    decision = policy.decide_all(make_state(), legal)[0]
    assert decision.llm is not None and decision.llm.network_retries == 2


# ---------------------------------------------------------------------------
# render_event 文本（女巫/狼人刀口修正，见 DESIGN §14 v0.25）
# ---------------------------------------------------------------------------


def _ev(type_: str, payload: dict, actor: int | None = None) -> Event:
    return Event(
        seq=0,
        game_id="t",
        day=1,
        phase="night",
        type=type_,
        visibility=Visibility.PUBLIC,
        actor=actor,
        payload=payload,
    )


def test_render_witch_save_includes_seat() -> None:
    # 旧实现丢掉了 seat，女巫后期会写成「我救人了但不知道救的是谁」。
    text = render_event(_ev("witch_save", {"seat": 9}, actor=9))
    assert text is not None
    assert "9 号" in text
    assert "解药" in text


def test_render_witch_poison_includes_seat() -> None:
    text = render_event(_ev("witch_poison", {"seat": 6}, actor=5))
    assert text is not None
    assert "6 号" in text
    assert "毒" in text


def test_render_witch_pass_says_no_potion() -> None:
    text = render_event(_ev("witch_pass", {}, actor=5))
    assert text is not None
    assert "没有用药" in text


def test_render_wolf_kill_shows_saved_status() -> None:
    # 带上 saved 状态，狼人才能把「我们刀了 X」和「平安夜」对上。
    saved = render_event(_ev("wolf_kill", {"seat": 9, "saved": True}))
    assert saved is not None and "9 号" in saved and "被女巫救下" in saved
    dead = render_event(_ev("wolf_kill", {"seat": 9, "saved": False}))
    assert dead is not None and "已出局" in dead
    pending = render_event(_ev("wolf_kill", {"seat": 9}))
    assert pending is not None and "9 号" in pending
    assert "被女巫救下" not in pending and "已出局" not in pending
    empty = render_event(_ev("wolf_kill", {}))
    assert empty == "今晚空刀。"


def test_digest_types_retain_witch_actions() -> None:
    # 女巫的用药结果必须进摘要，否则压缩后她会彻底忘记自己救/毒了谁。
    for kind in ("witch_save", "witch_poison", "witch_pass"):
        assert kind in DIGEST_TYPES


def test_wolf_chat_instruction_tells_no_lie_to_teammates() -> None:
    state = make_state()
    state.phase = Phase.NIGHT_WOLF_CHAT
    text = phase_instruction(state, 1, [])
    assert "无需对内撒谎" in text
    assert "队友" in text


def test_witch_situation_reports_remaining_potions() -> None:
    """女巫每回合的局面摘要都要写明剩余药剂，否则她会以为解药还在（真实踩过）。"""
    state = make_state(roles={1: Role.WITCH, 2: Role.VILLAGER, 3: Role.VILLAGER})
    text = situation_text(state, 1)
    assert "解药可用" in text and "毒药可用" in text

    state.potions.antidote_left = False
    text2 = situation_text(state, 1)
    assert "解药已用掉" in text2 and "毒药可用" in text2

    # 非女巫席位不该看到任何药剂信息
    assert "药剂" not in situation_text(state, 2)


def test_witch_instruction_lists_only_available_potions() -> None:
    """第二夜起只有真正还能用的药才出现在指令里，解药已用就明说、不再劝人救人。"""
    state = make_state(roles={1: Role.WITCH, 2: Role.VILLAGER, 3: Role.VILLAGER})
    state.phase = Phase.NIGHT_WITCH
    state.night.kill_target = 2  # 今夜刀口，满药时应能救

    full = phase_instruction(state, 1, [])
    assert "witch_save" in full and "witch_poison" in full and "witch_pass" in full

    state.potions.antidote_left = False
    used = phase_instruction(state, 1, [])
    assert "witch_save" not in used
    assert "无法救人" in used
    assert "witch_pass" in used
