"""对局索引：``data/games/index.json``，供历史列表与回放入口使用。

只存摘要，不存事件——事件在各自的 ``{game_id}/`` 目录下。
按 ``game_id`` 覆盖更新，重复跑同一个 game_id 不会留下两条记录。
"""

from __future__ import annotations

import json
from pathlib import Path

from storage.atomic import atomic_write_json

INDEX_NAME = "index.json"


def index_path(root: str | Path) -> Path:
    return Path(root) / "games" / INDEX_NAME


def load_index(root: str | Path) -> list[dict]:
    path = index_path(root)
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def update_index(root: str | Path, entry: dict) -> Path:
    """按 ``game_id`` 覆盖写入一条索引。"""
    items = [item for item in load_index(root) if item.get("game_id") != entry["game_id"]]
    items.append(entry)
    items.sort(key=lambda item: str(item.get("started_at", "")))
    atomic_write_json(index_path(root), items)
    return index_path(root)
