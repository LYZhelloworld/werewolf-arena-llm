"""单局事件落盘：``events.jsonl`` 增量追加 + ``events.json`` 收官归档。

为什么两个都要：单个 JSON 数组无法安全增量追加——写到一半崩溃会留下语法
损坏的文件，这一局的复盘数据就全没了。进行中用 JSON Lines（崩溃最多丢最后
一行），结束时再一次性归档成完整数组。两者都是 JSON，不引入二进制数据库。
"""

from __future__ import annotations

import os
from pathlib import Path

from engine.events import Event
from storage.atomic import atomic_write_json

EVENTS_JSONL = "events.jsonl"
EVENTS_JSON = "events.json"
META_JSON = "meta.json"


class EventLog:
    """一局对局的落盘句柄。目录结构见 DESIGN.md §8.1。"""

    def __init__(self, root: str | Path, game_id: str) -> None:
        self.root = Path(root)
        self.game_id = game_id
        self.dir = self.root / "games" / game_id
        self.dir.mkdir(parents=True, exist_ok=True)

    @property
    def jsonl_path(self) -> Path:
        return self.dir / EVENTS_JSONL

    @property
    def json_path(self) -> Path:
        return self.dir / EVENTS_JSON

    @property
    def meta_path(self) -> Path:
        return self.dir / META_JSON

    def append(self, events: list[Event]) -> None:
        """增量追加一批事件。每批写完立即刷盘，崩溃最多丢最后一批。"""
        if not events:
            return
        with self.jsonl_path.open("a", encoding="utf-8") as handle:
            for event in events:
                handle.write(event.model_dump_json() + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def archive(self, events: list[Event]) -> Path:
        """把完整事件数组归档为 ``events.json``。复盘主要读这个文件。"""
        atomic_write_json(self.json_path, [e.model_dump(mode="json") for e in events])
        return self.json_path

    def write_meta(self, meta: dict) -> Path:
        atomic_write_json(self.meta_path, meta)
        return self.meta_path

    def write_context(self, seat: int, messages: list[dict]) -> Path:
        """落盘某个席位的完整消息历史快照（M3 接 LLM 后才会有内容）。"""
        path = self.dir / "contexts" / f"{seat}.json"
        atomic_write_json(path, messages)
        return path
