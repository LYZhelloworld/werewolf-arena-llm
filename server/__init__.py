"""服务层：FastAPI + WebSocket，把对局以实时事件流的形式暴露给观战前端。

M5 只做「REST + WS 可用」：启动对局、实时推送事件、回放查询。
前端（M6）、视角切换的完整打磨（M7）建立在它之上。
"""

from server.app import create_app

__all__ = ["create_app"]
