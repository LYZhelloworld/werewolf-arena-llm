"""对局管理：启动对局、把事件实时推送给观战者、提供回放读取。

设计要点（为什么这么搭）：

- ``GameRunner`` 是**同步**主循环（M2 定下，M3 的并发只在 ``decide_all`` 内部一次性
  ``asyncio.run``）。M5 不把它整体 async 化（DESIGN §5.3.2 之后再做），而是让一局对局
  跑在**独立守护线程**里。这样对局推进不依赖事件循环是否被持续驱动——
  某些测试 / 网关环境下循环会在请求间隙暂停，用 ``run_in_executor`` 反而会把后台任务卡死。
  线程里的 LLM 并发照常在 ``decide_all`` 内部完成，互不干扰。
- 实时推送**不碰事件循环**：worker 线程把每批事件放进每个订阅者的普通 ``queue.Queue``
  （线程安全、无需 loop 引用）。WebSocket 处理器在自己的事件循环上用
  ``loop.run_in_executor(None, q.get)`` 把队列里的批次取出来发给客户端。
  这样无论 TestClient（每次请求一个新 loop）还是 uvicorn（单 loop 常驻）都能正确工作。
- 每个 ``GameHandle`` 持有一份内存事件列表（``events``）做回放历史。观战者连上 WS：
  先回灌「截至此刻的快照」作为历史，再订阅队列接实时。快照拷贝与入队在 ``lock`` 下原子
  完成，二者之间不会漏事件、也不会重复。
"""

from __future__ import annotations

import queue
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from config import BoardConfig, GameConfig
from engine.engine import RuleEngine
from engine.events import Event
from engine.state import Role
from engine.visibility import ViewFilter
from orchestrator.game_loop import GameRunner
from orchestrator.policies import FirstLegalPolicy
from storage.event_log import EVENTS_JSON, EventLog
from storage.index import load_index, update_index
from storage.meta import build_meta

# 投递给订阅队列的单元：("event", batch) | ("done", None) | ("error", message)
StreamItem = tuple[str, Any]


class GameHandle:
    """一局对局的内存句柄。worker 线程写、WS/REST 读，靠 ``lock`` 保护。"""

    def __init__(
        self,
        game_id: str,
        event_log: EventLog,
        board: BoardConfig,
        game: GameConfig,
        policy: Any,
        models_map: dict[str, str],
        seed: int | None,
        started_at: datetime,
    ) -> None:
        self.game_id = game_id
        self.event_log = event_log
        self.board = board
        self.game = game
        self.policy = policy
        self.models_map = models_map
        self.seed = seed
        self.started_at = started_at

        self.events: list[Event] = []
        self.subscribers: set[queue.Queue[StreamItem]] = set()
        self.lock = threading.Lock()
        self.done = False
        self.error: str | None = None


class GameManager:
    """管理所有在跑 / 已结束的对局。作为单例挂在 ``app.state`` 上。"""

    def __init__(self, root: str | Path, config_dir: Path) -> None:
        self.root = Path(root)
        self.config_dir = config_dir
        self.games: dict[str, GameHandle] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # 启动
    # ------------------------------------------------------------------

    def _new_game_id(self) -> str:
        return f"{datetime.now(UTC):%Y%m%d-%H%M%S}-{uuid4().hex[:6]}"

    def start(
        self,
        *,
        board: BoardConfig,
        game: GameConfig,
        policy: Any,
        models_map: dict[str, str],
        seed: int | None,
    ) -> GameHandle:
        """开一局并立即在后台线程跑起来，返回句柄（含 ``game_id``）。"""
        game_id = self._new_game_id()
        handle = GameHandle(
            game_id=game_id,
            event_log=EventLog(self.root, game_id),
            board=board,
            game=game,
            policy=policy,
            models_map=models_map,
            seed=seed,
            started_at=datetime.now(UTC),
        )
        with self._lock:
            self.games[game_id] = handle
        threading.Thread(target=self._run_game_thread, args=(handle,), daemon=True).start()
        return handle

    def _run_game_thread(self, handle: GameHandle) -> None:
        """在独立线程里跑完整局（同步 ``GameRunner.run``）。"""

        def on_events(batch: list[Event], _state: Any) -> None:
            with handle.lock:
                handle.events.extend(batch)
            handle.event_log.append(batch)
            with handle.lock:
                for q in list(handle.subscribers):
                    q.put(("event", batch))

        engine = RuleEngine(handle.board, game_id=handle.game_id, seed=handle.seed)
        state, opening = engine.new_game()
        with handle.lock:
            handle.events.extend(opening)
        handle.event_log.append(opening)

        runner = GameRunner(engine, handle.policy, on_events=on_events, fallback=FirstLegalPolicy())
        try:
            final_state, _collected = runner.run(state)
        except Exception as exc:  # 运行期错误不能静默吞掉，要让观战者知道
            handle.error = str(exc)
            with handle.lock:
                handle.done = True
                for q in list(handle.subscribers):
                    q.put(("error", str(exc)))
            return

        # 收官：归档事件、写 meta、转储 agent 上下文、更新索引
        handle.event_log.archive(handle.events)
        handle.event_log.write_meta(
            build_meta(final_state, started_at=handle.started_at, models=handle.models_map)
        )
        if handle.game.persist.dump_agent_contexts:
            for seat, context in sorted(getattr(handle.policy, "contexts", {}).items()):
                handle.event_log.write_context(seat, context.snapshot())

        update_index(
            self.root,
            {
                "game_id": final_state.game_id,
                "started_at": handle.started_at.isoformat(),
                "seed": handle.seed,
                "models": handle.models_map,
                "winner": final_state.winner.value if final_state.winner else None,
                "days": final_state.day,
                "event_count": len(handle.events),
                "path": f"games/{final_state.game_id}",
            },
        )
        with handle.lock:
            handle.done = True
            for q in list(handle.subscribers):
                q.put(("done", None))

    # ------------------------------------------------------------------
    # 订阅（实时）
    # ------------------------------------------------------------------

    def get(self, game_id: str) -> GameHandle | None:
        return self.games.get(game_id)

    def subscribe(self, game_id: str) -> tuple[list[Event], queue.Queue[StreamItem], bool] | None:
        """观战者连上时调用：返回 ``(历史快照, 实时队列, 是否已结束)``。

        快照拷贝与入队在 ``lock`` 下原子完成：此前产出的批次只会在快照里（不会进新队列），
        此后产出的批次只进新队列（不会重复进快照），二者之间不会漏也不会重。
        """
        handle = self.games.get(game_id)
        if handle is None:
            return None
        sub: queue.Queue[StreamItem] = queue.Queue()
        with handle.lock:
            backfill = list(handle.events)
            handle.subscribers.add(sub)
            done = handle.done
        return backfill, sub, done

    def unsubscribe(self, game_id: str, sub: queue.Queue[StreamItem]) -> None:
        handle = self.games.get(game_id)
        if handle is None:
            return
        with handle.lock:
            handle.subscribers.discard(sub)

    @staticmethod
    def seat_role(handle: GameHandle, seat: int) -> Role:
        """从 ``role_assigned`` 事件里取该座位的角色（避免重新发牌）。"""
        for event in handle.events:
            if event.type == "role_assigned":
                return Role(event.payload["roles"][str(seat)])
        return Role.VILLAGER

    # ------------------------------------------------------------------
    # 回放读取
    # ------------------------------------------------------------------

    def list_games(self) -> list[dict]:
        """历史列表（含进行中的）。index.json 优先，再补上内存里还在跑的。"""
        items = {item["game_id"]: item for item in load_index(self.root)}
        for handle in self.games.values():
            if handle.game_id in items:
                continue
            items[handle.game_id] = {
                "game_id": handle.game_id,
                "started_at": handle.started_at.isoformat(),
                "seed": handle.seed,
                "models": handle.models_map,
                "winner": None,
                "days": None,
                "event_count": len(handle.events),
                "path": f"games/{handle.game_id}",
                "running": True,
            }
        return sorted(items.values(), key=lambda item: str(item.get("started_at", "")))

    def events_for(self, game_id: str, seat: int | None = None) -> list[Event] | None:
        """该局全部事件。还在跑就读内存；已结束从落盘的 events.json 读。

        ``seat`` 给出则按该玩家视角过滤（剔除 CoT，私有频道随死亡关闭）。
        """
        handle = self.games.get(game_id)
        if handle is not None:
            with handle.lock:
                events: list[Event] = list(handle.events)
        else:
            loaded = _load_archived(self.root, game_id)
            if loaded is None:
                return None
            events = loaded
        if seat is not None:
            role = (
                Role(
                    next(e.payload["roles"][str(seat)] for e in events if e.type == "role_assigned")
                )
                if any(e.type == "role_assigned" for e in events)
                else Role.VILLAGER
            )
            events = ViewFilter(seat, role).apply(events)
        return events

    def meta_for(self, game_id: str) -> dict | None:
        handle = self.games.get(game_id)
        path = self.root / "games" / game_id / "meta.json"
        if path.exists():
            import json

            return json.loads(path.read_text(encoding="utf-8"))
        if handle is not None:
            return {
                "game_id": handle.game_id,
                "started_at": handle.started_at.isoformat(),
                "seed": handle.seed,
                "models": handle.models_map,
                "winner": None,
                "days": None,
                "running": True,
                "event_count": len(handle.events),
            }
        return None


def _load_archived(root: str | Path, game_id: str) -> list[Event] | None:
    """读已结束对局的 events.json（落盘归档）。"""
    path = Path(root) / "games" / game_id / EVENTS_JSON
    if not path.exists():
        return None
    import json

    raw = json.loads(path.read_text(encoding="utf-8"))
    return [Event.model_validate(item) for item in raw]
