import type {
  CreateGameRequest,
  CreateGameResponse,
  Event,
  GameListItem,
  GameMeta,
} from "./types";

// 默认连本机后端；部署时用 VITE_API_BASE 覆盖。
const API_BASE: string =
  (import.meta.env.VITE_API_BASE as string | undefined)?.replace(/\/$/, "") ??
  "http://localhost:8000";

// FastAPI 的错误体是 `{"detail": "..."}`；直接塞给用户看会带一层花括号和引号。
function extractDetail(body: string): string {
  try {
    const parsed = JSON.parse(body) as { detail?: unknown };
    if (typeof parsed.detail === "string") return parsed.detail;
  } catch {
    /* 不是 JSON，原样返回 */
  }
  return body;
}

async function jsonOrThrow(res: Response, what: string): Promise<unknown> {
  if (!res.ok) {
    const body = await res.text().catch(() => "");
    throw new Error(`${what}失败（${res.status}）：${extractDetail(body) || res.statusText}`);
  }
  return res.json();
}

export async function createGame(req: CreateGameRequest): Promise<CreateGameResponse> {
  const res = await fetch(`${API_BASE}/games`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      model: req.model ?? null,
      random: req.random ?? false,
      seed: req.seed ?? null,
      view: req.view ?? null,
    }),
  });
  return jsonOrThrow(res, "启动对局") as Promise<CreateGameResponse>;
}

export async function listGames(): Promise<GameListItem[]> {
  const res = await fetch(`${API_BASE}/games`);
  return jsonOrThrow(res, "获取对局列表") as Promise<GameListItem[]>;
}

export async function getMeta(gameId: string): Promise<GameMeta> {
  const res = await fetch(`${API_BASE}/games/${encodeURIComponent(gameId)}`);
  return jsonOrThrow(res, "获取对局信息") as Promise<GameMeta>;
}

export async function getEvents(
  gameId: string,
  seat?: number | null,
): Promise<Event[]> {
  const url =
    seat != null
      ? `${API_BASE}/games/${encodeURIComponent(gameId)}/events?seat=${seat}`
      : `${API_BASE}/games/${encodeURIComponent(gameId)}/events`;
  const res = await fetch(url);
  return jsonOrThrow(res, "获取事件") as Promise<Event[]>;
}

export function wsUrl(gameId: string, seat?: number | null): string {
  const wsBase = API_BASE.replace(/^http/, "ws");
  const q = seat != null ? `?seat=${seat}` : "";
  return `${wsBase}/games/${encodeURIComponent(gameId)}/ws${q}`;
}

export { API_BASE };
