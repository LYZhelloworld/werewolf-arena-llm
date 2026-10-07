"""前端 TS 类型必须与后端 Pydantic 模型保持同步（DESIGN.md §10.4）。

``web/src/api/types.ts`` 是**生成物**：改了 ``engine/events.py`` 或 ``server/schemas.py``
里的模型却忘了重新生成，跑 ``uv run pytest`` 就会在这里失败。这是防止「前后端类型
静默漂移」的闸门 —— 手写两份类型的时代靠人盯，现在靠测试。
"""

from __future__ import annotations

from pathlib import Path

from engine.state import PHASE_LABELS, Phase
from scripts.gen_ts_types import OUTPUT, generate, main

DERIVE_TS = Path(__file__).resolve().parent.parent / "web" / "src" / "lib" / "derive.ts"

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


def _ts_string_map(source: str, name: str) -> dict[str, str]:
    """从 TS 源码里抠出一个 ``Record<string, string>`` 字面量。"""
    assert name in source, f"derive.ts 里找不到 {name}"
    body = source.split(name, 1)[1].split("};", 1)[0]
    pairs: dict[str, str] = {}
    for line in body.splitlines():
        stripped = line.strip()
        if ":" not in stripped or stripped.startswith("//"):
            continue
        key, _, value = stripped.partition(":")
        key = key.strip().strip('"')
        value = value.strip().rstrip(",").strip()
        if value.startswith('"') and value.endswith('"'):
            pairs[key] = value[1:-1]
    return pairs


def test_frontend_phase_labels_match_backend_verbatim() -> None:
    """derive.ts 的 PHASE_LABELS 必须与 ``engine/state.py`` 的**逐字一致**。

    阶段名是英文枚举、直接写进 ``event.phase``。UI 文案的唯一事实来源在 Python 侧
    （``phase_enter`` 事件的 ``render.text`` 也由它生成），TS 里没法 import 只能抄一份，
    所以这里同时比对 key 与 value：少一个 key 会让分隔条掉出英文原值，value 不一致
    则会出现「事件流里写『夜晚 · 狼人出刀』、分隔条写别的」这种前后矛盾。
    """
    ts_map = _ts_string_map(DERIVE_TS.read_text(encoding="utf-8"), "PHASE_LABELS")
    expected = dict(PHASE_LABELS)
    missing = sorted(set(expected) - set(ts_map))
    assert not missing, f"derive.ts PHASE_LABELS 缺少阶段标签：{missing}"
    extra = sorted(set(ts_map) - set(expected))
    assert not extra, f"derive.ts PHASE_LABELS 有后端已不存在的阶段：{extra}"
    mismatched = {k: (v, ts_map[k]) for k, v in expected.items() if ts_map[k] != v}
    assert not mismatched, f"derive.ts PHASE_LABELS 文案与后端不一致：{mismatched}"


def test_frontend_phase_labels_cover_every_phase_enum_value() -> None:
    """枚举成员一个都不能漏（防止 ``PHASE_LABELS`` 自己漏了某个 Phase）。"""
    assert {phase.value for phase in Phase} == set(PHASE_LABELS)
