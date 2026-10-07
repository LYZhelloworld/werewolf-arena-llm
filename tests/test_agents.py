"""Agent 运行时单测：多轮工具循环、CoT 捕获与回传、记忆、兜底、并发。

全部用脚本化客户端，**不碰真实 API key**。
"""

from __future__ import annotations

import asyncio
import json
import re

import pytest

from agents.client import ChatRequest, LLMError, LLMResponse, ToolCall
from agents.context import AgentContext
from agents.loop import TurnParams, run_turn
from agents.mock import MockClient, auto_responder, tool_call
from agents.policy import LLMPolicy, SeatSpec, specs_from_models
from agents.prompts import system_prompt
from agents.tools import build_tools, call_to_action
from config import AgentMemoryConfig, BoardConfig, GameConfig, ModelsConfig, ThinkingConfig
from engine.actions import Action, ActionType, LegalAction
from engine.engine import RuleEngine
from engine.state import GameState, Player, Role
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
) -> AgentContext:
    state = state or make_state()
    return AgentContext(
        seat,
        state.players[seat] if role is None else Player(seat=seat, role=role),
        system_prompt(seat, Player(seat=seat, role=role), state),
        thinking=thinking or ThinkingConfig(),
        memory=memory or AgentMemoryConfig(),
        model="mock",
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
            tool_call("speak", message="我先记一笔。"),
        ]
    )
    ctx = make_ctx()
    outcome = run(client, ctx, make_state(), legal)

    assert outcome.action == Action(actor=1, type=ActionType.SPEAK, message="我先记一笔。")
    assert outcome.tool_turns == 2
    assert ctx.memories == {"怀疑": "2 号发言很急"}
    assert [e.type for e in outcome.pending_events] == ["memory_write"]


def test_memory_read_returns_written_entries() -> None:
    legal = [LegalAction(seat=1, type=ActionType.SPEAK)]
    client = MockClient(
        [
            tool_call("memory_write", key="k", content="v"),
            tool_call("memory_read"),
            tool_call("speak", message="想起来了。"),
        ]
    )
    ctx = make_ctx()
    outcome = run(client, ctx, make_state(), legal)
    assert outcome.tool_turns == 3
    assert [e.type for e in outcome.pending_events] == ["memory_write", "memory_read"]
    assert any("k：v" in json.dumps(m, ensure_ascii=False) for m in ctx.messages)


def test_memory_limit_is_enforced() -> None:
    legal = [LegalAction(seat=1, type=ActionType.SPEAK)]
    client = MockClient(
        [
            tool_call("memory_write", key="a", content="1"),
            tool_call("memory_write", key="b", content="2"),
            tool_call("speak", message="满了。"),
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


def test_model_that_only_talks_is_asked_to_call_tools() -> None:
    legal = [LegalAction(seat=1, type=ActionType.SPEAK)]
    client = MockClient([LLMResponse(content="我不服"), tool_call("speak", message="好吧")])
    outcome = run(client, make_ctx(), make_state(), legal)
    assert outcome.action == Action(actor=1, type=ActionType.SPEAK, message="好吧")
    assert any(
        "必须通过调用工具来行动" in json.dumps(m, ensure_ascii=False)
        for m in client.calls[1].messages
    )


# --- CoT -------------------------------------------------------------------


def test_cot_from_native_reasoning_field() -> None:
    legal = [LegalAction(seat=1, type=ActionType.SPEAK)]
    response = LLMResponse(
        content=None,
        tool_calls=[ToolCall(id="c", name="speak", arguments={"message": "我发言"})],
        raw={"role": "assistant", "content": None, "reasoning_content": "我心里想的是 2 号"},
    )
    outcome = run(MockClient([response]), make_ctx(), make_state(), legal)
    assert outcome.thinking.text == "我心里想的是 2 号"
    assert outcome.thinking.source == "reasoning_content"


def test_cot_falls_back_to_content() -> None:
    """没有原生推理字段时，正文就是 CoT（对无 reasoning 能力的模型）。"""
    legal = [LegalAction(seat=1, type=ActionType.SPEAK)]
    response = LLMResponse(
        content="我先想想：2 号很可疑",
        tool_calls=[ToolCall(id="c", name="speak", arguments={"message": "2 号很可疑"})],
        raw={"role": "assistant", "content": "我先想想：2 号很可疑"},
    )
    outcome = run(MockClient([response]), make_ctx(), make_state(), legal)
    assert outcome.thinking.source == "content"
    assert outcome.thinking.text == "我先想想：2 号很可疑"


def test_cot_from_think_tool() -> None:
    legal = [LegalAction(seat=1, type=ActionType.SPEAK)]
    client = MockClient(
        [tool_call("think", text="2 号保 3 号，可疑"), tool_call("speak", message="我怀疑 2 号")]
    )
    outcome = run(client, make_ctx(), make_state(), legal)
    assert outcome.thinking.text is not None and "可疑" in outcome.thinking.text
    assert outcome.thinking.source == "think_tool"


def test_replay_puts_cot_in_its_own_field_never_in_content() -> None:
    legal = [LegalAction(seat=1, type=ActionType.SPEAK)]
    response = LLMResponse(
        content="我发言",
        tool_calls=[ToolCall(id="c", name="speak", arguments={"message": "我发言"})],
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
        tool_calls=[ToolCall(id="c", name="speak", arguments={"message": "我发言"})],
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
        content=None,
        tool_calls=[ToolCall(id="c", name="speak", arguments={"message": "重来一次"})],
        raw={"role": "assistant", "content": None, "reasoning_content": "想了一遍"},
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
            content=None,
            tool_calls=[ToolCall(id="c", name="speak", arguments={"message": f"{seat} 号发言"})],
            raw={
                "role": "assistant",
                "content": None,
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
            content=None,
            tool_calls=[ToolCall(id="c", name="speak", arguments={"message": "刀他"})],
            raw={"role": "assistant", "content": None, "reasoning_content": f"想-{seat}"},
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
        return tool_call("speak", message="收到")

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
    assert [e.type for e in decision.side_events] == ["action_fallback"]
    assert decision.side_events[0].payload["reason"]


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

    with pytest.raises(KeyError):
        specs_from_models(models, {1: "不存在的模型"})


def test_mock_client_records_requests_for_isolation_assertions() -> None:
    client = MockClient([tool_call("speak", message="hi")])
    ctx = make_ctx()
    run(client, ctx, make_state(), [LegalAction(seat=1, type=ActionType.SPEAK)])
    assert client.call_count == 1
    assert "speak" in client.tool_names()
