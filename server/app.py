"""FastAPI 应用：把对局以 REST + WebSocket 暴露给观战前端。

入门：

    uv run python -m server.app                 # 默认 127.0.0.1:8000
    uv run python -m server.app --host 0.0.0.0 --port 8080
    uv run uvicorn server.app:create_app --factory   # 自己控制启动

接口：
- ``POST /games``：启动一局，立即返回 ``game_id``（对局在后台跑，不阻塞）。
- ``GET  /games``：历史列表（含进行中）。
- ``GET  /games/{id}``：对局元信息（meta.json）。
- ``GET  /games/{id}/events``：全量事件（落盘归档；进行中读内存）。``?seat=N`` 按玩家视角过滤。
- ``WS   /games/{id}/ws``：实时事件流。先回灌历史，再推送新增。``?seat=N`` 切换视角。
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from config import ConfigError, load_board, load_game
from engine.events import Event
from engine.state import Role
from engine.visibility import ViewFilter
from orchestrator.cli import build_policy
from server.schemas import CreateGameRequest, CreateGameResponse
from server.ws import GameHandle, GameManager

DEFAULT_CONFIG_DIR = Path(__file__).resolve().parent.parent / "configs"


def create_app(
    config_dir: Path = DEFAULT_CONFIG_DIR,
    root: str | Path | None = None,
) -> FastAPI:
    """构造 FastAPI 应用。``root`` 缺省时从 game.yaml 的 ``persist.root`` 取。"""
    board = load_board(config_dir)  # 启动即校验，缺配置直接报错（与 CLI 一致）
    game = load_game(config_dir)
    if root is None:
        root = game.persist.root

    manager = GameManager(root=root, config_dir=config_dir)

    app = FastAPI(title="九人狼人杀观战服务", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.state.manager = manager
    app.state.board = board
    app.state.game = game

    @app.post("/games", response_model=CreateGameResponse)
    async def create_game(req: CreateGameRequest) -> CreateGameResponse:
        total_seats = sum(int(v) for v in board.roles.model_dump().values())
        try:
            policy, models_map = build_policy(
                argparse.Namespace(random=req.random, model=req.model, seed=req.seed),
                game,
                total_seats,
            )
        except ConfigError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        handle = manager.start(
            board=board, game=game, policy=policy, models_map=models_map, seed=req.seed
        )
        return CreateGameResponse(game_id=handle.game_id)

    @app.get("/games")
    async def list_games() -> list[dict]:
        return manager.list_games()

    @app.get("/games/{game_id}")
    async def game_detail(game_id: str) -> dict:
        meta = manager.meta_for(game_id)
        if meta is None:
            raise HTTPException(status_code=404, detail=f"找不到对局：{game_id}")
        return meta

    @app.get("/games/{game_id}/events")
    async def game_events(game_id: str, seat: int | None = None) -> list[dict]:
        events = manager.events_for(game_id, seat=seat)
        if events is None:
            raise HTTPException(status_code=404, detail=f"找不到对局：{game_id}")
        return [e.model_dump(mode="json") for e in events]

    @app.websocket("/games/{game_id}/ws")
    async def game_ws(websocket: WebSocket, game_id: str) -> None:
        await websocket.accept()
        handle = manager.get(game_id)
        if handle is None:
            await websocket.send_json({"type": "error", "message": f"找不到对局：{game_id}"})
            await websocket.close()
            return

        seat = _parse_seat(websocket.query_params.get("seat"), handle)
        view_filter: ViewFilter | None = None
        if seat is not None:
            role = Role(manager.seat_role(handle, seat).value)
            view_filter = ViewFilter(seat, role)

        subscribed = manager.subscribe(game_id)
        if subscribed is None:
            await websocket.send_json({"type": "error", "message": f"对局已销毁：{game_id}"})
            await websocket.close()
            return
        backfill, queue, done = subscribed

        for event in view_filter.apply(backfill) if view_filter else backfill:
            await websocket.send_json(event.model_dump(mode="json"))
        if done:
            await websocket.send_json({"type": "done"})
            await websocket.close()
            return

        loop = asyncio.get_running_loop()
        try:
            while True:
                kind, payload = await loop.run_in_executor(None, queue.get)
                if kind == "event":
                    batch: list[Event] = payload
                    for event in view_filter.apply(batch) if view_filter else batch:
                        await websocket.send_json(event.model_dump(mode="json"))
                elif kind == "error":
                    await websocket.send_json({"type": "error", "message": payload})
                    break
                else:  # done
                    await websocket.send_json({"type": "done"})
                    break
        except WebSocketDisconnect:
            return
        finally:
            manager.unsubscribe(game_id, queue)

    return app


def _parse_seat(raw: str | None, handle: GameHandle) -> int | None:
    """把 ``?seat=N`` 解析成座位号；非法或越界返回 None（退化为全量视角）。"""
    if not raw:
        return None
    try:
        seat = int(raw)
    except ValueError:
        return None
    board = handle.board
    total = sum(int(v) for v in board.roles.model_dump().values())
    return seat if 1 <= seat <= total else None


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m server.app", description="九人狼人杀观战服务")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址，默认 127.0.0.1")
    parser.add_argument("--port", type=int, default=8000, help="监听端口，默认 8000")
    parser.add_argument("--config-dir", type=Path, default=DEFAULT_CONFIG_DIR, help="配置目录")
    args = parser.parse_args(argv)

    import uvicorn

    app = create_app(config_dir=args.config_dir)
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
