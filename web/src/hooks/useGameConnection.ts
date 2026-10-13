import { useEffect, useRef } from "react";
import { wsUrl } from "../api/client";
import type { WsMessage } from "../api/types";
import { useGameStore } from "../store/gameStore";

const RECONNECT_MS = 1500;

// 维护一条到后端的 WebSocket：先回灌历史，再实时追加。
// 切换 seat（上帝视角 <-> 某玩家视角）会关闭旧连接、开新连接，
// 因为不同视角的事件流是服务端按 seat 过滤出来的。
//
// 这里**必须用每次 effect 各自的局部标志**（而不是跨 effect 共享的 ref）来判断
// 「这条连接是不是我们自己关掉的」。之前用一个共享的 closedByUs ref，切视角时旧连接
// 的 onclose 会晚于新 effect 把 ref 重置回 false，于是旧连接误以为自己「非正常断开」
// 去自动重连——它带着旧视角（常常是上帝视角）把事件重新灌进刚清空的事件流，界面就
// 「切到某人视角后又跳回上帝视角」。局部标志随 effect 一起销毁，从根上避免这个竞态。
export function useGameConnection(gameId: string, seat: number | null) {
  const wsRef = useRef<WebSocket | null>(null);

  const appendEvents = useGameStore((s) => s.appendEvents);
  const setStatus = useGameStore((s) => s.setStatus);
  const setError = useGameStore((s) => s.setError);
  const resetEvents = useGameStore((s) => s.resetEvents);

  useEffect(() => {
    let cancelled = false; // 本 effect 是否已被卸载（切了 seat / 组件销毁）
    let reconnectTimer: number | null = null;
    resetEvents();
    setStatus("connecting");

    const connect = () => {
      const ws = new WebSocket(wsUrl(gameId, seat));
      wsRef.current = ws;

      ws.onopen = () => {
        if (!cancelled) setStatus("live");
      };

      ws.onmessage = (ev) => {
        if (cancelled) return;
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
        // 本 effect 已失效就不再重连：旧连接的重连只会把过期视角的数据灌回来。
        if (cancelled) return;
        // 尚未结束就断了：标记为重连中并自动重连，重连后服务端会重新回灌全量历史。
        if (useGameStore.getState().status !== "done") {
          setStatus("reconnecting");
          reconnectTimer = window.setTimeout(connect, RECONNECT_MS);
        }
      };

      ws.onerror = () => {
        // 错误后浏览器会接着触发 onclose，重连逻辑在 onclose 里处理。
      };
    };

    connect();

    return () => {
      cancelled = true;
      if (reconnectTimer !== null) {
        clearTimeout(reconnectTimer);
        reconnectTimer = null;
      }
      wsRef.current?.close();
      wsRef.current = null;
    };
  }, [gameId, seat, appendEvents, setStatus, setError, resetEvents]);
}
