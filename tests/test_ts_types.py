"""前端 TS 类型必须与后端 Pydantic 模型保持同步（DESIGN.md §10.4）。

``web/src/api/types.ts`` 是**生成物**：改了 ``engine/events.py`` 或 ``server/schemas.py``
里的模型却忘了重新生成，跑 ``uv run pytest`` 就会在这里失败。这是防止「前后端类型
静默漂移」的闸门 —— 手写两份类型的时代靠人盯，现在靠测试。
"""

from __future__ import annotations

from scripts.gen_ts_types import OUTPUT, generate, main

# 前端 import 的类型名，一个都不能少
EXPECTED_TYPES = (
    "Visibility",
    "RenderText",
    "Thinking",
    "LlmMeta",
    "Event",
    "CreateGameRequest",
    "CreateGameResponse",
    "GameListItem",
    "GameMeta",
    "WsDone",
    "WsError",
    "WsMessage",
)


def test_types_ts_matches_backend_models() -> None:
    """磁盘上的 types.ts 必须与当前后端模型生成的内容逐字一致。"""
    expected = generate()
    current = OUTPUT.read_text(encoding="utf-8").replace("\r\n", "\n")
    assert current == expected, (
        f"{OUTPUT.name} 与后端模型不同步，请运行：uv run python -m scripts.gen_ts_types"
    )


def test_check_mode_passes_when_in_sync() -> None:
    """``--check`` 是 CI 用的路径：同步时必须返回 0。"""
    assert main(["--check"]) == 0


def test_generated_types_cover_api_contract() -> None:
    text = generate()
    for name in EXPECTED_TYPES:
        assert f"export interface {name} " in text or f"export type {name} " in text, (
            f"生成结果缺少类型：{name}"
        )


def test_generated_types_have_no_raw_schema_leaks() -> None:
    """转换必须彻底：不该把 JSON Schema 的 $ref / $defs 原样漏进 TS。"""
    text = generate()
    assert "$ref" not in text
    assert "$defs" not in text
