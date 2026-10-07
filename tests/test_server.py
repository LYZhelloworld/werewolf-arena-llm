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


def _make_app(tmp_path: Path, models_yaml: str | None = None) -> tuple[TestClient, Path]:
    """建一个指向临时目录的 app（配置从仓库样例拷），返回 ``(客户端, 存档根目录)``。

    同一个 ``tmp_path`` 调用两次即可模拟「服务重启」——第二次的 app 内存里没有第一次的对局，
    但存档根目录是同一个。``models_yaml`` 给定时写一份 ``models.yaml``（默认不写，
    用于覆盖「用户还没配 models.yaml」这条路径）。
    """
    cfg = tmp_path / "cfg"
    cfg.mkdir(exist_ok=True)
    for name in ("board.example.yaml", "game.example.yaml"):
        (cfg / name.replace(".example", "")).write_text(
            (REPO_CONFIGS / name).read_text(encoding="utf-8")
        )
    if models_yaml is not None:
        (cfg / "models.yaml").write_text(models_yaml, encoding="utf-8")
    root = tmp_path / "data"
    return TestClient(create_app(config_dir=cfg, root=root)), root


# 一个「模型名存在但服务不可达」的配置：build_policy 只构造客户端对象，不会联网，
# 所以用它就能覆盖 POST /games 的完整成功路径。
_FAKE_MODELS_YAML = """
providers:
  fake:
    base_url: http://127.0.0.1:9/v1
    api_key: test-key
    models:
      - name: qwen/qwen3.5-9b
defaults:
  timeout_s: 1
  network_retries: 0
"""


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    client, _root = _make_app(tmp_path)
    return client


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


def test_create_game_with_model_starts(tmp_path: Path) -> None:
    """``model`` 非空时必须能真正起局。

    这条是回归守卫：``build_policy`` 曾依赖 ``argparse.Namespace``，而服务层少传了
    ``config_dir``，于是任何「指定模型」的请求都在这里抛 AttributeError 变成 500
    （浏览器端只看到一句 ``Failed to fetch``，因为未捕获异常生成的响应不带 CORS 头）。
    """
    client, _root = _make_app(tmp_path, models_yaml=_FAKE_MODELS_YAML)
    resp = client.post("/games", json={"model": "qwen/qwen3.5-9b"})
    assert resp.status_code == 200, resp.text
    game_id = resp.json()["game_id"]
    _wait_done(client, game_id)  # 模型不可达：runner 会收尾成 error 而不是把线程挂死


def test_create_game_with_model_without_models_yaml_is_400(client: TestClient) -> None:
    """没配 ``models.yaml`` 时给 400 + 中文修复提示，不是 500。"""
    resp = client.post("/games", json={"model": "qwen/qwen3.5-9b"})
    assert resp.status_code == 400
    assert "models.yaml" in resp.json()["detail"]


def test_create_game_with_unknown_model_lists_available_models(tmp_path: Path) -> None:
    """模型名写错（如漏掉 ``qwen/`` 前缀）要列出可用模型，让用户照着改。"""
    client, _root = _make_app(tmp_path, models_yaml=_FAKE_MODELS_YAML)
    resp = client.post("/games", json={"model": "qwen3.5-9b"})
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "可用模型" in detail
    assert "qwen/qwen3.5-9b" in detail


def test_missing_game_404(client: TestClient) -> None:
    assert client.get("/games/nope/events").status_code == 404
    assert client.get("/games/nope").status_code == 404


def test_websocket_replays_archived_game_after_restart(tmp_path: Path) -> None:
    """服务重启后，历史对局不在内存里，WS 也必须能放完归档再 ``done``。

    否则「读取历史对战」会以一句 ``找不到对局`` 收场——而 ``/games`` 列表里明明有它。
    """
    first, _root = _make_app(tmp_path)
    game_id = first.post("/games", json={}).json()["game_id"]
    _wait_done(first, game_id)

    restarted, _root = _make_app(tmp_path)
    # 确实是「重启后」的状态：内存里没有这个对局
    assert restarted.app.state.manager.get(game_id) is None  # type: ignore[attr-defined]

    messages = _collect_ws(restarted, game_id)
    types = [m.get("type") for m in messages if "type" in m]
    assert "done" in types
    assert not any(m.get("type") == "error" for m in messages)
    assert messages[0]["type"] == "game_start"
    # 与 REST 全量回放同源同量
    assert len([m for m in messages if "type" in m and m["type"] != "done"]) == len(
        restarted.get(f"/games/{game_id}/events").json()
    )


def test_websocket_archived_game_respects_seat_view(tmp_path: Path) -> None:
    """归档回放同样要按 ``?seat=N`` 过滤：玩家视角收不到底牌与私有频道。"""
    first, _root = _make_app(tmp_path)
    game_id = first.post("/games", json={}).json()["game_id"]
    _wait_done(first, game_id)

    restarted, _root = _make_app(tmp_path)
    spectator = restarted.get(f"/games/{game_id}/events").json()
    seat = next(int(s) for s, r in spectator[1]["payload"]["roles"].items() if r != "werewolf")

    messages = _collect_ws(restarted, game_id, seat=seat)
    types = [m.get("type") for m in messages if "type" in m]
    assert "done" in types
    assert not any(m.get("type") == "role_assigned" for m in messages if "type" in m)
    assert len(messages) < len(spectator)


def test_websocket_unknown_game_reports_error(client: TestClient) -> None:
    messages = _collect_ws(client, "不存在的对局")
    assert messages[0]["type"] == "error"
    assert "找不到对局" in messages[0]["message"]


def test_root_serves_a_signpost_not_a_404(client: TestClient) -> None:
    """根路径必须给指路牌，不能是 ``{"detail":"Not Found"}``。

    后端只提供 API，用户（和浏览器自动补全）打开 ``http://127.0.0.1:8000``
    看到裸 404 会以为服务没起来——这正是真实踩过的坑。
    """
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    body = resp.text
    assert "5173" in body  # 指向前端
    assert "/docs" in body  # 指向 API 文档
    assert "not found" not in body.lower()
    # 根路径是给人看的，不该混进 OpenAPI schema
    assert "/" not in client.get("/openapi.json").json()["paths"]
