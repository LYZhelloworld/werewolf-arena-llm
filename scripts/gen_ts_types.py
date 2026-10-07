"""从后端 Pydantic 模型生成前端 TS 类型（DESIGN.md §10.4）。

**单一事实来源是后端模型**：本脚本读取 ``engine/events.py`` 与 ``server/schemas.py``
里的模型，把它们的 JSON Schema 转成 ``web/src/api/types.ts``。前端不再手写类型：

    uv run python -m scripts.gen_ts_types            # 生成 / 覆盖
    uv run python -m scripts.gen_ts_types --check    # 只校验是否最新（不写盘）

``tests/test_ts_types.py`` 会在 ``uv run pytest`` 时断言两者同步 —— 改了后端模型却忘了
重新生成，测试直接失败。这是防止「前后端类型静默漂移」的那道闸。

为什么不用现成工具（如 json-schema-to-typescript）：这里只用到 schema 的一个小子集
（object / enum / const / anyOf / array / $ref / additionalProperties），自己转量小可控、
零额外依赖，输出风格也能固定成项目约定（英文标识符 + 中文注释）。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, TypeAdapter

from engine.events import Event, LlmMeta, RenderText, Thinking, Visibility
from server.schemas import (
    CreateGameRequest,
    CreateGameResponse,
    GameListItem,
    GameMeta,
    WsDone,
    WsError,
    WsMessage,
)

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "web" / "src" / "api" / "types.ts"

REF_PREFIX = "#/$defs/"
REF_TEMPLATE = REF_PREFIX + "{model}"

# 导出顺序 = 输出顺序，先基础类型（被引用的）再复合类型。TS 声明会提升，
# 顺序只影响可读性，不影响正确性。
EXPORTS: list[Any] = [
    Visibility,
    RenderText,
    Thinking,
    LlmMeta,
    Event,
    CreateGameRequest,
    CreateGameResponse,
    GameListItem,
    GameMeta,
    WsDone,
    WsError,
    WsMessage,
]

# 默认所有字段都「非可选」：后端 model_dump 会带上全部字段（含默认值），
# 因此响应 / 事件类型里不存在 undefined。只有请求体是「用户可省略字段」的，
# 才按 JSON Schema 的 required 决定要不要加 `?`。
RESPECT_REQUIRED = frozenset({"CreateGameRequest"})

# 联合类型单行排不下时的换行阈值
ALIAS_WIDTH = 88

BANNER = """\
// ⚠️ 本文件由脚本自动生成，请勿手改。
// 生成：uv run python -m scripts.gen_ts_types
// 事实来源：后端 Pydantic 模型（engine/events.py、server/schemas.py）。
// tests/test_ts_types.py 会校验本文件与后端是否同步（DESIGN.md §10.4）。"""


# ---------------------------------------------------------------------------
# JSON Schema -> TS
# ---------------------------------------------------------------------------


def _schema_of(obj: Any) -> dict[str, Any]:
    """取一个模型 / 枚举的 JSON Schema（子模型都进 ``$defs``，用名字引用）。"""
    if isinstance(obj, type) and issubclass(obj, BaseModel):
        return obj.model_json_schema(ref_template=REF_TEMPLATE)
    return TypeAdapter(obj).json_schema(ref_template=REF_TEMPLATE)


def _ref_name(ref: str) -> str:
    return ref.split("/")[-1]


def _literal(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _union(parts: list[str]) -> str:
    """按出现顺序去重后拼成联合类型。"""
    seen: list[str] = []
    for part in parts:
        if part not in seen:
            seen.append(part)
    return " | ".join(seen)


def _node_type(node: dict[str, Any]) -> str:
    """把 JSON Schema 的一个节点转成 TS 类型表达式。"""
    if "$ref" in node:
        return _ref_name(node["$ref"])
    if "enum" in node:
        return _union([_literal(v) for v in node["enum"]])
    if "const" in node:
        return _literal(node["const"])
    if "anyOf" in node:
        return _union([_node_type(sub) for sub in node["anyOf"]])
    if "oneOf" in node:
        return _union([_node_type(sub) for sub in node["oneOf"]])

    node_type = node.get("type")
    if isinstance(node_type, list):  # 形如 ["string", "null"]
        return _union([_node_type({**node, "type": item}) for item in node_type])

    if node_type == "array":
        item = _node_type(node.get("items", {}))
        return f"({item})[]" if " | " in item else f"{item}[]"

    if node_type == "object" or "properties" in node or "additionalProperties" in node:
        properties = node.get("properties")
        if properties:
            required = set(node.get("required", []))
            fields = []
            for name, sub in properties.items():
                opt = "" if name in required else "?"
                fields.append(f"{name}{opt}: {_node_type(sub)}")
            return "{ " + "; ".join(fields) + " }"
        extra = node.get("additionalProperties")
        if isinstance(extra, dict):
            return f"Record<string, {_node_type(extra)}>"
        return "Record<string, unknown>"

    if node_type in ("integer", "number"):
        return "number"
    if node_type == "string":
        return "string"
    if node_type == "boolean":
        return "boolean"
    if node_type == "null":
        return "null"
    return "unknown"


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------


def _doc(text: str | None, indent: str = "") -> list[str]:
    """把 description 渲染成 TS 注释；空描述返回空列表。"""
    if not text:
        return []
    lines = [line.strip().replace("*/", "*\\/") for line in text.strip().splitlines()]
    lines = [line for line in lines if line]
    if not lines:
        return []
    if len(lines) == 1:
        return [f"{indent}/** {lines[0]} */"]
    return [f"{indent}/**", *(f"{indent} * {line}" for line in lines), f"{indent} */"]


def _alias(name: str, union: str) -> list[str]:
    one_line = f"export type {name} = {union};"
    if len(one_line) <= ALIAS_WIDTH:
        return [one_line]
    parts = union.split(" | ")
    lines = [f"export type {name} ="]
    for index, part in enumerate(parts):
        lines.append(f"  | {part}{';' if index == len(parts) - 1 else ''}")
    return lines


def _interface(name: str, schema: dict[str, Any]) -> list[str]:
    required = set(schema.get("required", []))
    respect = name in RESPECT_REQUIRED
    lines = [f"export interface {name} {{"]
    for field, node in schema.get("properties", {}).items():
        lines.extend(_doc(node.get("description"), indent="  "))
        lines.append(
            f"  {field}{'' if not respect or field in required else '?'}: {_node_type(node)};"
        )
    lines.append("}")
    return lines


def _emit(name: str, schema: dict[str, Any]) -> str:
    doc = _doc(schema.get("description"))
    if "enum" in schema:
        body = _alias(name, _union([_literal(v) for v in schema["enum"]]))
    elif "anyOf" in schema or "oneOf" in schema:
        body = _alias(name, _node_type(schema))
    elif schema.get("type") == "object" or "properties" in schema:
        body = _interface(name, schema)
    else:
        body = _alias(name, _node_type(schema))
    return "\n".join([*doc, *body])


def generate() -> str:
    """生成 ``types.ts`` 的完整内容（结尾带一个换行，行尾统一 LF）。"""
    roots: list[tuple[str, dict[str, Any]]] = []
    defs: dict[str, dict[str, Any]] = {}

    for obj in EXPORTS:
        schema = _schema_of(obj)
        for key, value in schema.get("$defs", {}).items():
            defs.setdefault(key, value)
        title = schema.get("title") or getattr(obj, "__name__", "Unknown")
        roots.append((str(title), schema))

    exported_names = {name for name, _ in roots}
    blocks = [BANNER]
    blocks.extend(_emit(name, schema) for name, schema in roots)
    # 未被显式导出的子模型（引用得到的）补在后面，避免出现悬空类型名
    blocks.extend(
        _emit(name, schema) for name, schema in defs.items() if name not in exported_names
    )
    return "\n\n".join(blocks) + "\n"


def read_current() -> str:
    """读盘上的 types.ts，并把行尾归一化成 LF（本机 core.autocrlf=true 会写 CRLF）。"""
    return OUTPUT.read_text(encoding="utf-8").replace("\r\n", "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.gen_ts_types",
        description="从后端 Pydantic 模型生成前端 TS 类型",
    )
    parser.add_argument("--check", action="store_true", help="只校验是否最新，不写盘")
    args = parser.parse_args(argv)

    generated = generate()
    target = OUTPUT.relative_to(ROOT).as_posix()

    if args.check:
        if read_current() == generated:
            print(f"✓ {target} 与后端模型同步")
            return 0
        print(f"✗ {target} 与后端模型不同步，请运行：uv run python -m scripts.gen_ts_types")
        return 1

    OUTPUT.write_text(generated, encoding="utf-8", newline="\n")
    print(f"✓ 已生成 {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
