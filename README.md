# werewolf-arena-llm

**让 9 个大语言模型坐在一桌打狼人杀，你负责看。**

规则引擎当裁判，LLM 当玩家。狼人们在你看不见的频道里咬耳朵，预言家每晚偷偷查验，
女巫攥着两瓶药犹豫不决——而你拥有上帝视角，能看到所有人的底牌、全部私聊，
**以及每个人做决定时在想什么**。

---

## 这是什么

一个本地运行的九人狼人杀多智能体对战平台：

- **9 个席位全部由 LLM 扮演**，人类只观战，不下场。
- **任意模型可插拔**：通过 OpenAI 兼容协议接入，DeepSeek、GPT、通义、Kimi、GLM，
  或者你本地 Ollama / vLLM 跑的模型，改一行配置就能上桌。想看 GPT 和 DeepSeek 谁更会撒谎，随你安排。
- **真正的私有频道**：狼人夜间协商对其他 6 个玩家完全不可见——这不是 UI 上的遮遮掩掩，
  而是引擎层强制的信息隔离。
- **你能看到 CoT**：每位玩家的思考过程只有你（观众）和他自己能看到，其他参战玩家看不到。
  DeepSeek / Kimi / Qwen 这类会返回推理内容的模型，思考过程按各家协议原样回传以保证推理连贯；
  不返回推理内容的模型则忽略。**无论回不回传，思考过程一律完整记录**，复盘时逐条可看。
- **玩家有记忆**：每位玩家可以用 `memory_write` / `memory_read` 记下并回看自己认为重要的信息，
  避免长局里忘事。记忆也是私有的，只有他自己和你（在复盘里）能看到。
- **完整复盘**：思考、发言、决策、记忆、遗言、赛后感想——全部落盘为 JSON，事后逐条回看。

## 它不做什么

- 不支持人类玩家入局
- 没有警长 / 警徽竞选
- 不做账号系统、不上云、不联网部署，纯本地单机
- 不做战绩统计与 ELO 评级
- 不使用任何二进制数据库（持久化一律 JSON）

## 游戏规则

9 人标准局，**3 狼人 + 3 神职（预言家 / 女巫 / 猎人）+ 3 平民**。

- **胜负**：屠边。狼人杀光所有神职或所有平民即胜；3 狼全部出局则好人胜。
- **夜间**：狼人协商 → 出刀 → 预言家查验 → 女巫用药 → 结算
- **白天**：死亡公告 → 自由讨论（2 轮，按座位顺序，后发言者能看到前面的发言）
  → 投票 → 平票则加时一轮重投，二次平票则本日无人出局 → 出局者遗言 → 猎人开枪
- **投票是同时的**：所有人一起提交，投票期间谁也看不到别人的选择，全部提交后一次性公开。
- **细则**：女巫首夜可自救、之后不可自救，不可自毒，同晚不能同时使用两瓶药；
  猎人被毒死不能开枪，被刀或被票出可以开枪；出局不翻牌。
- **遗言**：被票出局的玩家有遗言，夜间死亡者没有。
- **出局不是离场**：出局玩家继续旁观后续的公开发言与投票（私有频道对他永久关闭），
  所以赛后他谈感想时是有东西可谈的。
- **赛后感想**：分出胜负后，全部 9 名选手逐一发表感想。

以上每一条都是**配置项**，集中在 `configs/board.yaml` 里按角色分组、以 boolean 为主的开关，
例如：

```yaml
witch:
  self_save: true
  self_save_first_night_only: true
  self_poison: false
hunter:
  shoot_on_poison: false
```

完整规则规格见 [DESIGN.md §4](./DESIGN.md)。

## 快速开始

```bash
# 1. 从样例生成你自己的配置（真实配置不入库）
cp configs/board.example.yaml  configs/board.yaml
cp configs/game.example.yaml   configs/game.yaml
cp configs/models.example.yaml configs/models.yaml

# 2. 提供 API key，两种方式任选（优先级：配置明文 > 环境变量）
export DEEPSEEK_API_KEY=sk-xxx        # 方式一：环境变量（推荐）
# 方式二：在 configs/models.yaml 的 providers.deepseek 下写一行 api_key: sk-xxx
```

两种方式都没有 → 启动报错并同时给出这两条修复命令。两者都写 → 以配置明文为准，
启动日志会标注「（环境变量已设置但被明文覆盖）」，不会让你以为 export 没生效。

配置缺失或非法时，程序会**打印明确的修复命令并异常退出**，不会静默用默认值糊弄过去。

**现在就能跑**（M2 阶段用随机策略替身，不需要 API key、不需要前端）：

```bash
uv run python -m orchestrator.cli                    # 上帝视角看一局彩色日志
uv run python -m orchestrator.cli --view 3           # 换成 3 号玩家的视角（看不到底牌与私有频道）
uv run python -m orchestrator.cli --games 50 --summary   # 批量压规则，只看结果
uv run python -m orchestrator.cli --delay 0.3        # 放慢节奏，一屏一屏地看
```

接入 LLM 后的目标形态（尚未实现）：

```bash
uv run python -m server.app          # 启动服务
cd web && npm install && npm run dev # 打开前端
```

## 对局记录

每局保存在 `data/games/{game_id}/` 下，全部是 JSON：

```
events.jsonl    进行中增量追加（崩溃安全）
events.json     结束时归档的完整事件数组（复盘主用）
meta.json       配置快照、模型分配、胜负结果
contexts/       每个席位的完整消息历史快照（可选）
```

## 架构一瞥

```
React 前端 (观战 / 回放)
        │ WebSocket
FastAPI 服务层
        │
Orchestrator（对局编排）
   ┌────┴────┐
规则引擎      Agent 运行时      ← 两者互不知晓
(确定性)      (LLM + 多轮工具循环)
   └────┬────┘
     EventLog（全 JSON）      ← 唯一事实来源，直播与复盘共用
```

核心思路：**确定的归确定，随机的归 LLM**。发牌、结算、胜负判定全部是纯代码、可单测；
说什么话、投谁、救不救交给模型。两者之间只有一个窄接口：阶段 + 动作。

详细设计见 [DESIGN.md](./DESIGN.md)。

## 技术栈

| 层 | 选型 |
| --- | --- |
| 引擎 / 服务 | Python 3.12+ · uv · asyncio · Pydantic v2 · FastAPI |
| LLM 接入 | OpenAI 兼容协议（官方 SDK 改 `base_url`） |
| 存储 | 本地 JSON / JSON Lines，无二进制数据库 |
| 前端 | React 19 · TypeScript · Vite · Tailwind · zustand |

## 路线图

- [x] M0 仓库骨架与配置样例
- [x] M1 规则引擎（纯确定性 + 单测，含信息隔离测试）
- [x] M2 CLI 随机策略跑通整局（彩色输出 + 事件流落盘 JSON）
- [x] M3 接入 LLM、多轮工具循环、CoT 捕获（记忆工具已随 M3 提前实现）
- [ ] M5 FastAPI + WebSocket 服务化
- [ ] M6 前端实时观战 + CoT 面板
- [ ] M7 回放与视角切换
- [ ] M8 遗言、赛后感想与打磨

## 文档

- [DESIGN.md](./DESIGN.md) — 设计基准文档，开发以此为准
- [AGENTS.md](./AGENTS.md) — 给 AI agent 的工作约定

## License

MIT
