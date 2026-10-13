"""``meta.json``：一局对局的配置快照与结果，用于事后对照。

对局本身不承诺可复现（LLM 输出不确定），但规则侧的输入必须留痕，
否则复盘时无法判断「这一局为什么打成这样」。
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from config import BoardConfig
from engine.state import ROLE_LABELS, GameState, Role

# 与 pyproject.toml 的 version 保持一致；项目以仓库根为导入根运行，不打包安装
ENGINE_VERSION = "0.1.0"


def board_hash(board: BoardConfig) -> str:
    """板子配置的指纹。板子变了但结论混在一起统计会失真，所以要留痕。"""
    digest = hashlib.sha256(board.model_dump_json().encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def build_meta(
    state: GameState,
    *,
    started_at: datetime,
    finished_at: datetime | None = None,
    models: dict[str, str] | None = None,
    prompt_version: str | None = None,
    temperature: float | None = None,
) -> dict:
    """组装 ``meta.json`` 的内容。"""
    return {
        "game_id": state.game_id,
        "started_at": started_at.isoformat(),
        "finished_at": (finished_at or datetime.now(UTC)).isoformat(),
        "seed": state.seed,
        "engine_version": ENGINE_VERSION,
        "prompt_version": prompt_version,
        "board_hash": board_hash(state.board),
        "models": models or {},
        "temperature": temperature,
        "winner": state.winner.value if state.winner else None,
        "final_roles": {str(s): p.role.value for s, p in sorted(state.players.items())},
        "survivors": sorted(s for s, p in state.players.items() if p.alive),
        "days": state.day,
    }


def describe_roles(roles: dict[str, str]) -> str:
    """把 ``{"1": "werewolf"}`` 渲染成中文一行，给终端摘要用。"""
    return " ".join(f"{seat}={ROLE_LABELS.get(Role(role), role)}" for seat, role in roles.items())
