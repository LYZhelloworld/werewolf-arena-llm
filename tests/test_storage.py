"""持久化单测：JSONL 追加、events.json 归档、meta/index 原子写。

这里全部用 tmp_path，不碰仓库里的 data/。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from config import BoardConfig
from engine.engine import RuleEngine
from engine.events import Event, Visibility
from engine.state import Phase
from orchestrator.game_loop import GameRunner
from orchestrator.policies import RandomPolicy
from storage.event_log import EventLog
from storage.index import load_index, update_index
from storage.meta import board_hash, build_meta


def _events(count: int, offset: int = 0) -> list[Event]:
    return [
        Event(
            seq=offset + i + 1,
            game_id="g1",
            day=1,
            phase="day_discussion",
            type="speech",
            visibility=Visibility.PUBLIC,
            actor=1,
            payload={"message": f"第 {i + 1} 句"},
        )
        for i in range(count)
    ]


def test_append_then_archive_keeps_every_event(tmp_path: Path) -> None:
    log = EventLog(tmp_path, "g1")
    batch1, batch2 = _events(3), _events(2, offset=3)
    log.append(batch1)
    log.append(batch2)

    lines = log.jsonl_path.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 5
    assert json.loads(lines[0])["payload"]["message"] == "第 1 句"

    log.archive(batch1 + batch2)
    archived = json.loads(log.json_path.read_text(encoding="utf-8"))
    assert [e["seq"] for e in archived] == [1, 2, 3, 4, 5]
    # ts 是 datetime，归档后必须变成 JSON 字符串
    assert isinstance(archived[0]["ts"], str)
    # jsonl 只是进行中的临时载体，归档后必须删掉，不留两份等价数据
    assert not log.jsonl_path.exists()


def test_archive_leaves_no_temp_files(tmp_path: Path) -> None:
    log = EventLog(tmp_path, "g1")
    log.archive(_events(2))
    leftovers = [p.name for p in log.dir.iterdir() if p.suffix == ".tmp"]
    assert leftovers == []


def test_archive_without_appending_still_works(tmp_path: Path) -> None:
    """没追加过 jsonl 也要能归档（重复归档同理，不能因缺文件而失败）。"""
    log = EventLog(tmp_path, "g1")
    log.archive(_events(2))
    log.archive(_events(2))
    assert json.loads(log.json_path.read_text(encoding="utf-8"))[0]["seq"] == 1


def test_meta_records_board_fingerprint_and_result(tmp_path: Path) -> None:
    board = BoardConfig()
    engine = RuleEngine(board, seed=5)
    state, opening = engine.new_game()
    runner = GameRunner(engine, RandomPolicy(seed=5))
    state, more = runner.run(state)

    log = EventLog(tmp_path, state.game_id)
    log.write_meta(build_meta(state, started_at=datetime.now(UTC), models={}, temperature=0.8))
    meta = json.loads(log.meta_path.read_text(encoding="utf-8"))

    assert state.winner is not None
    assert meta["winner"] == state.winner.value
    assert meta["seed"] == 5
    assert meta["board_hash"] == board_hash(board)
    assert len(meta["final_roles"]) == 9
    assert state.phase == Phase.GAME_OVER
    assert len(opening + more) > 0


def test_index_is_upserted_by_game_id(tmp_path: Path) -> None:
    entry = {"game_id": "g1", "started_at": "2026-10-07T00:00:00+00:00", "winner": "wolf"}
    update_index(tmp_path, entry)
    update_index(tmp_path, {**entry, "winner": "good"})
    update_index(tmp_path, {"game_id": "g2", "started_at": "2026-10-07T00:01:00+00:00"})

    items = load_index(tmp_path)
    assert [item["game_id"] for item in items] == ["g1", "g2"]
    assert next(i for i in items if i["game_id"] == "g1")["winner"] == "good"


def test_board_hash_changes_with_board(tmp_path: Path) -> None:
    board = BoardConfig()
    tweaked = board.model_copy(deep=True)
    tweaked.witch.has_poison = not tweaked.witch.has_poison
    assert board_hash(board) != board_hash(tweaked)
