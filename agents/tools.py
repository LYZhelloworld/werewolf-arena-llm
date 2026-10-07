"""工具协议：把 :class:`LegalAction` 翻译成 OpenAI function schema，反之亦然。

工具名就是 :class:`ActionType` 的枚举值——英文，因为要过模型与前端两层。
描述与错误原因一律中文（AGENTS.md §2）。
"""

from __future__ import annotations

from typing import Any

from agents.client import ToolCall
from engine.actions import Action, ActionType, LegalAction

MEMORY_WRITE = "memory_write"
MEMORY_READ = "memory_read"
THINK = "think"

# 需要指定目标的动作 / 需要发言文本的动作
TARGET_TYPES = frozenset(
    {
        ActionType.WOLF_VOTE_KILL,
        ActionType.SEER_CHECK,
        ActionType.WITCH_POISON,
        ActionType.HUNTER_SHOOT,
        ActionType.VOTE,
    }
)
MESSAGE_TYPES = frozenset({ActionType.SPEAK, ActionType.LAST_WORDS, ActionType.REFLECT})

ACTION_DESCRIPTIONS: dict[ActionType, str] = {
    ActionType.SPEAK: "公开发言。说你想说的话，可以指控、辩护、伪装或沉默带过。",
    ActionType.WOLF_VOTE_KILL: "选择今晚要袭击的玩家。只有狼人能看到你的选择。",
    ActionType.SEER_CHECK: "选择今晚要查验的玩家。结果只有你自己知道。",
    ActionType.WITCH_SAVE: "用解药救下今晚被狼人袭击的玩家。",
    ActionType.WITCH_POISON: "用毒药毒死一名玩家。",
    ActionType.WITCH_PASS: "今夜不使用任何药。",
    ActionType.HUNTER_SHOOT: "开枪带走一名玩家。",
    ActionType.HUNTER_HOLD: "放弃开枪。",
    ActionType.VOTE: "投票放逐一名玩家。",
    ActionType.ABSTAIN: "弃权，本轮不投给任何人。",
    ActionType.LAST_WORDS: "留下你的遗言，所有存活玩家都会听到。",
    ActionType.REFLECT: "发表你对这局比赛的感想。",
}

__all__ = [
    "build_tools",
    "call_to_action",
    "is_memory_tool",
    "is_think_tool",
    "memory_tools",
    "think_tool",
]


def _schema(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        },
    }


def action_tools(legal: list[LegalAction]) -> list[dict[str, Any]]:
    """当前阶段允许的动作工具。引擎没给的动作不会出现（没得毒就不给毒药）。"""
    tools: list[dict[str, Any]] = []
    for spec in legal:
        description = ACTION_DESCRIPTIONS.get(spec.type, spec.type.value)
        if spec.type in TARGET_TYPES:
            seats = spec.allowed_targets
            tools.append(
                _schema(
                    spec.type.value,
                    f"{description} 可选座位：{_seat_list(seats)}。",
                    {
                        "seat": {
                            "type": "integer",
                            "description": f"目标座位号，必须是 {_seat_list(seats)} 之一",
                            "enum": seats,
                        }
                    },
                    ["seat"],
                )
            )
        elif spec.type in MESSAGE_TYPES:
            tools.append(
                _schema(
                    spec.type.value,
                    description,
                    {
                        "message": {
                            "type": "string",
                            "description": "你要说的话，中文，口语化，不超过 300 字",
                        }
                    },
                    ["message"],
                )
            )
        else:
            tools.append(_schema(spec.type.value, description, {}, []))
    return tools


def memory_tools() -> list[dict[str, Any]]:
    return [
        _schema(
            MEMORY_WRITE,
            "记住一条重要信息，供你后面几轮回想。同 key 会覆盖旧内容。",
            {
                "key": {"type": "string", "description": "短标签，如「怀疑3号」「2号是预言家」"},
                "content": {"type": "string", "description": "要记住的内容，尽量简短"},
            },
            ["key", "content"],
        ),
        _schema(MEMORY_READ, "读取你此前记下的全部内容。", {}, []),
    ]


def think_tool() -> dict[str, Any]:
    return _schema(
        THINK,
        "写下你的思考过程。这段内容只有观众能看到，其他玩家看不到。行动前请先调用它。",
        {"text": {"type": "string", "description": "你的推理过程，中文"}},
        ["text"],
    )


def build_tools(
    legal: list[LegalAction], *, memory_enabled: bool = True, require_think_tool: bool = False
) -> list[dict[str, Any]]:
    """本回合暴露给模型的全部工具。

    **不使用** ``tool_choice="required"``：必须先允许模型调记忆 / 思考工具（§7.2）。
    """
    tools = action_tools(legal)
    if require_think_tool:
        tools.append(think_tool())
    if memory_enabled:
        tools.extend(memory_tools())
    return tools


def is_memory_tool(name: str) -> bool:
    return name in (MEMORY_WRITE, MEMORY_READ)


def is_think_tool(name: str) -> bool:
    return name == THINK


def call_to_action(call: ToolCall, legal: list[LegalAction]) -> tuple[Action | None, str | None]:
    """把一次工具调用翻译成 :class:`Action`。

    返回 ``(None, 原因)`` 时，原因会作为 tool result 回给模型让它重选。
    """
    try:
        action_type = ActionType(call.name)
    except ValueError:
        return None, f"未知工具 {call.name}，请从当前可用的工具中选择一个。"

    spec = next((la for la in legal if la.type == action_type), None)
    if spec is None:
        return None, f"当前阶段不能调用 {call.name}，请从可选动作中选择。"

    seat = call.arguments.get("seat")
    message = call.arguments.get("message")

    if spec.type in TARGET_TYPES:
        if not isinstance(seat, int):
            return None, f"{call.name} 需要一个整数座位号。"
        if seat not in spec.allowed_targets:
            return None, (
                f"{seat} 号不是合法目标，请在 {_seat_list(spec.allowed_targets)} 中选择。"
            )
        return Action(actor=spec.seat, type=spec.type, target=seat), None

    if spec.type in MESSAGE_TYPES:
        if not isinstance(message, str) or not message.strip():
            return None, f"{call.name} 需要非空的消息内容。"
        return Action(actor=spec.seat, type=spec.type, message=message.strip()), None

    return Action(actor=spec.seat, type=spec.type), None


def _seat_list(seats: list[int]) -> str:
    return "、".join(str(s) for s in seats)
