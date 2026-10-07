"""服务层测试：REST 启动 / 回放 + WebSocket 实时推送。

全程用 ``MockClient`` 顶替 LLM（与 CLI 一致，不产生任何真实调用、不读 API key），
对局在后台线程跑，由 TestClient 驱动。持久化指向临时目录，不污染 ``data/``。

注意：mock 客户端不产出 CoT，所以这里不测 CoT 内容，只测「视角过滤是否生效」
（spectator 看得到底牌与所有私有频道；玩家视角看不到别人的私有频道、且 CoT 被物理剔除）。
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from server.app import create_app

REPO_CONFIGS = Path(__file__).resolve().parent.parent / "configs"


def _wait_done(client: TestClient, game_id: str, timeout: float = 30.0) -> None:
    """后台对局在独立线程跑，测试里要等它收官再读回放。"""
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        handle = client.app.state.manager.games.get(game_id)  # type: ignore[attr-defined]
        if handle is not None and handle.done:
            return
        time.sleep(0.05)
    raise AssertionError(f"对局 {game_id} 在 {timeout}s 内未结束")


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    for name in ("board.example.yaml", "game.example.yaml"):
        (cfg / name.replace(".example", "")).write_text(
            (REPO_CONFIGS / name).read_text(encoding="utf-8")
        )
    root = tmp_path / "data"
    app = create_app(config_dir=cfg, root=root)
    return TestClient(app)


def _collect_ws(client: TestClient, game_id: str, seat: int | None = None) -> list[dict]:
    """连上 WS，读到 done/error 为止，返回收到的全部消息。"""
    query = f"?seat={seat}" if seat is not None else ""
    messages: list[dict] = []
    with client.websocket_connect(f"/games/{game_id}/ws{query}") as ws:
        while True:
            msg = ws.receive_json()
            messages.append(msg)
            if msg.get("type") in ("done", "error"):
                break
    return messages


def test_create_and_replay(client: TestClient) -> None:
    resp = client.post("/games", json={})
    assert resp.status_code == 200
    game_id = resp.json()["game_id"]
    assert game_id

    _wait_done(client, game_id)

    events_resp = client.get(f"/games/{game_id}/events")
    assert events_resp.status_code == 200
    events = events_resp.json()
    assert events[0]["type"] == "game_start"
    assert any(e["type"] == "role_assigned" for e in events)  # spectator 看得到底牌

    detail = client.get(f"/games/{game_id}")
    assert detail.status_code == 200
    assert detail.json()["winner"] in ("wolf", "good")

    listing = client.get("/games").json()
    assert any(g["game_id"] == game_id for g in listing)


def test_websocket_streams_full_game(client: TestClient) -> None:
    game_id = client.post("/games", json={}).json()["game_id"]
    messages = _collect_ws(client, game_id)

    types = [m.get("type") for m in messages if "type" in m]
    assert "done" in types
    assert any(m.get("type") == "game_start" for m in messages)


def test_websocket_seat_view_filters_private_channels(client: TestClient) -> None:
    game_id = client.post("/games", json={}).json()["game_id"]
    _wait_done(client, game_id)

    spectator = client.get(f"/games/{game_id}/events").json()
    # 选一个非狼座位，验证其视角被收窄
    seat = next(int(s) for s, r in spectator[1]["payload"]["roles"].items() if r != "werewolf")

    messages = _collect_ws(client, game_id, seat=seat)
    types = [m.get("type") for m in messages if "type" in m]
    assert "done" in types

    # 玩家视角收不到 spectator-only 的底牌事件
    assert not any(m.get("type") == "role_assigned" for m in messages if "type" in m)
    # 玩家视角事件数应少于全量（私有频道被过滤）
    assert len([m for m in messages if "type" in m]) < len(spectator)


def test_missing_game_404(client: TestClient) -> None:
    assert client.get("/games/nope/events").status_code == 404
    assert client.get("/games/nope").status_code == 404
