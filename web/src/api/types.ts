// 与后端 Pydantic 模型对齐的 TS 类型。event `type` / `visibility` 保持英文，
// 因为后端引擎、JSON 持久化、前端 TS 共用同一套字面量（见 DESIGN §10.4）。

export type Visibility =
  | "public"
  | "private:wolf"
  | "private:seer"
  | "private:witch"
  | "private:self"
  | "spectator";

export interface Thinking {
  text: string | null;
  // reasoning_content | content | think_tool | none
  source: string;
  replayed: boolean;
}

export interface LlmMeta {
  provider: string;
  model: string;
  latency_ms: number;
  prompt_tokens: number | null;
  completion_tokens: number | null;
  tool_turns: number;
  network_retries: number;
  fallback: boolean;
}

export interface RenderText {
  text: string;
  tone: string | null;
}

export interface Event {
  seq: number;
  game_id: string;
  ts: string;
  day: number;
  phase: string;
  type: string;
  visibility: Visibility;
  actor: number | null;
  payload: Record<string, unknown>;
  thinking: Thinking | null;
  llm: LlmMeta | null;
  render: RenderText | null;
}

// WebSocket 上除了事件，还会收到控制消息。
export type WsMessage =
  | Event
  | { type: "done" }
  | { type: "error"; message: string };

export interface CreateGameRequest {
  model?: string | null;
  random?: boolean;
  seed?: number | null;
  view?: string | null;
}

export interface CreateGameResponse {
  game_id: string;
  status: string;
}

export interface GameListItem {
  game_id: string;
  started_at: string;
  seed: number | null;
  models: Record<string, string>;
  winner: string | null;
  days: number | null;
  event_count: number;
  path: string;
  running?: boolean;
}

export interface GameMeta {
  game_id: string;
  started_at: string;
  finished_at?: string;
  seed: number | null;
  engine_version?: string;
  board_hash?: string;
  models: Record<string, string>;
  winner: string | null;
  final_roles: Record<string, string>;
  survivors: number[];
  days: number;
  running?: boolean;
  event_count?: number;
}
