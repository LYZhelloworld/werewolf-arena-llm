// ⚠️ 本文件由脚本自动生成，请勿手改。
// 生成：uv run python -m scripts.gen_ts_types
// 事实来源：后端 Pydantic 模型（engine/events.py、server/schemas.py）。
// tests/test_ts_types.py 会校验本文件与后端是否同步（DESIGN.md §10.4）。

/** 可见性。英文枚举值——前端 TS 要按字面量 switch。 */
export type Visibility =
  | "public"
  | "private:wolf"
  | "private:seer"
  | "private:witch"
  | "private:self"
  | "spectator";

/** 前端直接用的展示文本。中文。 */
export interface RenderText {
  text: string;
  tone: string | null;
}

/**
 * 思考过程（CoT）。
 * 恒为观众可见：构建 agent 上下文时必须**物理剔除**本字段，
 * 而不是渲染后打码。见 DESIGN.md §6.2。
 */
export interface Thinking {
  text: string | null;
  /** reasoning_content | content | think_tool | none */
  source: string;
  replayed: boolean;
}

/**
 * 一次 LLM 调用的可观测信息（DESIGN.md §8.2）。
 * 放在 ``engine/events.py`` 而不是 ``agents/``：事件 schema 必须由引擎侧统一定义，
 * 且它只是纯数据，不含任何 LLM 依赖。``fallback`` 表示这一手是兜底出来的。
 */
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

/** 一条事件。落盘为 JSON（jsonl 追加 / json 归档）。 */
export interface Event {
  seq: number;
  game_id: string;
  ts: string;
  day: number;
  phase: string;
  /** 英文枚举，见 DESIGN.md §8.3 */
  type: string;
  visibility: Visibility;
  /** None 表示系统事件 */
  actor: number | null;
  payload: Record<string, unknown>;
  thinking: Thinking | null;
  llm: LlmMeta | null;
  render: RenderText | null;
}

/**
 * 启动一局的入参。
 * - ``model`` 留空：用脚本化客户端顶替 LLM（默认，不需要 API key）。
 * - ``model`` 给模型名：9 席全部用这一个模型（会产生真实费用，需配置 key）。
 * - ``random``：完全用随机策略跑，最快、用于压规则，不产生任何 LLM 调用。
 * - ``seed``：可选随机种子；留空则每局随机。
 */
export interface CreateGameRequest {
  /** models.yaml 里的模型名；留空走 mock */
  model?: string | null;
  /** 用随机策略跑，不调 LLM */
  random?: boolean;
  /** 随机种子；留空每局随机 */
  seed?: number | null;
  /** 事件流默认视角；留空为 spectator（全量，含 CoT） */
  view?: "spectator" | number | null;
}

/** 启动一局的返回值。对局在后台跑，接口不阻塞等待结束。 */
export interface CreateGameResponse {
  game_id: string;
  status: string;
}

/** ``GET /games`` 的列表项。进行中的对局 ``running=True`` 且结果字段为空。 */
export interface GameListItem {
  game_id: string;
  started_at: string;
  seed: number | null;
  models: Record<string, string>;
  winner: string | null;
  days: number | null;
  event_count: number;
  path: string;
  running: boolean;
}

/**
 * ``GET /games/{id}``：``meta.json`` 的内容。
 * 已结束的对局从落盘文件读；进行中的对局用内存快照顶替（``running=True``）。
 */
export interface GameMeta {
  game_id: string;
  started_at: string;
  finished_at: string | null;
  seed: number | null;
  engine_version: string | null;
  prompt_version: string | null;
  board_hash: string | null;
  models: Record<string, string>;
  temperature: number | null;
  winner: string | null;
  final_roles: Record<string, string>;
  survivors: number[];
  days: number | null;
  running: boolean;
  event_count: number | null;
}

/**
 * ``GET /models`` 的一项：可选的模型及其所属 provider。
 * 前端「新建对局（指定模型）」用它把模型名做成下拉框，而不是让用户手输——
 * 抄错模型名（多一个空格、大小写不对）是最常见的手误，且报错要等到真正发起
 * 对局才出现。
 */
export interface ModelListItem {
  name: string;
  provider: string;
}

/** ``GET /models`` 的返回值：当前 ``models.yaml`` 里配置的全部模型。 */
export interface ModelsResponse {
  models: ModelListItem[];
}

/** WebSocket 控制消息：对局正常结束，之后连接关闭。 */
export interface WsDone {
  type: "done";
}

/** WebSocket 控制消息：对局运行期报错，或连上时找不到对局。 */
export interface WsError {
  type: "error";
  message: string;
}

/**
 * WebSocket 上的一条消息：带 ``seq`` 的是事件，否则是控制消息（done / error）。
 * 仅用于生成前端联合类型；服务端逐条 ``model_dump`` 透传，不整体校验。
 */
export type WsMessage = Event | WsDone | WsError;
