"""OpenAI 兼容协议的 LLM 客户端。

只走 OpenAI 兼容协议：换 provider 只是换 ``base_url`` + ``api_key`` + ``model``，
**不为任何一家写原生 adapter**（DESIGN.md §7.1）。各家的私有开关一律由
``extra_body`` 透传，代码里不出现 provider 名字。

客户端只负责「发请求 + 把响应归一化为内部结构」，不做 CoT 语义判断——
那是 :mod:`agents.thinking` 的事。
"""

from __future__ import annotations

import json
import time
from typing import Any, Protocol

from openai import NOT_GIVEN, AsyncOpenAI
from pydantic import BaseModel, Field


class ToolCall(BaseModel):
    """一次工具调用。``arguments`` 已解析成 dict，解析失败时为空 dict。"""

    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class LLMResponse(BaseModel):
    """归一化响应。

    ``raw`` 是完整的 assistant 消息字典（含各家私有字段），CoT 捕获靠它。
    内部结构只有一套，出口序列化才按 provider 配置分叉（DESIGN.md §7.1）。
    """

    content: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    raw: dict[str, Any] = Field(default_factory=dict)
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    latency_ms: int = 0


class ChatRequest(BaseModel):
    """一次对话请求。``messages`` 里的额外字段（如 CoT）会原样发给 provider。"""

    model: str
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]] = Field(default_factory=list)
    temperature: float = 0.8
    max_tokens: int = 1024
    extra_body: dict[str, Any] = Field(default_factory=dict)


class LLMError(RuntimeError):
    """LLM 调用失败。``status_code`` 用于区分「回传被拒」与「网络抖动」。"""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class LLMClient(Protocol):
    """客户端协议。测试用 :class:`agents.mock.MockClient` 顶替。"""

    async def achat(self, request: ChatRequest) -> LLMResponse: ...


class OpenAICompatClient:
    """AsyncOpenAI 的薄封装。"""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout_s: float = 60.0,
        network_retries: int = 3,
    ) -> None:
        self.base_url = base_url
        # 网络层重试交给 SDK；动作非法重试在工具循环里单独计数（DESIGN.md §9.1）
        self._client = AsyncOpenAI(
            base_url=base_url,
            api_key=api_key,
            timeout=timeout_s,
            max_retries=network_retries,
        )

    async def achat(self, request: ChatRequest) -> LLMResponse:
        started = time.perf_counter()
        try:
            response = await self._client.chat.completions.create(
                model=request.model,
                messages=request.messages,  # type: ignore[arg-type]
                tools=request.tools if request.tools else NOT_GIVEN,  # type: ignore[arg-type]
                temperature=request.temperature,
                max_tokens=request.max_tokens,
                extra_body=request.extra_body or None,
            )
        except Exception as exc:  # SDK 的异常类型很多，统一收敛成 LLMError
            status = getattr(exc, "status_code", None)
            raise LLMError(f"{type(exc).__name__}: {exc}", status_code=status) from exc

        message = response.choices[0].message
        raw = message.model_dump(exclude_none=True)
        # 各家 CoT 字段未必在 SDK 的声明字段里，从 extras 里捞出来一并保留
        extras = getattr(message, "model_extra", None) or {}
        for key, value in extras.items():
            raw.setdefault(key, value)

        usage = getattr(response, "usage", None)
        calls: list[ToolCall] = []
        for call in message.tool_calls or []:
            function = getattr(call, "function", None)
            if function is None:  # 自定义工具调用不走本项目协议，跳过
                continue
            calls.append(
                ToolCall(
                    id=call.id,
                    name=function.name,
                    arguments=_parse_arguments(function.arguments),
                )
            )
        return LLMResponse(
            content=message.content,
            tool_calls=calls,
            raw=raw,
            prompt_tokens=getattr(usage, "prompt_tokens", None),
            completion_tokens=getattr(usage, "completion_tokens", None),
            latency_ms=int((time.perf_counter() - started) * 1000),
        )

    async def aclose(self) -> None:
        await self._client.close()


def _parse_arguments(raw: str | None) -> dict[str, Any]:
    """工具参数是 JSON 字符串。模型写坏了就当空 dict，交给校验环节报错重试。"""
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}
