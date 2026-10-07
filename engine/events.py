"""事件定义。事件流是唯一事实来源，直播与复盘共用同一份数据。"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class Visibility(StrEnum):
    """可见性。英文枚举值——前端 TS 要按字面量 switch。"""

    PUBLIC = "public"  # 所有人可见（发言、投票结果、遗言、感想、死亡公告）
    WOLF = "private:wolf"  # 仅狼人可见（夜间协商）
    SEER = "private:seer"  # 仅预言家可见（查验结果）
    WITCH = "private:witch"  # 仅女巫可见（刀口信息、用药记录）
    SELF = "private:self"  # 仅行动者本人可见（记忆读写）
    SPECTATOR = "spectator"  # 仅观众可见（真实身份底牌、全部 CoT、投票中间过程）


class RenderText(BaseModel):
    """前端直接用的展示文本。中文。"""

    text: str
    tone: str | None = None


class Thinking(BaseModel):
    """思考过程（CoT）。

    恒为观众可见：构建 agent 上下文时必须**物理剔除**本字段，
    而不是渲染后打码。见 DESIGN.md §6.2。
    """

    text: str | None = None
    # reasoning_content | content | think_tool | none
    source: str = "none"
    replayed: bool = False


class LlmMeta(BaseModel):
    """一次 LLM 调用的可观测信息（DESIGN.md §8.2）。

    放在 ``engine/events.py`` 而不是 ``agents/``：事件 schema 必须由引擎侧统一定义，
    且它只是纯数据，不含任何 LLM 依赖。``fallback`` 表示这一手是兜底出来的。
    """

    provider: str
    model: str
    latency_ms: int = 0
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    tool_turns: int = 0
    network_retries: int = 0
    fallback: bool = False


class Event(BaseModel):
    """一条事件。落盘为 JSON（jsonl 追加 / json 归档）。"""

    seq: int
    game_id: str
    ts: datetime = Field(default_factory=lambda: datetime.now(UTC))
    day: int
    phase: str
    type: str  # 英文枚举，见 DESIGN.md §8.3
    visibility: Visibility
    actor: int | None = None  # None 表示系统
    payload: dict[str, Any] = Field(default_factory=dict)
    thinking: Thinking | None = None
    llm: LlmMeta | None = None
    render: RenderText | None = None
