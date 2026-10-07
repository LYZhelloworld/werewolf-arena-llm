"""服务层请求 / 响应 schema（Pydantic）。

事件本身直接以 ``Event.model_dump(mode="json")`` 透传给前端，不在这里再包一层——
前端按 event ``type`` 渲染即可，schema 只约束「启动对局」这一类入参。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


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
    game_id: str
    status: str = "running"
