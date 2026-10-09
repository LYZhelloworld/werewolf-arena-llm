"""脚本化 LLM 客户端，用于开发与单测。

本项目开发阶段**不碰真实 API key**（用户明确选择），所有链路验证都靠它。
它把每次请求都记下来，测试可以据此断言「发给模型的东西里没有别人的 CoT」。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from agents.client import ChatRequest, LLMClient, LLMError, LLMResponse, ToolCall

# responder(request, call_index) -> LLMResponse
Responder = Callable[[ChatRequest, int], LLMResponse]


class MockClient:
    """按脚本返回响应。

    ``responder`` 可以是响应列表（按调用顺序消费），也可以是函数。
    省略时默认「既不说话也不调工具」，用来测兜底那条路径。
    """

    def __init__(self, responder: Responder | list[LLMResponse] | None = None) -> None:
        self.calls: list[ChatRequest] = []
        if isinstance(responder, list):
            queue = list(responder)

            def from_queue(_request: ChatRequest, _index: int) -> LLMResponse:
                if not queue:
                    raise LLMError("MockClient 的脚本已用尽")
                return queue.pop(0)

            self._responder: Responder = from_queue
        else:
            self._responder = responder or (lambda _request, _index: LLMResponse())

    async def achat(self, request: ChatRequest) -> LLMResponse:
        index = len(self.calls)
        self.calls.append(request)
        return self._responder(request, index)

    async def aclose(self) -> None:
        return None

    # --- 测试用的便利访问 -------------------------------------------------

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def last_request(self) -> ChatRequest:
        return self.calls[-1]

    def all_text(self) -> str:
        """把历史所有请求拼成一个字符串，供「不该出现什么」这类断言使用。"""
        return json.dumps([c.messages for c in self.calls], ensure_ascii=False)

    def tool_names(self, index: int = 0) -> list[str]:
        return [t.get("function", {}).get("name", "") for t in self.calls[index].tools]


AUX_TOOLS = frozenset({"memory_write", "memory_read", "think"})


def auto_responder(default_text: str = "我先观察一轮，暂时没有明确判断。") -> Responder:
    """一个「总是选第一个合法动作」的脚本化响应器。

    让整条链路在没有 API key 的情况下也能跑完整局，用于验证编排、
    工具协议、CoT 捕获与持久化，而不是验证模型能力。
    """

    def respond(request: ChatRequest, _index: int) -> LLMResponse:
        action_tool: dict[str, Any] | None = None
        for tool in request.tools:
            name = tool.get("function", {}).get("name", "")
            if name in AUX_TOOLS:
                continue
            action_tool = tool
            break
        # 发言类阶段没有动作工具（speak 已被移除）：直接把要说的话作为发言返回。
        if action_tool is None:
            return speech(default_text)
        properties = action_tool.get("function", {}).get("parameters", {}).get("properties", {})
        arguments: dict[str, Any] = {}
        if "seat" in properties:
            seats = properties["seat"].get("enum") or []
            if not seats:
                return speech(default_text)
            arguments["seat"] = seats[0]
        return tool_call(action_tool["function"]["name"], **arguments)

    return respond


def tool_call(name: str, **arguments: Any) -> LLMResponse:
    """构造一次「只调一个工具」的响应。"""
    return LLMResponse(
        tool_calls=[ToolCall(id="call_1", name=name, arguments=arguments)],
        raw={"role": "assistant", "content": None},
    )


def speech(text: str, *, reasoning: str | None = None) -> LLMResponse:
    """构造一次「带正文发言」的响应；``reasoning`` 模拟原生推理字段。"""
    raw: dict[str, Any] = {"role": "assistant", "content": text}
    if reasoning is not None:
        raw["reasoning_content"] = reasoning
    return LLMResponse(content=text, raw=raw)


def make_client(client: MockClient) -> LLMClient:
    """让 mypy 把 MockClient 当成 LLMClient 用（Protocol 是结构子类型）。"""
    return client
