"""FastAPI 应用：把对局以 REST + WebSocket 暴露给观战前端。

入门：

    uv run python -m server.app                 # 默认 127.0.0.1:8000
    uv run python -m server.app --host 0.0.0.0 --port 8080
    uv run uvicorn server.app:create_app --factory   # 自己控制启动

接口：
- ``GET  /``：指路牌（后端没有页面，观战界面在前端 5173）。
- ``POST /games``：启动一局，立即返回 ``game_id``（对局在后台跑，不阻塞）。
- ``GET  /games``：历史列表（含进行中）。
- ``GET  /games/{id}``：对局元信息（meta.json）。
- ``GET  /games/{id}/events``：全量事件（落盘归档；进行中读内存）。``?seat=N`` 按玩家视角过滤。
- ``WS   /games/{id}/ws``：实时事件流。先回灌历史，再推送新增。``?seat=N`` 切换视角。
  服务重启后历史对局不在内存里，此时把归档事件一次性放完再发 ``done``（见下方 ``game_ws``）。
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

from config import ConfigError, load_board, load_game, load_models
from engine.events import Event
from engine.visibility import ViewFilter
from orchestrator.cli import build_policy
from server.schemas import (
    CreateGameRequest,
    CreateGameResponse,
    GameListItem,
    GameMeta,
    ModelListItem,
    ModelsResponse,
)
from server.ws import GameManager

DEFAULT_CONFIG_DIR = Path(__file__).resolve().parent.parent / "configs"

# 根路径的指路牌。后端只提供 API，本身没有页面，浏览器直接打开
# ``http://127.0.0.1:8000`` 只会看到 ``{"detail":"Not Found"}``，很容易让人以为没起起来。
_ROOT_PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>九人狼人杀观战服务</title>
<style>
  body { margin: 0; padding: 48px 24px; background: #0f172a; color: #e2e8f0;
         font: 15px/1.7 system-ui, "Segoe UI", "Microsoft YaHei", sans-serif; }
  main { max-width: 560px; margin: 0 auto; }
  h1 { font-size: 20px; margin: 0 0 4px; }
  p.sub { margin: 0 0 28px; color: #94a3b8; font-size: 13px; }
  ul { list-style: none; margin: 0; padding: 0; }
  li { margin-bottom: 12px; padding: 12px 16px; border: 1px solid #334155;
       border-radius: 10px; background: #1e293b; }
  a { color: #818cf8; text-decoration: none; font-family: ui-monospace, Consolas, monospace; }
  a:hover { text-decoration: underline; }
  .note { color: #94a3b8; font-size: 12px; margin-top: 28px; }
  code { font-family: ui-monospace, Consolas, monospace; color: #cbd5e1; }
</style>
</head>
<body>
<main>
  <h1>九人狼人杀 · 观战服务</h1>
  <p class="sub">这里只是后端 API，没有页面。观战界面在前端开发服务器上。</p>
  <ul>
    <li><a href="/docs">GET /docs</a> &nbsp;—&nbsp; 交互式 API 文档</li>
    <li><a href="/games">GET /games</a> &nbsp;—&nbsp; 历史对局列表（JSON）</li>
    <li><a href="/openapi.json">GET /openapi.json</a> &nbsp;—&nbsp; OpenAPI schema</li>
  </ul>
  <p class="note">
    观战界面前端：在 <code>web/</code> 目录执行 <code>npm run dev</code>，
    然后打开 <a href="http://localhost:5173">http://localhost:5173</a>。
  </p>
</main>
</body>
</html>
"""


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

    @app.get("/", include_in_schema=False)
    async def signpost() -> HTMLResponse:
        """根路径给一张指路牌：后端只有 API，真正的界面在前端（默认 5173）。"""
        return HTMLResponse(_ROOT_PAGE)

    @app.post("/games", response_model=CreateGameResponse)
    async def create_game(req: CreateGameRequest) -> CreateGameResponse:
        total_seats = sum(int(v) for v in board.roles.model_dump().values())
        try:
            policy, models_map = build_policy(
                game,
                total_seats,
                model=req.model,
                random=req.random,
                seed=req.seed,
                config_dir=config_dir,
            )
        except ConfigError as exc:
            # 配置类问题一律 400 且带中文说明。绝不能让它冒成 500：
            # 未捕获异常由 ServerErrorMiddleware 在 CORS 外层生成响应，缺 CORS 头，
            # 浏览器只会甩一句 `Failed to fetch`，用户根本看不到原因。
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        handle = manager.start(
            board=board, game=game, policy=policy, models_map=models_map, seed=req.seed
        )
        return CreateGameResponse(game_id=handle.game_id)

    @app.get("/games", response_model=list[GameListItem])
    async def list_games() -> list[GameListItem]:
        return manager.list_games()

    @app.get("/models", response_model=ModelsResponse)
    async def list_models() -> ModelsResponse:
        """当前 ``models.yaml`` 里的全部模型，供前端下拉选择（不校验 key）。

        ``check_keys=False``：这里只是「有哪些模型名可用」，不该因为某个 provider
        还没配 key 就整个列表都取不到。真正发起对局时才做 key 校验。
        """
        try:
            cfg = load_models(config_dir, check_keys=False)
        except ConfigError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        items = [
            ModelListItem(name=model.name, provider=provider_name)
            for provider_name, provider in cfg.providers.items()
            for model in provider.models
        ]
        return ModelsResponse(models=items)

    @app.get("/games/{game_id}", response_model=GameMeta)
    async def game_detail(game_id: str) -> GameMeta:
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
        total = manager.total_seats(game_id)
        if total is None:
            await websocket.send_json({"type": "error", "message": f"找不到对局：{game_id}"})
            await websocket.close()
            return
        seat = _parse_seat(websocket.query_params.get("seat"), total)

        handle = manager.get(game_id)
        if handle is None:
            # 历史对局：服务重启后它不在内存里，没有实时队列可订阅。
            # 把归档事件按视角过滤后一次性放完再发 done——对前端而言与「一场已结束的直播」同构，
            # 不必为此单开一条 REST 通道。
            events = manager.events_for(game_id, seat=seat) or []
            for event in events:
                await websocket.send_json(event.model_dump(mode="json"))
            await websocket.send_json({"type": "done"})
            await websocket.close()
            return

        view_filter: ViewFilter | None = None
        if seat is not None:
            view_filter = ViewFilter(seat, manager.seat_role(handle, seat))

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


def _parse_seat(raw: str | None, total: int) -> int | None:
    """把 ``?seat=N`` 解析成座位号；非法或越界返回 None（退化为全量视角）。"""
    if not raw:
        return None
    try:
        seat = int(raw)
    except ValueError:
        return None
    return seat if 1 <= seat <= total else None


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m server.app", description="九人狼人杀观战服务")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址，默认 127.0.0.1")
    parser.add_argument("--port", type=int, default=8000, help="监听端口，默认 8000")
    parser.add_argument("--config-dir", type=Path, default=DEFAULT_CONFIG_DIR, help="配置目录")
    args = parser.parse_args(argv)

    import uvicorn

    app = create_app(config_dir=args.config_dir)
    # 后端只有 API，别让用户对着 8000 的 404 发懵（uvicorn 会紧接着打自己的监听地址）
    print(
        f"观战界面在前端，请另开一个终端执行：cd web && npm run dev\n"
        f"然后打开 http://localhost:5173（API 文档：http://{args.host}:{args.port}/docs）",
        flush=True,
    )
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
