import { useEffect, useRef } from "react";
import { wsUrl } from "../api/client";
import type { WsMessage } from "../api/types";
import { useGameStore } from "../store/gameStore";

const RECONNECT_MS = 1500;

// 维护一条到后端的 WebSocket：先回灌历史，再实时追加。
// 切换 seat（上帝视角 <-> 某玩家视角）会关闭旧连接、开新连接，
// 因为不同视角的事件流是服务端按 seat 过滤出来的。
export function useGameConnection(gameId: string, seat: number | null) {
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimer = useRef<number | null>(null);
  const closedByUs = useRef(false);

  const appendEvents = useGameStore((s) => s.appendEvents);
  const setStatus = useGameStore((s) => s.setStatus);
  const setError = useGameStore((s) => s.setError);
  const resetEvents = useGameStore((s) => s.resetEvents);

  useEffect(() => {
    closedByUs.current = false;
    resetEvents();
    setStatus("connecting");

    const connect = () => {
      const ws = new WebSocket(wsUrl(gameId, seat));
      wsRef.current = ws;

      ws.onopen = () => setStatus("live");

      ws.onmessage = (ev) => {
        let msg: WsMessage;
        try {
          msg = JSON.parse(ev.data as string) as WsMessage;
        } catch {
          return;
        }
        // 控制消息（done/error）没有 seq；事件都有 seq，以此区分。
        if (!("seq" in msg)) {
          if (msg.type === "done") {
            setStatus("done");
          } else if (msg.type === "error") {
            setError(msg.message);
            setStatus("error");
          }
          return;
        }
        appendEvents([msg]);
      };

      ws.onclose = () => {
        if (closedByUs.current) return;
        // 尚未结束就断了：标记为重连中并自动重连，重连后服务端会重新回灌全量历史。
        if (useGameStore.getState().status !== "done") {
          setStatus("reconnecting");
          reconnectTimer.current = window.setTimeout(connect, RECONNECT_MS);
        }
      };

      ws.onerror = () => {
        // 错误后浏览器会接着触发 onclose，重连逻辑在 onclose 里处理。
      };
    };

    connect();

    return () => {
      closedByUs.current = true;
      if (reconnectTimer.current !== null) {
        clearTimeout(reconnectTimer.current);
        reconnectTimer.current = null;
      }
      wsRef.current?.close();
      wsRef.current = null;
    };
  }, [gameId, seat, appendEvents, setStatus, setError, resetEvents]);
}
