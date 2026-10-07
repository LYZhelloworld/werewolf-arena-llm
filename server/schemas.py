"""服务层请求 / 响应 schema（Pydantic）。

**这里是前端 TS 类型的唯一事实来源**：``scripts/gen_ts_types.py`` 读取本文件的模型，
生成 ``web/src/api/types.ts``（见 DESIGN.md §10.4）。因此凡是接口里会出现的形状，
都应该在本文件有个模型 —— 路由里手写 ``dict`` 不会进入生成结果，也就不会同步到前端。

事件本体仍然直接以 ``Event.model_dump(mode="json")`` 透传（见 ``engine/events.py``），
它也是被生成的一份契约；``WsMessage`` 只用于生成前端联合类型，服务端不做校验。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, RootModel

from engine.events import Event


class CreateGameRequest(BaseModel):
    """启动一局的入参。

    - ``model`` 留空：用脚本化客户端顶替 LLM（默认，不需要 API key）。
    - ``model`` 给模型名：9 席全部用这一个模型（会产生真实费用，需配置 key）。
    - ``random``：完全用随机策略跑，最快、用于压规则，不产生任何 LLM 调用。
    - ``seed``：可选随机种子；留空则每局随机。
    """

    model: str | None = Field(default=None, description="models.yaml 里的模型名；留空走 mock")
    random: bool = Field(default=False, description="用随机策略跑，不调 LLM")
    seed: int | None = Field(default=None, description="随机种子；留空每局随机")
    view: Literal["spectator"] | int | None = Field(
        default=None, description="事件流默认视角；留空为 spectator（全量，含 CoT）"
    )


class CreateGameResponse(BaseModel):
    """启动一局的返回值。对局在后台跑，接口不阻塞等待结束。"""

    game_id: str
    status: str = "running"


class GameListItem(BaseModel):
    """``GET /games`` 的列表项。进行中的对局 ``running=True`` 且结果字段为空。"""

    game_id: str
    started_at: str = ""
    seed: int | None = None
    models: dict[str, str] = Field(default_factory=dict)
    winner: str | None = None
    days: int | None = None
    event_count: int = 0
    path: str = ""
    running: bool = False


class GameMeta(BaseModel):
    """``GET /games/{id}``：``meta.json`` 的内容。

    已结束的对局从落盘文件读；进行中的对局用内存快照顶替（``running=True``）。
    """

    game_id: str
    started_at: str = ""
    finished_at: str | None = None
    seed: int | None = None
    engine_version: str | None = None
    prompt_version: str | None = None
    board_hash: str | None = None
    models: dict[str, str] = Field(default_factory=dict)
    temperature: float | None = None
    winner: str | None = None
    final_roles: dict[str, str] = Field(default_factory=dict)
    survivors: list[int] = Field(default_factory=list)
    days: int | None = None
    running: bool = False
    event_count: int | None = None


class WsDone(BaseModel):
    """WebSocket 控制消息：对局正常结束，之后连接关闭。"""

    type: Literal["done"] = "done"


class WsError(BaseModel):
    """WebSocket 控制消息：对局运行期报错，或连上时找不到对局。"""

    type: Literal["error"] = "error"
    message: str


class WsMessage(RootModel[Event | WsDone | WsError]):
    """WebSocket 上的一条消息：带 ``seq`` 的是事件，否则是控制消息（done / error）。

    仅用于生成前端联合类型；服务端逐条 ``model_dump`` 透传，不整体校验。
    """
