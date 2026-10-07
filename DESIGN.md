# DESIGN.md — werewolf-arena-llm 设计文档

> 本文件是本项目开发的**唯一设计基准**。任何设计变更都必须同步更新本文件，并在
> 文末「变更记录」中追加一条。实现与本文档冲突时，以本文档为准并先讨论再改代码。

---

## 1. 项目定位

**一句话**：一个让 9 个大语言模型坐在一桌打狼人杀、供人类实时观战的娱乐产品。

### 1.1 目标（Goals）

| 编号 | 目标 | 说明 |
| --- | --- | --- |
| G1 | 规则绝对权威 | 所有胜负判定、合法性校验由确定性规则引擎负责，LLM 只做「决策」不做「裁判」。 |
| G2 | 严格信息隔离 | 每个 agent 只能看到其角色应知的信息；狼人夜间协商与任何人的 CoT 对其他玩家完全不可见。 |
| G3 | 观战体验优先 | 实时事件流 + 上帝视角（含 CoT）+ 可回放，这是产品的主要价值。 |
| G4 | 模型可插拔 | 任意一个席位都能换成不同的 LLM，只改配置不改代码。 |
| G5 | 完整复盘 | 思考、发言、决策、记忆、赛后感想全部持久化，事后可逐条回看。 |

### 1.2 非目标（Non-Goals）

- **不做**人类玩家入局（9 席全 LLM，人类只观战）。
- **不做**警长/警徽竞选机制。
- **不做**多用户账号体系、云端部署、付费。单机本地运行。
- **不做**LLM 训练/微调，只做推理调用。
- **不做**战绩统计与 ELO 评级。
- **不做**多局同屏对比观看。
- **不使用**任何二进制数据库（SQLite 等），持久化一律 JSON。

---

## 2. 可行性分析

**结论：可行，风险可控，适合用「确定性状态机 + LLM 决策器」的分离架构来做。**

### 2.1 为什么可行

1. **边界天然清晰**。狼人杀可以干净地切成两半：
   - *确定性部分*（发牌、阶段推进、技能结算、投票计票、胜负判定）→ 纯代码，零歧义，可单测。
   - *不确定性部分*（说什么话、投谁、刀谁、救不救）→ 交给 LLM。
   这两部分通过「阶段（Phase）+ 动作（Action）」的窄接口交互，耦合面极小。

2. **LLM 的输出可以被强约束**。现代模型普遍支持 function calling / tool use。
   把每个阶段的合法动作定义成工具，模型就无法「用自然语言糊弄过去」；
   非法参数（目标已死、选择自己）由引擎在 tool result 中返回错误并让模型重选。

3. **接入成本极低**。OpenAI 兼容协议（`base_url` + `model` + `api_key`）已成为事实标准，
   OpenAI / DeepSeek / 通义千问 / Kimi / GLM / vLLM / Ollama / LM Studio 全都能一套客户端覆盖。

4. **规模适中**。9 人局通常 3–4 个昼夜。粗算单局 LLM **往返**次数（一次往返 = 一次 API 调用）：

   | 阶段 | 往返次数 | 说明 |
   | --- | --- | --- |
   | 夜间·狼人协商 | 3 狼 × 2 轮 × ~1.5 ≈ 9 | 含记忆工具额外往返 |
   | 夜间·狼人出刀投票 | 3 × 1.5 ≈ 5 | |
   | 夜间·预言家 / 女巫 | 2 × 1.5 ≈ 3 | |
   | 白天·讨论 | 存活人数 × 2 轮 × 1.5 ≈ 18 | 逐轮递减 |
   | 白天·投票 | 存活人数 × 1.5 ≈ 11 | |
   | 遗言 / 赛后感想 | ≈ 12 | |
   | **单个昼夜合计** | **≈ 45** | |
   | **单局（3.5 昼夜）** | **≈ 150–200** | |

   > 系数 1.5 是记忆工具带来的额外往返。若关闭 `agent.memory.enabled`，单局约 100–130 次。
   > 并发请求下 wall-clock 主要受「阶段内最慢的一次调用 × 阶段数」支配。

### 2.2 主要风险与对策

| 风险 | 严重度 | 对策 |
| --- | --- | --- |
| R1 LLM 不遵守指令（不调工具 / 调错工具 / 参数非法） | 高 | 多轮工具循环 + 参数合法性校验在 tool result 内返回错误重试；超过 `max_tool_turns` 后引擎按默认策略兜底（§8.5）。 |
| R2 上下文膨胀，长局超出窗口或成本飙升 | 中 | 每个 agent 独立历史；公开历史滑动窗口 + 超阈值摘要压缩（§7.5）。 |
| R3 **信息隔离被破坏**（最危险） | **极高** | 引擎层强制可见性过滤（§6）；私有历史物理不共享对象；单测断言「狼人历史不含神职私有信息」。 |
| R4 **CoT 泄漏进 agent 上下文** | **极高** | `thinking` 字段写入事件流时标记为 `spectator`；构建 agent 上下文时物理排除。单测覆盖。 |
| R5 单局 wall-clock 过长（观战等得无聊） | 中 | 同一阶段内无依赖的调用并发（`asyncio.gather`）；前端「正在思考」骨架屏；提供暂停/单步/快进。 |
| R6 记忆工具导致往返次数上升 | 中 | `max_tool_turns: 4` 硬上限；`agent.memory.enabled` 可关闭；prompt 引导「仅在关键时记录」。 |
| R7 CoT 捕获不到（provider 差异） | 中 | 多源捕获 + prompt 强制「先思考再调用工具」；`thinking.require_think_tool` 兜底模式；捕获失败时 UI 明示而非静默留空。 |
| R8 **CoT 回传规则各家冲突**（有的强制、有的回传即 400） | **高** | 配置驱动的适配层：`thinking.field` + `replay`（boolean）+ `extra_body`；启动校验 + 运行时降级（§7.4.2、§7.4.3）。 |
| R9 回传 CoT 计入 input token，长局上下文膨胀、成本上升 | 中 | `thinking.replay_window` 限制回传轮数；**持久化始终记录全量**，与回传解耦。 |
| R10 API key 泄漏到仓库 | 高 | 真实配置文件全部 gitignore（§3.2）；明文 key 用 `SecretStr` 承载，`repr` / `model_dump` / 异常信息一律不回显；启动日志只打脱敏结果（`api_key_report`）。 |

---

## 3. 语言与配置约定

### 3.1 语言约定

**面向人阅读的一切使用简体中文**；**机器协议标识符保持英文**。

| 类别 | 语言 | 例子 |
| --- | --- | --- |
| 项目文档（DESIGN / README / AGENTS） | 中文 | — |
| 代码注释、docstring | 中文 | `# 女巫首夜可自救，之后不可` |
| LLM system prompt 与 user 消息 | **中文** | 「你是 5 号玩家，身份：预言家。」 |
| 前端 UI 文案 | 中文 | 「夜间 · 狼人协商（仅狼人可见）」 |
| 事件流中的人工可读文本（`render.text`、发言、记忆、感想、CoT） | 中文 | — |
| 代码标识符（类名 / 函数名 / 变量名） | 英文 | `RuleEngine`, `apply_action()` |
| **协议字段与枚举值**（event `type`、`visibility`、tool 名、配置 key） | **英文** | `speech`, `private:wolf`, `witch_poison`, `witch.self_save` |

**为什么枚举值保持英文**：event `type` 会同时出现在 Python 引擎、JSON 持久化文件和前端 TypeScript 代码中，
前端需要按字面量做 `switch`；工具名要传给模型的 function schema。用中文会让跨语言映射和 JSON/TS 类型生成全部失效。

### 3.2 配置文件约定

**仓库只提交样例配置，真实配置不入库。**

```
configs/
├── board.example.yaml     # 板子与角色细则  ← 提交
├── game.example.yaml      # 对局参数        ← 提交
└── models.example.yaml    # provider 与模型  ← 提交
# 用户自行 cp 出的 board.yaml / game.yaml / models.yaml  ← 不提交
```

`.gitignore` 规则：

```gitignore
configs/*.yaml
configs/*.yml
!configs/*.example.yaml
!configs/*.example.yml
```

**启动校验（硬性）**：程序启动时按顺序检查 `configs/board.yaml`、`configs/game.yaml`、`configs/models.yaml`
是否存在且能被 schema 校验通过。任一缺失或非法 → **打印明确错误并异常退出**，绝不静默使用默认值。

错误信息必须包含可复制的修复命令，例如：

```
✗ 缺少配置文件：configs/board.yaml
  请执行：cp configs/board.example.yaml configs/board.yaml
  然后按注释修改为自己想要的板子配置。

✗ configs/models.yaml 中以下 provider 没有可用的 API key：
  deepseek：既没在配置里写 api_key，环境变量 DEEPSEEK_API_KEY 也未设置
    方式一（环境变量，推荐）：export DEEPSEEK_API_KEY=<你的 key>
    方式二（配置明文）：在 configs/models.yaml 的 providers.deepseek 下加一行
      api_key: <你的 key>          # 该文件已被 gitignore，请勿提交
```

**API key 的两条来源（优先级：配置明文 > 环境变量）**

| 来源 | 写法 | 说明 |
| --- | --- | --- |
| 配置文件明文 | `providers.{name}.api_key: sk-xxx` | 优先级最高。用 `SecretStr` 承载，`print` / `repr` / `model_dump` / 异常信息都不会回显完整 key。 |
| 环境变量 | `providers.{name}.api_key_env: DEEPSEEK_API_KEY` | 只写变量名。用于 CI、多环境切换、避免明文落盘。 |

两者都没有 → 启动报错并同时给出上述两条修复方式（联网前失败，对齐 DeepSeek Harness 的 fail-fast）。
两者都写 → **以明文为准**，启动日志里会明确标注「（环境变量已设置但被明文覆盖）」，避免用户困惑。
`api_key_env` 可留空，表示「该 provider 只用明文」。

key 只存在于进程内存与（用户自己的）配置文件中，**绝不写入事件流、`meta.json`、日志或任何持久化 JSON**；
启动日志用 `api_key_report()` 输出脱敏来源（如 `deepseek：环境变量 DEEPSEEK_API_KEY = sk-a***7890`）。

---

## 4. 游戏规则规格（9 人标准局）

### 4.1 身份配置

| 阵营 | 角色 | 人数 |
| --- | --- | --- |
| 狼人 | 狼人 Werewolf | 3 |
| 神职 | 预言家 Seer | 1 |
| 神职 | 女巫 Witch | 1 |
| 神职 | 猎人 Hunter | 1 |
| 平民 | 平民 Villager | 3 |

### 4.2 胜负条件

默认**屠边**：狼人阵营使「全部神职」或「全部平民」出局 → 狼人胜。
3 名狼人全部出局 → 好人胜。不做「情侣」「第三方阵营」。

### 4.3 夜间流程（固定顺序）

```
1. 狼人协商   → 私有群聊 N 轮（默认 2 轮）
2. 狼人出刀   → 每狼**同时**投票选袭击目标，互不看见；平票按 §4.7 决断
3. 预言家     → 查验一名存活玩家，获知「好人 / 狼人」
4. 女巫       → 得知今夜刀口；可选择用解药救 / 用毒药毒 / 弃权
5. 夜间结算   → 计算最终死亡名单；触发遗言与猎人技能
```

### 4.4 白天流程

```
1. 死亡公告   → 公布昨夜出局者（不公布死因细节）
2. 自由讨论   → 存活玩家按座位顺序发言，默认 2 轮（串行，后发言者能看到前面的发言）
3. 投票       → 全体存活玩家**同时**投票，互不看见；全部提交后一次性公开
4. 平票处理   → 见 §4.7
5. 出局结算   → 被投出者出局 → 遗言 → 若为猎人则触发 §4.6
6. 进入下一夜
```

**投票形式已定：同时投票（secret ballot）。**
投票阶段所有存活玩家并发提交，投票期间谁也看不到别人的选择，全部提交后一次性公开全部票型。
理由：符合主流规则；且能并发，单日 wall-clock 明显短于串行。
`vote.secret_ballot: false` 可切换为按座位依次公开投票（此时必须串行），保留配置以备实验。

**遗言的位置**：遗言发生在「被投出者出局」之后（步骤 5），不是白天开场。
夜间死亡者无遗言，因此白天开场没有遗言环节——步骤 2 直接从自由讨论开始。

### 4.5 女巫细则

配置键全部为 boolean，见 §4.8。默认：拥有解药与毒药各一瓶；首夜可自救、之后不可自救；
不可自毒；每晚得知刀口；毒人后不知对方身份；同晚不可两药齐用。

**解药的作用对象固定为「今夜刀口」**：女巫只能决定救或不救，**不能另选一个救人目标**。
因此 `witch_save()` 无参数。`witch.self_save` / `self_save_first_night_only` 这两个开关
**只在刀口恰好是女巫本人时才生效**（即「自救」场景）；刀口是别人时不受这两个开关限制。
若今夜空刀（狼人未产出有效刀口），则解药无从使用，女巫本夜只能毒人或弃权。

### 4.6 猎人细则

配置键全部为 boolean，见 §4.8。默认：被女巫毒死时不能开枪；其余死亡方式可开枪；可放弃开枪。

### 4.7 平票处理

| 场景 | 规则 |
| --- | --- |
| 白天投票平票 | 平票者各发言一轮（加时辩）→ 全体在平票者中重新投票 → 若二次平票，则**本日无人出局**，直接进入夜晚。 |
| 狼人出刀平票 | **全体存活狼人**再发言一轮 → 重新投票（候选项限定为平票目标）→ 二次平票则取**平票候选中座位号最小者**（确定性兜底，不再追加第三轮）。 |

### 4.8 板子配置（按角色分组，尽量 boolean）

`configs/board.yaml`（由 `board.example.yaml` 复制而来）：

```yaml
roles:
  werewolf: 3
  seer: 1
  witch: 1
  hunter: 1
  villager: 3

witch:
  has_antidote: true                # 拥有解药
  has_poison: true                  # 拥有毒药
  self_save: true                   # 可以自救
  self_save_first_night_only: true  # 自救仅限首夜
  self_poison: false                # 不可以毒死自己
  knows_kill_target: true           # 每晚得知今夜刀口
  knows_poisoned_identity: false    # 毒人后是否得知对方身份
  same_night_both_potions: false    # 同一晚不可同时使用解药和毒药

hunter:
  shoot_on_lynch: true              # 被投票出局时可开枪
  shoot_on_kill: true               # 被狼人刀死时可开枪
  shoot_on_poison: false            # 被女巫毒死时不能开枪
  can_hold_fire: true               # 可以放弃开枪

seer:
  check_every_night: true           # 每晚可查验一人
  reveal_faction_only: true         # 只告知「好人/狼人」，不告知具体角色
  can_check_self: false             # 不可查验自己
  can_check_dead: false             # 不可查验已出局者

werewolf:
  chat_rounds: 2                    # 夜间私有协商轮数
  vote_kill: true                   # 出刀需全体狼人投票
  tie_break_by_lowest_seat: true    # 二次平票按最小座位号决断
  can_kill_teammate: false          # 不可刀自己的狼队友
  can_kill_self: false              # 不可刀自己

death:
  reveal_role: false                # 出局不翻牌（身份仅观众可见）
  last_words_on_lynch: true         # 被票出局者有遗言
  last_words_on_night_death: false  # 夜间死亡者无遗言
  last_words_on_first_night: false  # 首夜死亡者无遗言

vote:
  runoff_on_tie: true               # 平票进入加时辩
  no_lynch_on_second_tie: true      # 二次平票则本日无人出局
  secret_ballot: true               # 同时投票，互不看见；false = 按座位依次公开投票（串行）
  allow_abstain: true               # 允许弃权（弃权票不投给任何人，但计入「已投票」）

day:
  discussion_rounds: 2              # 白天讨论轮数
  speech_max_chars: 300             # 单条发言字数上限

win:
  eliminate_one_faction: true       # 屠边（神职全灭或平民全灭即狼人胜）

postgame:
  enabled: true                     # 赛后感想环节
  reveal_all_roles: true            # 感想前向全体揭示真实身份
```

> 新增角色时，在对应分组下追加 boolean 键，并在 `engine/rules.py` 中新增分支 + 单测。

### 4.9 赛后感想环节

公布胜负后进入 `postgame` 阶段：**全部 9 名选手**（含已出局者）按座位顺序逐一发表感想。
默认在感想开始前向所有 agent 揭示全部真实身份（`postgame.reveal_all_roles: true`），
否则已出局的玩家无从谈起。感想内容公开可见，并完整持久化。

---

### 4.10 阶段枚举（Phase）

引擎的状态机阶段，英文枚举值（`event.phase` 字段直接用它）：

```
night_wolf_chat           夜间·狼人私有协商
night_wolf_vote           夜间·狼人出刀投票
night_wolf_runoff_speech  夜间·出刀平票后的加时协商
night_wolf_runoff_vote    夜间·出刀平票后的重投
night_seer                夜间·预言家查验
night_witch               夜间·女巫用药
night_resolve             夜间·结算（无人行动，引擎自动推进）
night_hunter_shoot        夜间·猎人死亡触发开枪
day_announce              白天·昨夜死亡公告（无人行动）
day_discussion            白天·自由讨论
day_vote                  白天·投票
day_runoff_speech         白天·平票者加时辩
day_runoff_vote           白天·平票重投
day_resolve               白天·出局结算（无人行动）
last_words                遗言
hunter_shoot              白天·猎人开枪
postgame                  赛后感想
game_over                 终局
```

补充说明：
- **猎人夜间死亡时在夜间就触发开枪**（`night_hunter_shoot`），结果计入当夜死亡名单，随
  `day_announce` 一起公布。这与 §4.3 步骤 5「夜间结算触发猎人技能」一致。
- 需要 LLM 行动才推进的阶段，由 Orchestrator 向 `legal_actions()` 列出的席位索要动作；
  **无人行动的阶段**（`night_resolve` / `day_announce` / `day_resolve`）由引擎自动结算推进。

### 4.11 投票事件的可见性

为保证「投票期间互不看见」（§4.4），投票事件分两段：

| 事件 | 时机 | 可见性 |
| --- | --- | --- |
| `vote_cast` / `vote_abstain` | 逐个提交时 | **spectator**——仅观众可见，任何 agent 都看不到 |
| `vote_result` | 全部收齐、计票完成后 | 白天投票 = `public`；狼人出刀 = `private:wolf` |

即：**agent 永远只能看到「计完票的结果」，看不到「谁投了谁」的中间过程**。
观众能看到全部细节（这也正是观战的价值）。

同理，夜间死亡在 `night_resolve` 时以 **spectator** 可见性写入 `death` 事件
（夜里没人该知道），到 `day_announce` 才以 `public` 的 `night_deaths_announced` 公布名单，
且不公布死因。白天被票出局 / 被猎人开枪的 `death` 则直接是 `public`。

---

## 5. 总体架构

### 5.1 分层

```
┌─────────────────────────────────────────────────────────────┐
│  Web 前端  (React + Vite + TS)                              │
│  大厅 / 对局房间 / 回放页        —— WebSocket 订阅事件流     │
└───────────────────────────┬─────────────────────────────────┘
                            │  WS: /ws/games/{game_id}
┌───────────────────────────▼─────────────────────────────────┐
│  Server 层  (FastAPI + uvicorn)                             │
│  REST: 创建对局 / 查询历史 / 加载回放                        │
│  WS:   实时推送事件、接收观战控制指令（暂停/单步/调速）      │
└───────────────────────────┬─────────────────────────────────┘
                            │
┌───────────────────────────▼─────────────────────────────────┐
│  Orchestrator  对局编排器                                   │
│  推进阶段状态机 → 向 agent 索要动作 → 交回引擎结算 → 发事件  │
│  （唯一同时认识「引擎」和「LLM」的层）                       │
└──────────┬──────────────────────────────────┬───────────────┘
           │                                  │
┌──────────▼──────────────┐      ┌────────────▼───────────────┐
│  RuleEngine 规则引擎     │      │  AgentRuntime  Agent 运行时 │
│  · 纯确定性，无 I/O      │      │  · OpenAI 兼容客户端        │
│  · 状态机 + 合法性校验   │      │  · 每 agent 独立历史        │
│  · 动作结算 + 胜负判定   │      │  · 多轮工具循环 + 重试      │
│  · 生成可见性视图        │      │  · CoT 捕获 / 上下文压缩    │
└──────────┬──────────────┘      └────────────┬───────────────┘
           │                                  │
┌──────────▼──────────────────────────────────▼───────────────┐
│  EventLog + Persistence  （全 JSON，无二进制数据库）         │
│  唯一事实来源；实时观战与赛后复盘共用同一份数据               │
└─────────────────────────────────────────────────────────────┘
```

### 5.2 关键设计原则

1. **RuleEngine 是纯函数式的**：输入 `(state, action)` → 输出 `(new_state, events)`。
   不 import 任何 LLM / 网络 / 文件代码。因此可以被完整单测覆盖。
2. **EventLog 是唯一事实来源**：任何 UI 状态都由事件流推导，不额外维护影子状态。
   实时观战与赛后回放因此走同一条代码路径。
3. **Orchestrator 是唯一的「脏」层**：只有它同时依赖引擎和 LLM。引擎与 LLM 互不知晓对方存在。
4. **可见性是一等公民**：每个 event 带 `visibility` 字段；引擎按此过滤后再交给 agent。
5. **持久化无二进制依赖**：一切落盘都是 JSON（JSON Lines 或 JSON 数组），不用 SQLite 等。
6. **provider 差异只存在于配置里**：内部表示归一化，出口处按配置序列化（§7.1）。
   代码里不出现 `if provider == "..."`。

### 5.3 策略缝（Policy Seam）

Orchestrator 不直接依赖 LLM，而是依赖一个极窄的接口：

```python
class Policy(Protocol):
    def decide(self, state: GameState, legal: list[LegalAction]) -> Action: ...
```

- 入参 `legal` 只含**单个席位**在当前阶段的合法动作（含 `allowed_targets`）。
  编排层按席位分组后逐个调用，策略无权改动别人的选项。
- 出参就是一个 `Action`，和引擎唯一认的输入类型一致。

这条缝的意义：**M2 用 `RandomPolicy` 顶替 LLM 把整条链路跑通，M3 换成
`LLMPolicy` 时编排层的同步主循环一行都不用改**。随机策略故意会做出各种蠢事
（自刀、毒队友、弃权），正好用来压规则边界。

**同时投票的调度约定**：一个阶段内所有待行动席位都基于**同一份状态快照**
决策，先决策者的选择不会进入后决策者的上下文；动作收齐后才逐个交给引擎
结算。「投票期间互不看见」就是靠这一条保证的，不是靠事后过滤。

#### 5.3.1 M3 扩展：批量决策与 `Decision`

同步的 `decide()` 装不下 LLM 的两样东西：**并发**（同一阶段多个席位要同时打 API）
和**元信息**（CoT、token 用量、工具轮数、记忆读写这些都要落盘，见 §8.4）。
因此增加一条**可选**的批量缝，只有 LLM 策略实现它：

```python
class Decision(BaseModel):
    action: Action  # 交给引擎的仍然是同一个 Action
    thinking: Thinking | None = None  # CoT，恒为观众可见
    llm: LlmMeta | None = None  # provider / model / 延迟 / token / 轮数 / fallback
    side_events: list[Event] = []  # 记忆读写、被拒动作等，编号后并入事件流


class BatchPolicy(Protocol):
    def decide_all(self, state: GameState, legal: list[LegalAction]) -> list[Decision]: ...
```

- 编排层**优先**用 `decide_all`，没有实现就退回逐个 `decide()`（M2 的同步测试因此不受影响）。
- `side_events` 在动作事件**之前**编号入流（先记下记忆、再行动，顺序才符合直觉）。
- `thinking` / `llm` 由编排层**附加到该动作产生的第一个事件**上，而不是另起一条事件——
  CoT 与它导致的那个动作必须绑在一起，否则复盘时会对不上。
- `fallback: true` 由工具循环写进 `llm` 元信息（§8.5），前端据此打标记。

#### 5.3.2 并发：同步外壳 + 内部 asyncio 批处理

编排层主循环保持**同步**（M2 的测试与 CLI 都建立在它上面），LLM 策略在
`decide_all` 内部用一次 `asyncio.run` + `asyncio.gather` 把本阶段所有席位并发打完，
受 §7.7 的信号量约束。这样：

- 同步策略与异步策略共用同一个 `GameRunner`；
- 单次 `asyncio.run` 的开销（一局约 30 批）可忽略；
- **M5 实际落地（见 §14 v0.10）**：没有把主循环整体 async 化，而是让一局对局跑在
  **独立守护线程**里（见 `server/ws.py`）。原因——服务层要同时服务 REST 请求与
  WebSocket 推送，若用 `loop.run_in_executor` 把同步 `GameRunner` 丢进事件循环线程池，
  在「请求间隙循环被暂停」的网关 / 测试环境下后台任务会卡死。独立线程不依赖循环是否被
  持续驱动，最稳。`decide_all` 内部那次 `asyncio.run` 在独立线程里照常工作；实时推送改用
  普通线程安全队列（`queue.Queue`），WebSocket 处理器在自己的循环上用
  `loop.run_in_executor(None, q.get)` 取批次下发，从而与「每次请求新 loop」的测试环境也兼容。

### 5.4 兜底链

1. 策略产出非法动作 → 引擎 `IllegalActionError` → 编排层记一条 `Rejection`，
   改用 `FirstLegalPolicy`（取最小目标）继续，流程不中断。
2. M3 起，非法动作会先在工具循环里带着原因回给模型重试，重试次数用尽才走到上面这条。
3. 任何兜底都要留痕：M2 记在 `GameRunner.rejections`，M3 起写成 `action_fallback`
   事件（§8.5）。**静默兜底等于埋雷。**

---

## 6. 信息隔离模型（核心安全设计）

这是整个项目最容易出错、后果最严重的部分，单独成章。

### 6.1 可见性枚举

```python
class Visibility(str, Enum):
    PUBLIC = "public"  # 所有人可见（发言、投票、遗言、感想、死亡公告）
    WOLF = "private:wolf"  # 仅狼人可见（夜间协商）
    SEER = "private:seer"  # 仅预言家可见（查验结果）
    WITCH = "private:witch"  # 仅女巫可见（刀口信息）
    SELF = "private:self"  # 仅行动者本人可见（记忆读写）
    SPECTATOR = "spectator"  # 仅观众可见（真实身份底牌、**全部 CoT**）
```

### 6.2 CoT（思考过程）的隔离规则

| 对象 | 能否看到 CoT |
| --- | --- |
| 观众 / 前端上帝视角 | ✅ 能，且是产品卖点 |
| 发出该 CoT 的 agent 本人 | ✅ 能（自己的思考历史，是否回传由 `thinking.replay` 统一决定，见 §7.4.2） |
| **其他任何 agent** | ❌ **绝对不能** |

实现约束：
- `thinking` 字段写入事件流时，其可见性**恒为 `spectator`**，不受事件本身 `visibility` 影响。
- 构建 agent 上下文时，从事件流渲染文本这一步**物理跳过** `thinking`（不是「渲染但打码」，是根本不读）。
- 单测断言：随机对局结束后，任一 agent 的 `messages` 序列化文本中不包含任何其他 agent 的 `thinking` 子串。
- **回传只走结构化字段**：agent 自己的 CoT 若需回传，必须放在 assistant 消息的 `reasoning_content` 字段，
  不得拼进 `content`，更不得进入任何渲染文本。详见 §7.4.2。

### 6.3 各角色的可见信息

| 角色 | 额外可见信息 |
| --- | --- |
| 狼人 | 3 名狼队友身份；狼人夜间协商全文 |
| 预言家 | 自己每夜的查验结果 |
| 女巫 | 每晚的刀口是谁；自己的用药记录 |
| 猎人 | 无额外私有信息 |
| 平民 | 无额外私有信息 |
| 所有人（含已出局者） | 白天发言、投票结果、遗言、死亡公告、存活列表、赛后感想 |
| **已出局者** | **继续旁观后续全部公开信息**（见 §6.5），但不再收到任何私有频道内容 |
| 仅观众 | 全部真实身份、全部私有频道内容、**全部 CoT** |

### 6.4 实现约束（硬性）

1. **物理隔离**：每个 agent 持有**独立的** `AgentContext` 对象，内含自己的 `messages`。
   引擎**从不**把同一个 list 交给两个 agent。狼人协商通过显式的「广播到 wolf channel」完成。
2. **过滤发生在引擎侧**：`engine.visibility.view_for(state, seat) -> ObservationView`。
   Agent 拿到的永远是过滤后的视图，**不存在**「先给全部再让 agent 自己不看」的实现方式。
3. **私有事件不进公开历史**：`private:wolf` / `private:seer` / `private:witch` 事件
   不会出现在好人 agent 的 history 里，**也不会出现在已出局玩家的 history 里**——
   已出局者继续旁观的是**公开**部分（§6.5），私有频道对他是永久关闭的。
4. **单测强制**（`tests/test_visibility.py`）：
   - 狼人 history 不含 `seer_check` 结果、不含女巫用药信息；
   - 好人 history 不含任何 `private:wolf` 内容；
   - **任何 agent history 不含他人的 `thinking`**。
5. **观众视角单独渲染**：前端上帝视角直接读全量事件流，这是 UI 层的事，不经过 agent 通道。

### 6.5 已出局玩家的信息流（已定）

**已定：出局后继续旁观后续全部公开信息。**

即玩家出局后，`view_for(state, seat)` 仍然返回 `visibility == public` 的全部事件
（白天发言、投票、死亡公告、他人遗言），但不返回任何 `private:*` 事件——
包括他**生前**所属阵营的私有频道（狼人出局后也不再看到狼队协商）。

这样到了赛后感想阶段，出局者手里有完整的公开叙事 + 自己的私有记忆 + 揭示的真实身份，
感想才有内容可谈。若选择「出局即冻结」，感想就只剩身份揭示和结果，会明显干瘪。

实现要点：
- 出局是 `Player.alive = false`，**不是**把 agent 从调度表里摘掉。
- 调度时按阶段区分：只有存活者进入讨论/投票/夜间行动的调度队列；
  出局者只「被动接收」public 事件，不产出动作（遗言与赛后感想除外）。
- 单测：狼人出局后其 history 不再新增 `private:wolf` 事件，但仍新增 `public` 事件。

---

## 7. Agent 运行时设计

### 7.1 LLM 接入：Provider Adapter 边界

统一走 **OpenAI 兼容协议**，但**不为每家写 adapter 类**——而是用一个「归一化 + 出口序列化」的
**配置驱动适配层**：

- **内部只有一套表示**：assistant 消息里的 CoT 是一等字段 `reasoning`，与 `content`、`tool_calls` 平级。
- **出口由配置决定序列化成什么**：字段名、是否回传、各家私有开关，全部写在 `configs/models.yaml` 里。
- **不允许在代码里按 provider 名字 if-else**（唯一例外见 §7.4.4 的按模型名模式匹配）。

这与 OpenCode 的 `ProviderTransform`、DeepSeek Harness 的 LLM adapter 是同一个思路：
内部归一化，边界处按 provider 翻译。

`configs/models.yaml`（由 `models.example.yaml` 复制）：

```yaml
providers:
  deepseek:
    base_url: https://api.deepseek.com/v1
    api_key_env: DEEPSEEK_API_KEY     # 方式一：只存环境变量名（推荐）
    # api_key: sk-xxx                 # 方式二：直接写明文，优先级高于环境变量
    thinking: { enabled: false }      # provider 级默认
    models:
      - name: deepseek-chat
      - name: deepseek-reasoner
        # 官方：带 tools 时必须回传历史 reasoning_content
        thinking: { enabled: true, replay: true }
  moonshot:
    base_url: https://api.moonshot.cn/v1
    api_key_env: MOONSHOT_API_KEY
    models:
      - name: kimi-k2.7-code
        # 官方：保留式思考始终开启、无法关闭，必须原样保留
        thinking:
          enabled: true
          replay: true
          extra_body: { thinking: { type: enabled, keep: all } }
      - name: kimi-k2.6
        thinking:
          enabled: true
          replay: true
          extra_body: { thinking: { type: enabled, keep: all } }
  qwen:
    base_url: https://dashscope.aliyuncs.com/compatible-mode/v1
    api_key_env: DASHSCOPE_API_KEY
    models:
      - name: qwen3.8-max
        # 官方：preserve_thinking 默认 true，必须完整回传
        thinking:
          enabled: true
          replay: true
          extra_body: { preserve_thinking: true }
  openai:
    base_url: https://api.openai.com/v1
    api_key_env: OPENAI_API_KEY
    models:
      - name: gpt-4o-mini
  local:
    base_url: http://localhost:11434/v1   # Ollama
    api_key_env: LOCAL_API_KEY
    models:
      - name: qwen2.5:14b

defaults:
  temperature: 0.8
  max_tokens: 1024                  # 需容纳「思考 + 工具调用」
  timeout_s: 60
  network_retries: 3
  thinking:
    enabled: false
    field: auto                     # auto | reasoning_content | reasoning | reasoning_text | 任意字段名
    replay: true                    # 有 CoT 时是否原样回传
    replay_on_error: fallback       # 回传若被拒：fallback（关闭回传重试）| fail（报错）
```

**配置优先级**（同 OpenCode 的 provider → model → variant）：
`defaults` → `providers.{p}.thinking` → `providers.{p}.models[].thinking`，后者覆盖前者，嵌套对象深合并。

字段说明：
- `field`：读写 CoT 的**字段名**。OpenCode 的 `compatibility.reasoningField` 就是这么做的——
  它接受任意 provider 私有字符串，而不是硬编码几个候选。`auto`（默认）按
  `reasoning_content` → `reasoning` → `reasoning_text` 顺序探测。
- `replay`：有 CoT 时是否把历史 `field` 原样留在 messages 里。默认 `true`。
- `extra_body`：原样合进请求体，承载各家非 OpenAI 标准的开关
  （`thinking.keep` / `preserve_thinking` / `enable_thinking` / `thinking_budget` / `reasoning_effort`）。
- `replay_on_error`：回传被 provider 拒绝时的处置（§7.4.3）。

详见 §7.4。

用官方 `openai` Python SDK，只改 `base_url` 即可切 provider。**不为单家写原生 adapter**。

### 7.2 工具协议：多轮循环（ReAct）

每个阶段向模型暴露两类工具：

| 类别 | 工具 | 调用要求 |
| --- | --- | --- |
| **动作工具**（阶段专属） | 见下表 | 本回合结束前必须调用 |
| **辅助工具**（始终可用，可选） | `memory_write(key, content)` / `memory_read()` | 可调用 0~N 次 |

| 阶段 | 动作工具 |
| --- | --- |
| 夜间·狼人协商 | `speak(message)` |
| 夜间·狼人出刀 | `wolf_vote_kill(seat)` / `abstain()` |
| 夜间·预言家 | `seer_check(seat)` |
| 夜间·女巫 | `witch_save()` / `witch_poison(seat)` / `witch_pass()` |
| 猎人触发 | `hunter_shoot(seat)` / `hunter_hold()` |
| 白天·讨论 | `speak(message)` |
| 白天·投票 | `vote(seat)` / `abstain()` |
| 遗言 | `last_words(message)` |
| 赛后感想 | `reflect(message)` |

**关于 `abstain()`**：弃权是**显式合法动作**，不是只在兜底时才发生的行为
（`vote.allow_abstain: true` 时暴露该工具；关闭后不暴露，超时才走 §8.5 的兜底弃权）。
这么做是为了区分「模型主动选择弃权」和「模型卡住被兜底」——
前者是正常的策略（好人阵营信息不足时弃权很常见），后者要打 `fallback: true` 标记。

**循环规则**：
1. 向模型发起请求，同时传入动作工具与辅助工具。
2. 若返回的 tool call 是辅助工具 → 执行、把结果追加进消息、再次请求（**不推进阶段**）。
3. 若返回的是动作工具 → 校验参数；合法则结算并结束本回合；非法则在 tool result 里
   返回明确原因（如「3 号已出局，请在 1、2、5、7 中选择」）并再次请求。
4. 轮数超过 `agent.max_tool_turns`（默认 4）仍未产出合法动作 → 走兜底（§8.5）。
5. **不使用 `tool_choice="required"`**，因为必须先允许模型调用辅助工具。
6. 模型「只发言、没调任何工具」→ **先把这段发言写进历史**，再追加一条**点名本回合
   可用工具**的纠正提示，多给一轮。两个约束都有实测依据：
   - **不能静默丢弃发言**：丢了之后历史会退化成连续多条 `user`（一局实测堆出 8 条），
     而且模型看不到自己刚说过什么，只会把同一段话原样再说一遍，白烧到轮数用尽。
   - **纠正提示必须点名工具**：泛泛的「请调用一个工具」对弱模型无效。同理，
     `prompts.phase_instruction` **每条分支都必须点名本阶段至少一个工具**——
     只写「说出你的建议」时，9B 模型会老老实实写完一段发言就结束回合（它以为
     自己已经说过话了），整轮 4 次请求全部白跑（真实教训见 §14 v0.22）。

### 7.3 记忆工具

| 工具 | 签名 | 说明 |
| --- | --- | --- |
| `memory_write` | `(key: str, content: str)` | 写入或覆盖一条记忆。同 key 覆盖。 |
| `memory_read` | `()` | 返回自己的全部记忆条目（key + content 列表）。 |

约束：
- 记忆是 **agent 私有**（`visibility: private:self`），其他任何玩家都看不到，
  但**写入事件流**供观众复盘。
- 记忆仅存在于 agent 上下文与持久化记录中，**不参与任何规则结算**。
- 上限保护：`agent.memory.max_entries`（默认 20）、`max_chars_per_entry`（默认 200），
  超出时 `memory_write` 返回错误提示模型先精简。
- 玩家出局后其记忆仍保留在其上下文中（赛后感想阶段仍可读取）。
- `agent.memory.enabled: false` 可整体关闭（此时不暴露这两个工具，单局往返数下降约三分之一）。

### 7.4 CoT：捕获、回传、记录（三件事，必须分开）

这是本项目最容易混淆的一处。对同一份 CoT，引擎要做三件**互相独立**的事：

| 动作 | 做什么 | 能否关闭 |
| --- | --- | --- |
| **捕获** capture | 从响应里把 CoT 读出来，归一化为内部的 `reasoning` 一等字段 | 否 |
| **回传** replay | 把历史 assistant 消息的 CoT 按其**原字段名**留在下一次请求的 messages 里 | 按模型配置 |
| **记录** persist | 写进事件流供观众复盘 | **否，永远全量记录** |

**记录无条件做**（用户明确要求，也是产品价值所在）；**回传取决于 provider 协议**。
三者必须解耦：即使某模型 `replay: false`，事件流里 `thinking.text` 仍然完整。

#### 7.4.1 捕获

1. **原生推理字段**：按配置的 `thinking.field` 读取。`auto`（默认）按
   `reasoning_content` → `reasoning` → `reasoning_text` 探测；也可显式指定任意 provider 私有字段名。
2. **正文**：读 assistant message 的 `content`。
3. `thinking.require_think_tool: true` 时额外暴露 `think(text)` 工具，模型必须先调用再行动
   （用于那些在 tool_calls 时总是把 `content` 留空的模型）。
4. 全部为空 → `thinking.source = "none"`，前端显示「（该模型未输出思考）」，**不静默留空**。

对**没有**原生推理能力的模型（`thinking.enabled: false`），在 system prompt 中要求它调用工具之前
先用中文写下思考过程——这段正文就是 CoT。
对**有**原生推理能力的模型**不加**这条要求，避免重复浪费 token。

> **OpenAI o 系列附注**：Chat Completions 兼容接口下不返回推理明文，因此**无明文可捕获**。
> OpenCode 的做法值得留意——它改用 Responses API，关掉服务端存储（`store: false`），
> 用 `reasoning.encrypted_content` 这个**不透明 blob** 原样回传推理状态：
> 状态保住了，但平台侧也读不到明文。若将来要接这类模型又要保留推理连贯性，走这条路。

#### 7.4.2 回传（replay）

各家规则实测不一致，而且**不是风格偏好，是协议要求**：

| Provider / 模型 | 官方规则 | `replay` |
| --- | --- | --- |
| DeepSeek 思考模式 | 请求**带 `tools` 时**历史各轮 CoT 应回传并拼进上下文；不带 `tools` 时不需要、传了也被忽略 | `true`（我们全程带 tools） |
| DeepSeek 旧版 `deepseek-reasoner` | 输入 messages 中出现 `reasoning_content` 会返回 **400** | `false` |
| Kimi `kimi-k2.7-code` | 保留式思考始终开启、无法关闭，**必须**（非可选）原样保留 | `true` |
| Kimi `kimi-k2.6`（`thinking.keep: "all"`） | 开启后应原样保留，否则多步工具调用会掉质量 | `true` |
| Qwen `qwen3.8-max` / `qwen3.8-flash` | `preserve_thinking` 默认 **true**，**必须**完整回传 | `true` |
| Qwen 其他（`preserve_thinking` 默认 false） | 传了不报错，但计入 input token | `true` |
| OpenAI o 系列 | 兼容接口下不返回推理内容 | 无 CoT 可传 |

**`replay` 是 boolean，不是多值枚举。** 早期设计里用过 `auto|required|ignored|forbidden` 四值枚举，
实测下 `auto` 与 `required` 在实现上无法区分、`ignored` 与 `enabled: false` 语义重叠，收敛为 boolean 更清晰。

**硬性实现约束**：
- 回传必须放在 assistant 消息的**原字段名**里（`reasoning_content` 或配置指定的 `field`），
  **绝不能拼进 `content`**。Qwen 官方明确指出拼进 `content` 会掉效果。
- 只能回传**该 agent 自己**的 CoT。每个 agent 的 history 本来就独立，天然满足；
  任何「把别人的 CoT 也想办法塞进去」的念头都是 bug。
- 回传的 CoT **计入 input token 并计费**（Kimi、Qwen 官方均明示）。

#### 7.4.3 失败处置：先启动校验，再运行时降级

两层，缺一不可：

1. **启动校验（对齐 DeepSeek Harness）**。DSH 的 LLM adapter 在遇到不支持的推理强度时，
   会以 `UNSUPPORTED_REASONING_EFFORT` **在发起任何网络 I/O 之前**失败。
   我们也这么做：加载 `models.yaml` 时校验 `thinking` 配置组合是否自洽
   （如 `enabled: false` 却配了 `replay: true`、`field` 与 `extra_body` 冲突等），
   有问题**启动即退出**，不要等跑到半夜第二局才炸。
2. **运行时降级（对齐 OpenCode 的宽容）**。对未列出的 provider，默认 `replay: true` 原样回传。
   若 API 返回 4xx 且错误指向 CoT 字段，按 `replay_on_error` 处置：
   - `fallback`（默认）：剥离 CoT 重试一次，并把该模型**本局**的 `replay` 降级为 `false`，写入事件流；
   - `fail`：直接报错，交兜底流程（§8.5）。

#### 7.4.4 不在代码里按 provider 名字 if-else

Provider 差异一律走配置，不在代码里写 `if provider == "moonshot"`。
唯一允许的例外是**按模型名模式匹配做兜底**（OpenCode 也这么干——它有 `isKimiFamily()` 之类的探测），
且**仅当配置未显式指定时**才生效，配置永远优先。

#### 7.4.5 成本控制

`thinking.replay_window`（默认 `null` = 回传全部轮次）可限制只回传最近 N 轮的 CoT。
注意这与部分 provider 的「必须完整回传」要求冲突，开启等于**用推理连续性换成本**，默认关闭。
无论是否回传，**持久化始终记录全量**。

#### 7.4.6 参考：OpenCode 与 DeepSeek Harness 的做法

两者在这一点上收敛到了同一个模式，本设计即照此落地：

| 做法 | OpenCode | DeepSeek Harness | 本项目 |
| --- | --- | --- | --- |
| 抽象位置 | `ProviderTransform` 归一化层 | `ctx.llm` 适配器插件（每个 provider 一个插件，按 route id 注册，重复注册抛错） | `agents/` 内的配置驱动适配层（§7.1） |
| 内部表示 | reasoning 为一等 part | `reasoningEffort: off\|high\|max` 归一化 | CoT 为 assistant 消息的 `reasoning` 一等字段 |
| 出口翻译 | 按 provider 放到 `providerOptions.openaiCompatible.reasoning_content` / Anthropic `thinking` / Google `includeThoughts` | `high\|max` 序列化为顶层 `reasoning_effort`；`off` 序列化为 `thinking.type: disabled` 并省略 `reasoning_effort` | 按 `field` + `extra_body` 序列化 |
| 字段名可配 | `compatibility.reasoningField`，接受任意 provider 私有字符串 | — | `thinking.field`（`auto` 或显式） |
| 回传 | 读了该字段，**回传 assistant 消息时就带上该字段** | 由 adapter 决定 | `thinking.replay`（boolean） |
| 非法配置 | — | **网络 I/O 之前**抛 `UNSUPPORTED_REASONING_EFFORT` | 启动校验即退出（§7.4.3） |
| 特殊化 | 仍保留 `isKimiFamily()` 等按族探测 | 每个 provider 独立插件 | 配置优先，模型名模式匹配仅作兜底（§7.4.4） |
| 记录 | — | append-only session log，Trajectory 可**逐来源**回看 | 事件流 + `contexts/{seat}.json`（§8） |

顺带印证了本设计已有的两处选择：回传必须走独立字段而非拼进 `content`；
以及「模型能看到的一切都记进 append-only 日志」——DSH 把 prompt、推理、工具调用与结果、
上下文注入全部记录，Trajectory 里逐来源回看，与我们的 `contexts/{seat}.json` 是同一件事。

### 7.5 上下文管理

每个 agent 的 history 由四部分拼装：

```
[system]  角色身份 + 规则摘要 + 你的私有信息 + 行为准则（中文）
[history] 公开历史（按可见性过滤、且剔除 thinking 后渲染成文本）
[history] 私有记忆（查验结果、用药记录、狼队协商、自己的记忆条目）
[user]    当前阶段指令（中文）：「现在是第 2 天白天讨论第 1 轮，轮到你发言。」
```

**压缩策略**（`context.max_public_events`，默认 60）：
- 超出时对最早的公开事件做**滑动窗口丢弃**，并在丢弃处插入一行模板摘要：
  「（前情提要：第 1 天 4 号被投出局，2 号与 7 号互相指控……）」。
- MVP 阶段摘要由模板规则生成（死亡公告 + 投票结果 + 每轮首句），不额外调 LLM。

**M8 实现**：公开历史仍在**增量追加**的基础上做**滑动窗口压缩**（`agents/context.py`）——
累计渲染的公开事件一旦超过 `max_public_events`，就从**最早的回合**开始，把该回合
user 消息里的「自上次行动以来：…」事件文本整段丢弃（保留局面与指令，不破坏
user/assistant/tool 消息结构），并把其中值得保留的行累积成一行模板摘要，插在 system 之后：

> （前情提要：2 号：我是第一个发言的；3 号出局。）

摘要最多保留 `DIGEST_MAX_LINES = 20` 行；`max_public_events <= 0` 表示不压缩。
压缩是**原地**改写 `messages`，故 `contexts/{seat}.json` 快照即「模型实际看到的内容」。
- 自己的 CoT 是否留在上下文里由 `thinking.replay` 统一决定（§7.4.2），不再单独设开关：
  有原生推理的模型走结构化字段回传，无原生推理的模型其正文 CoT 本就在历史里。

### 7.6 System Prompt 结构（中文）

```
# 你是谁
你是 5 号玩家，身份：预言家。你的目标是让好人阵营获胜。

# 本局板子
9 人局：3 狼人、1 预言家、1 女巫、1 猎人、3 平民。当前规则下无警长和警徽竞选。
- 夜晚：狼人先私密协商 2 轮，再由全体狼人投票决定刀口（平票取座位号最小者）；随后预言家查验、女巫用药。
- 白天有 2 轮自由讨论（单条发言不超过 300 字），然后投票放逐。
- 投票：所有人同时投票，投票期间看不到别人投给谁；平票则加时辩一轮后重投，再次平票则本日无人出局；允许弃权。
- 出局：出局只公布座位号，不公布身份，也不公布死因。只有被投票出局的人有遗言。
- 胜负：屠边——神职全部出局或平民全部出局即狼人获胜，狼人全部出局则好人获胜。

# 当前局面
第 2 天 · 白天讨论
存活玩家：1、2、3、5、6、7、8（你是 5 号）
已出局：4、9

# 你的私有信息
- 第 1 夜查验 3 号：狼人
- 第 2 夜查验 7 号：好人

# 与你相关的规则
（只列与预言家相关的部分，不泄露其他角色的私有机制）

# 行为准则
- 行动前，先用中文写下你的思考过程。这段思考只有观众能看到，其他玩家看不到。
  （仅对无原生推理能力的模型注入此条；有原生推理的模型不重复要求，避免浪费 token。）
- 然后通过调用工具来行动，不要只在回复里说话。
- 你可以用 memory_write 记录重要信息，用 memory_read 读取你记下的内容。
- 发言不超过 300 字，说人话，像一个真实的玩家，可以撒谎、可以伪装。
```

**「本局板子」一节由 `agents/prompts.py::board_brief(board)` 从 `BoardConfig` 渲染**，
只写引擎**实际兑现**的规则，绝不在 prompt 里写死一套默认规则。除身份分布与全局规则外，
它还包含两条**恒真声明**：当前规则下无警长和警徽竞选，以及出局不翻牌、不公布死因。
前者在板子 schema 里没有对应字段（§1.2 非目标），后者 `death.reveal_role` 尚未接线——
都不能靠读配置得出，但都必须显式告知模型：真实联调中模型正是在无警长的板子上
自行脑补出了「警上争夺警徽」。板子是**公开信息**（所有席位渲染结果逐字相同），
不构成信息泄露。

模板放在 **`agents/prompts.py`**（不是 `engine/prompts/`）：
`engine/` 必须保持零 I/O 的纯净性（§5 原则 3），而 prompt 模板天然属于 LLM 层。
按角色一份中文模板，运行时注入变量。

### 7.7 并发与限流

同时投票（§4.4）意味着最坏情况下 9 个席位同时打 API。因此：

- **全局并发信号量** `agent.max_concurrency`（默认 4）限制同时进行的 LLM 请求数。
  同一阶段内的 `asyncio.gather` 受它约束，超出则排队。
- **同一 provider 的并发上限** `providers.{p}.max_concurrency`（可选，默认继承全局）。
  各家限流策略不同，必要时单独调低某一家。
- **白天讨论是串行的**（后发言者要看到前面的发言），不并发；
  **投票、狼人出刀、赛后感想可以并发**。这是 §2.1 里 wall-clock 估算的前提。
- 触发限流（HTTP 429）时按 `network_retries` 指数退避重试；
  重试耗尽则走 §8.5 兜底，并写 `fallback: true`。
- **重试只认真正的传输层失败**（408/409/425/429/5xx，或 SDK 的 `APIConnectionError` /
  `APITimeoutError` / httpx 系异常）。**不能**写成「没有 HTTP 状态码就重试」——那样会把
  编程错误当成网络抖动反复重发，把 bug 藏进「重试」这个无害的字眼里（真实教训见 §14 v0.21）。
- **HTTP 客户端与 event loop 绑定**：编排层每个阶段一次 `asyncio.run`，`httpx` 的连接池
  属于创建它的那个 loop，跨 loop 复用会抛 `RuntimeError: Event loop is closed`。
  所以「每阶段一套 SDK 客户端，跑完就在同一个 loop 里关掉」。根治要整局共用一个常驻 loop，
  但那与本条「不引入常驻后台线程」的取舍冲突，留待 M5 整体 async 化时一并处理。

---

## 8. 事件流与持久化

### 8.1 存储布局（全 JSON，无二进制数据库）

```
data/
└── games/
    ├── index.json                 # 对局索引（列表，按 game_id 覆盖更新）
    └── {game_id}/
        ├── events.jsonl           # 进行中增量追加（每行一个 JSON 对象，崩溃安全；归档后删除）
        ├── events.json            # 对局结束时归档的完整 JSON 数组（复盘主用，唯一权威版本）
        ├── meta.json              # 配置快照、模型分配、胜负结果
        └── contexts/              # 可选：每个席位的完整消息历史快照
            └── {seat}.json
```

`index.json` 每条只存摘要，不存事件：

```json
{
  "game_id": "20261007-153000-a1b2c3",
  "started_at": "2026-10-07T15:30:00.000Z",
  "seed": null,
  "models": { "1": "deepseek-chat" },
  "winner": "wolf",
  "days": 3,
  "event_count": 214,
  "path": "games/20261007-153000-a1b2c3"
}
```

**为什么同时有 `.jsonl` 和 `.json`**：
单个 JSON 数组文件无法安全地增量追加——写入到一半崩溃会留下语法损坏的文件，整局复盘数据全丢。
因此进行中用 JSON Lines 追加（每行独立，崩溃最多丢最后一行），对局结束时把完整事件数组
一次性归档为 `events.json`。两者都是 JSON，不引入任何二进制数据库。

`.jsonl` 只是**进行中的临时载体**：`EventLog.archive()` 归档出 `events.json` 之后立即删掉它，
否则同一份事件会以两种格式长期各留一份。删除顺序固定为「先写 json，再删 jsonl」——
写 json 失败时 jsonl 仍在，不会两头空。

`index.json` 与 `events.json` 采用**原子写**（写临时文件后 `os.replace`），避免读到半截文件。

### 8.2 Event Schema

```jsonc
{
  "seq": 42,                    // 局内单调递增序号
  "game_id": "20261007-153000-a1b2c3",
  "ts": "2026-10-07T15:31:02.123Z",
  "day": 2,
  "phase": "day_discussion",    // 英文枚举
  "type": "speech",             // 英文枚举，见 §8.3
  "visibility": "public",       // 英文枚举，见 §6.1
  "actor": 5,                   // 座位号 1-9，null 表示系统
  "payload": { "message": "我认为 3 号是狼……" },   // 中文内容
  "thinking": {                             // CoT，中文；恒为观众可见（spectator）
    "text": "3 号第 1 天就死保 7 号，而 7 号……",
    "source": "reasoning_content",          // reasoning_content | content | think_tool | none
    "replayed": true                        // 本条是否按策略回传给了模型
  },
  "llm": {
    "provider": "deepseek",
    "model": "deepseek-chat",
    "latency_ms": 1234,
    "prompt_tokens": 1820,
    "completion_tokens": 96,
    "tool_turns": 2,            // 本回合多轮工具循环轮数
    "network_retries": 0,
    "fallback": false
  },
  "render": {                   // 前端直接用的展示文本（中文）
    "text": "5 号：我认为 3 号是狼……",
    "tone": "accuse"
  }
}
```

**`render.text` 是每个事件的必备字段，不是可选装饰。** 前端与未来的回放 UI 只认它，
不再各自解释 `payload`；缺了它就只能把裸 JSON 摊给用户看（真实发生过：界面上出现
「进入阶段：night_wolf_chat」和赤裸的 `{"target":8}`）。因此 `payload` 里的每一个字段
（`phase` / `target` / `counts` / `tied` / `seat` / `saved` …）都要在 `text` 里有对应的
自然语言表达，且**由产生事件的那一层负责生成**（引擎事件在 `engine/`，agent 侧事件在
`agents/`），不要把翻译责任推给渲染层。`tests/test_full_game.py` 跑 60 局断言「没有任何
事件缺 `render.text`」，新增事件类型若忘了写文案会立刻红。

### 8.3 Event Type 枚举（英文）

```
game_start / role_assigned
phase_enter / phase_exit
speech / wolf_chat / last_words / reflect
vote_cast / vote_abstain / vote_result / vote_tie
seer_check / witch_save / witch_poison / witch_pass
wolf_kill / hunter_shoot / hunter_hold
memory_write / memory_read
death / night_deaths_announced
action_rejected / action_fallback / llm_downgrade   # 非法动作 / 兜底 / CoT 回传被拒降级（均 spectator）
roles_revealed                # 赛后感想前向全体揭示真实身份（public）
game_over / game_aborted
```

### 8.4 必须持久化的一切

每一条 LLM 产出都必须落到事件流，事后可逐条复盘：

| 内容 | 事件 type | 字段 |
| --- | --- | --- |
| 思考（CoT） | 任意 | `thinking.text` + `thinking.source` + `thinking.replayed`（全量，与是否回传无关） |
| 说的话（白天发言 / 狼人夜聊 / 遗言 / 赛后感想） | `speech` / `wolf_chat` / `last_words` / `reflect` | `payload.message` |
| 决策行为（查验 / 用药 / 刀人 / 投票 / 开枪） | 对应 type | `payload` |
| 记录的记忆 | `memory_write` | `payload.key` / `payload.content` |
| 读取记忆 | `memory_read` | `payload.keys` |
| 被拒绝的动作与兜底 | `action_rejected` / `action_fallback` | `payload.reason` |
| 每个席位的完整消息历史 | — | `contexts/{seat}.json`（`persist.dump_agent_contexts: true`） |

### 8.5 兜底策略（Fallback）

当 agent 反复给出非法动作或不响应时：

| 场景 | 兜底行为 |
| --- | --- |
| 白天投票超时 / 非法 | 弃权（记 `fallback: true`） |
| 白天发言失败 | 跳过该玩家本轮发言 |
| 狼人出刀失败 | 该狼弃权；全狼弃权则该夜空刀 |
| 预言家查验失败 | 引擎 RNG 选一名存活玩家 |
| 女巫超时 | 视为弃权 |
| 猎人超时 | 视为放弃开枪 |
| 遗言 / 赛后感想失败 | 跳过，不阻塞流程 |

所有兜底都写入事件流并在前端以特殊标记显示，避免「静默错误」。

---

## 9. 健壮性与随机性

### 9.1 健壮性

- 所有随机性（发牌、兜底选择）走引擎注入的 RNG 实例，**禁止**直接使用全局 `random`。
- LLM 调用统一带 `timeout_s` 和指数退避重试（网络错误，与动作非法重试分开计数）。
- 单局崩溃不影响其他局；Orchestrator 捕获异常后写 `game_aborted` 事件并落盘。
- 前端断线自动重连，重连后按 `seq` 补齐缺失事件。

### 9.2 随机性 / seed

**不强制可复现**。`game.seed` 为**可选**配置项，默认留空（每次随机）。
理由：LLM 输出本身不确定，即使规则侧固定也无法真正复现一局，强制 seed 收益有限。
但 RNG 仍由引擎统一注入，需要调试对照时填一个 seed 即可锁定规则侧行为。

`meta.json` 记录配置快照与结果，用于事后对照：

```json
{
  "game_id": "20261007-153000-a1b2c3",
  "started_at": "2026-10-07T15:30:00.000Z",
  "finished_at": "2026-10-07T15:42:11.000Z",
  "seed": null,
  "engine_version": "0.1.0",
  "prompt_version": "2026-10-07.a",
  "board_hash": "sha256:...",
  "models": { "1": "deepseek-chat", "2": "gpt-4o-mini" },
  "temperature": 0.8,
  "winner": "wolf",
  "final_roles": { "1": "werewolf", "2": "seer" },
  "survivors": [3, 7],
  "days": 3
}
```

---

## 10. 前端设计（React + Vite + TS）

### 10.1 页面

| 页面 | 内容 |
| --- | --- |
| **大厅** | 新建对局：为 9 个席位各选一个模型（支持「全部用同一个」「随机混搭」预设）、选板子、开始。历史对局列表入口。 |
| **对局房间** | 左：9 座位卡片（存活/出局、身份默认隐藏、上帝视角可看）；中：时间轴 + 发言流（按阶段分组，狼人频道用不同底色标注「仅狼人可见」）；右：阶段指示器 + 统计（用时、调用次数、token）。 |
| **回放** | 加载历史对局，时间轴拖动、倍速、单步、切换「上帝视角 / 某玩家视角」。 |

### 10.2 CoT 展示

上帝视角下，每条发言/决策可展开查看对应 agent 的 CoT，视觉上与公开发言明确区分
（折叠面板 + 「仅你可见」标记）。切换为「某玩家视角」时，CoT 与私有频道内容全部隐藏。

### 10.3 观战控制

WebSocket 双向：前端可发送 `pause / resume / step / speed` 指令。
Orchestrator 持有一个可等待的 gate，暂停时阻塞在下一个 LLM 调用之前。

### 10.4 技术选型

- React 19 + TypeScript + Vite + Tailwind CSS
- 状态：zustand（事件流 store，按 seq 追加）
- 通信：原生 WebSocket + 自动重连
- 类型：前端 TS 类型**由后端 Pydantic 模型自动生成，不手写**。链路：
  `engine/events.py` / `server/schemas.py` 的模型 → `model_json_schema()` → JSON Schema →
  `scripts/gen_ts_types.py` 转成 `web/src/api/types.ts`。

  ```bash
  uv run python -m scripts.gen_ts_types            # 生成 / 覆盖（前端 npm run gen:types）
  uv run python -m scripts.gen_ts_types --check    # 只校验不写盘（前端 npm run check:types）
  ```

  `tests/test_ts_types.py` 会在 `uv run pytest` 时断言两者逐字一致——**改了后端模型却忘了
  重新生成会直接测试失败**。这是「两边类型静默漂移」的闸门；也是 event `type` / `visibility`
  等枚举保持英文的直接原因。

  > 为什么自研转换而不用现成工具：只用到 JSON Schema 的一个小子集（object / enum / const /
  > anyOf / array / `$ref` / additionalProperties），零额外依赖、输出风格可控（英文标识符，
  > 中文注释随 `Field(description=...)` 带出）。由此形成一条约定：**接口里出现的每个形状都应
  > 在 `server/schemas.py` 有对应模型**——路由里手写 `dict` 不会进生成结果，也就不会同步到前端。

---

## 11. 目录结构

```
werewolf-arena-llm/
├── DESIGN.md / README.md / AGENTS.md
├── configs/
│   ├── board.example.yaml       # 板子与角色细则（提交）
│   ├── game.example.yaml        # 对局参数（提交）
│   └── models.example.yaml      # provider 与模型（提交）
│   # board.yaml / game.yaml / models.yaml 由用户复制生成，不提交
├── config.py                    # Pydantic 配置 schema + 加载与启动校验
├── engine/                      # 规则引擎（纯确定性，无 I/O）
│   ├── state.py                 # GameState / Player / Phase / NightState
│   ├── actions.py               # Action / ActionType / LegalAction
│   ├── events.py                # Event / Visibility / Thinking / RenderText
│   ├── rules.py                 # 合法性与目标计算、胜负判定
│   ├── engine.py                # RuleEngine: apply(state, action) -> (state, events)
│   ├── visibility.py            # view_for(state, seat)
│   ├── phases.py                # 阶段状态机（setup / resolve）
│   └── prompts/                 # 各角色中文 prompt 模板
├── agents/                      # LLM 运行时
│   ├── client.py                # OpenAI 兼容客户端（AsyncOpenAI，只换 base_url）
│   ├── mock.py                  # MockClient：脚本化响应，无 key 也能开发与测试
│   ├── thinking.py              # CoT 捕获（多源兼容）+ 回传决策
│   ├── tools.py                 # 各阶段 tool schema（动作 + 记忆 + 可选 think）
│   ├── loop.py                  # 多轮 ReAct 工具循环
│   ├── context.py               # AgentContext（每席位独立历史，落盘 contexts/，含滑动窗口压缩）
│   ├── prompts.py               # 中文 system prompt 与阶段指令模板
│   └── policy.py                # LLMPolicy：实现 Policy + decide_all 批量并发
├── orchestrator/                # 唯一同时认识引擎与决策方的层
│   ├── policies.py              # Policy 协议 + RandomPolicy（M2 顶替 LLM）
│   ├── game_loop.py             # 主循环、同时投票调度、兜底
│   ├── render.py                # 终端彩色渲染（中文）
│   └── cli.py                   # python -m orchestrator.cli
├── server/
│   ├── app.py                # FastAPI 应用工厂 + REST（启动 / 回放）+ WebSocket 实时推送
│   ├── ws.py                 # GameManager / GameHandle：后台线程跑局、线程安全队列推送、回放读取
│   ├── schemas.py            # API 契约模型（请求 / 响应 / WS 消息）——前端 TS 类型的生成来源
│   └── __init__.py
├── scripts/
│   └── gen_ts_types.py          # 后端 Pydantic 模型 -> web/src/api/types.ts（--check 供 CI / 测试）
├── storage/                     # 全 JSON 持久化
│   ├── atomic.py                # 临时文件 + os.replace 原子写
│   ├── event_log.py             # JSONL 追加 + 结束归档 events.json
│   ├── meta.py                  # meta.json 内容与板子指纹
│   └── index.py                 # index.json 与历史查询
├── web/                         # React + Vite + TS；src/api/types.ts 是生成物，勿手改
├── data/                        # 运行产物（.gitignore）
├── tests/                       # 引擎单测（规则 + 信息隔离 + CoT 不泄漏）+ 服务端 + 类型同步检查
└── pyproject.toml
```

---

## 12. 分阶段实施计划

| 阶段 | 目标 | 产出 |
| --- | --- | --- |
| **M0 骨架** | 仓库结构、依赖、配置样例、启动校验、Pydantic 模型 | `pyproject.toml`、空模块、`configs/*.example.yaml` |
| **M1 规则引擎** | 9 人局完整状态机，纯确定性，**零 LLM** | `engine/` + 完整单测（含信息隔离测试） |
| **M2 CLI 跑通** | 用规则随机策略（非 LLM）跑完整局，终端彩色输出，**顺带把持久化做掉** | `python -m orchestrator.cli` 能打完一局并写出 `data/games/{game_id}/` |
| **M3 接入 LLM** | AgentRuntime + 多轮工具循环 + CoT 捕获 | 9 个模型打完一局，思考与发言全部落盘 |
| **M4 记忆工具** | `memory_write` / `memory_read` 与上限保护 | **已随 M3 实现**（见 §14 v0.9）：工具挂在 AgentRuntime 的 ReAct 循环里，条目数与字数受 `agent.memory` 上限保护 |
| **M5 服务化** | FastAPI + WebSocket | REST 启动 / 回放 + WS 实时事件流可用（后台线程跑局，独立队列解耦） |
| **M6 前端观战** | 大厅 + 对局房间，实时观战 + CoT 面板 | React 应用可看直播 |
| **M7 回放** | 历史对局列表、回放页、视角切换 | 复盘功能完整 |
| **M8 打磨** | 上下文压缩、异常提示、TS 类型自动生成、性能优化（遗言 / 赛后感想已提前实现） | 上下文压缩（v0.12）、异常提示（v0.13）、TS 类型自动生成（v0.14）已实现；性能优化暂缓 |

**建议**：M1 必须做扎实，它是整个项目的地基；M2 的存在是为了让 LLM 接入前先验证规则正确。

---

## 13. 开放问题（待定）

### 13.1 仍待定

| 编号 | 问题 | 当前默认 |
| --- | --- | --- |
| Q1 | 预言家查验是否告知具体角色 | 只告知阵营（好人 / 狼人），`seer.reveal_faction_only: true` |
| Q2 | 赛后感想是否向已出局者揭示真相 | 是，`postgame.reveal_all_roles: true` |

这两项都已落到配置里，随时可调，不阻塞开发。

### 13.2 已定（不再讨论）

| 问题 | 结论 |
| --- | --- |
| 是否需要战绩统计 | **不做**（已明确排除） |
| 是否支持多局同屏对比观看 | **不做**（已明确排除） |
| 是否要警长 / 警徽竞选 | **不做** |
| 是否要人类入局 | **不做**，9 席全 LLM |
| 夜间死者是否有遗言 | **没有**，仅被票出局者有 |
| 出局是否翻牌 | **不翻牌**，身份仅观众可见 |
| 白天投票形式 | **同时投票，互不看见**，全部提交后一次性公开（`vote.secret_ballot: true`） |
| 已出局玩家的信息流 | **继续旁观全部公开信息**，私有频道永久关闭（§6.5） |
| 存档根目录 | `data/games/{game_id}/`（§8.1） |
| seed / 可复现性 | 可选，不承诺可复现（§9.2） |

---

## 14. 变更记录

| 日期 | 版本 | 变更内容 |
| --- | --- | --- |
| 2026-10-07 | v0.1 | 初始设计。确定定位（观战型娱乐产品）、技术栈（Python 引擎 + React 前端）、LLM 接入（OpenAI 兼容协议）、动作协议（tool call）、规则（9 人 3-3-3、无警长、主流线上细则、屠边、狼人多轮私有协商后投票出刀）、存储（本地 JSON + SQLite）。 |
| 2026-10-07 | v0.2 | 依据用户反馈修订，共 8 项：<br>1. **语言**：文档 / 注释 / LLM 提示词 / user 消息 / UI 文案 / 事件人工可读文本统一简体中文；代码标识符与协议枚举值（event `type`、`visibility`、tool 名、配置 key）保持英文（跨语言映射与 TS 类型生成需要）。<br>2. **配置**：仓库只提交 `*.example.yaml`，真实配置 gitignore；启动时校验，缺失则打印可复制的修复命令并异常退出。<br>3. **记忆工具**：新增 `memory_write` / `memory_read` 两个始终可用的辅助工具，私有（`private:self`），仅存在于上下文与持久化复盘记录中；工具协议改为多轮 ReAct 循环（不再用 `tool_choice=required`），配 `max_tool_turns` 上限。<br>4. **CoT**：观众可见、本人可见、**其他任何 agent 绝对不可见**；`thinking` 恒为 spectator 级并写入事件流；多源捕获（原生 reasoning 字段 → `content` → 可选 `think` 工具），best-effort。<br>5. **持久化**：移除 SQLite，全 JSON。进行中写 `events.jsonl`（崩溃安全），结束时归档 `events.json`；索引 `index.json`；可选 `contexts/{seat}.json` 完整消息快照。明确「思考 / 发言 / 决策 / 记忆 / 感想 / 被拒动作 / 兜底」全部落盘。<br>6. **seed**：改为可选，不承诺可复现；RNG 仍由引擎注入以便调试。<br>7. **规则补充**：新增遗言环节（默认仅被票出局者有）与赛后感想环节（全体 9 人逐一发表，前向全体揭示真实身份）；确认出局不翻牌；白天讨论 2 轮、狼人协商 2 轮。<br>8. **配置风格**：板子配置改为按角色分组的 boolean 风格（`witch.self_save` / `witch.self_save_first_night_only` / `hunter.shot_on_poison` 等），替换原枚举式写法；明确不做战绩统计与多局同屏对比。<br>注：用户示例中的 `hunter.shot_on_position` 应为 `hunter.shot_on_poison`（毒药），已按后者写入。 |
| 2026-10-07 | v0.3 | 明确并细化 CoT 处理，另确认遗言规则：<br>1. **遗言**：确认**夜间死者没有遗言**。仅被票出局者有遗言；`death.last_words_on_night_death` 与 `last_words_on_first_night` 默认 false（配置项保留以便实验）。开放问题中该项已关闭。<br>2. **CoT 拆成三件独立的事**：捕获（capture）/ 回传（replay）/ 记录（persist）。**记录永远无条件全量做**；回传按 provider 协议走。<br>3. **回传策略 `thinking.replay`**：`auto`（默认，原样保留，遇 400 自动剥离重试并降级）/ `required`（必须回传，不降级，用于 Kimi k2.7-code、Qwen qwen3.8-max 等硬性要求）/ `ignored`（不回传但照常记录）/ `forbidden`（必须剥离，用于旧版 deepseek-reasoner 这类回传即 400 的）。<br>4. **协议差异已核实**：DeepSeek 思考模式带 `tools` 时必须回传、不带时被忽略；旧版 `deepseek-reasoner` 回传即 400；Kimi `k2.7-code` 保留式思考强制开启必须回传；Qwen `qwen3.8-max`/`qwen3.8-flash` 的 `preserve_thinking` 默认 true 必须完整回传；OpenAI o 系列不返回推理内容。<br>5. **硬性约束**：回传必须放在 assistant 消息的 `reasoning_content` 字段，**绝不能拼进 `content`**（Qwen 官方明示会掉效果）；只能回传自己的 CoT；回传计入 input token 并计费。<br>6. **新增 `extra_body` 配置**：承载各家非 OpenAI 标准的开关（`thinking.keep` / `preserve_thinking` / `enable_thinking` / `thinking_budget`）。<br>7. **成本控制**：新增 `thinking.replay_window`（默认 null = 全量回传）；持久化始终全量，与回传解耦。<br>8. **事件 schema 调整**：`thinking` 由字符串改为对象 `{text, source, replayed}`；`source` 取值 `reasoning_content` \| `content` \| `think_tool` \| `none`。<br>9. 风险表新增 R8（回传规则冲突）、R9（回传成本与上下文膨胀），原 R8 顺延为 R10。 |
| 2026-10-07 | v0.4 | 参考 OpenCode 与 DeepSeek Harness 重构 CoT 兼容层（§7.1、§7.4）：<br>1. **引入 Provider Adapter 边界**（§7.1）：内部表示归一化，出口按配置序列化；新增设计原则「provider 差异只存在于配置里，代码里不出现 `if provider == "..."`」。<br>2. **配置三层优先级**：`defaults` → provider 级 → model 级，后者覆盖前者、嵌套深合并（同 OpenCode 的 provider → model → variant）。<br>3. **字段名改为可配字符串**：新增 `thinking.field`（`auto` 按 `reasoning_content` → `reasoning` → `reasoning_text` 探测，或显式指定任意 provider 私有字段名）——对应 OpenCode 的 `compatibility.reasoningField`。<br>4. **`replay` 由四值枚举收敛为 boolean**：原 `auto\|required\|ignored\|forbidden` 中 `auto` 与 `required` 实现上无法区分、`ignored` 与 `enabled: false` 语义重叠，改为 `replay: true\|false`。<br>5. **新增启动校验**（§7.4.3）：对齐 DeepSeek Harness 的 `UNSUPPORTED_REASONING_EFFORT`——非法推理配置在**发起任何网络 I/O 之前**失败，不等跑到中途才炸。运行时降级保留为第二层（`replay_on_error: fallback\|fail`）。<br>6. **新增 §7.4.4**：不在代码里按 provider 名字 if-else；唯一例外是按模型名模式匹配做兜底（OpenCode 的 `isKimiFamily()` 同理），且仅当配置未显式指定时生效。<br>7. **新增 §7.4.1 附注**：OpenAI o 系列可用 Responses API 的 `reasoning.encrypted_content` 不透明 blob 回传推理状态（OpenCode 做法），明文拿不到但连贯性可保。<br>8. **新增 §7.4.6**：OpenCode / DeepSeek Harness / 本项目三方做法对照表。顺带印证了两处既有选择——回传必须走独立字段而非拼进 `content`；「模型能看到的一切都记进 append-only 日志」（DSH 的 Trajectory 逐来源回看 ↔ 本项目的 `contexts/{seat}.json`）。 |
| 2026-10-07 | v0.5 | 收口设计阶段剩余未决项，另修两处文档内部矛盾：<br>1. **白天投票形式定为「同时投票、互不看见」**（§4.4）：投票期间谁也看不到别人的选择，全部提交后一次性公开；可并发，单日耗时明显短于串行。`vote.secret_ballot: false` 可切为按座位依次公开投票（必须串行），保留配置以备实验。狼人出刀同为同时投票。<br>2. **新增 §6.5「已出局玩家的信息流」**：定为**出局后继续旁观后续全部公开信息**，私有频道永久关闭（狼人出局后也不再看狼队协商）。这样赛后感想阶段出局者手里有完整公开叙事 + 自己的私有记忆 + 揭示的真实身份，感想才有内容可谈；若「出局即冻结」则感想会明显干瘪。实现上出局只置 `Player.alive = false`，不把 agent 从调度表摘除——出局者被动接收 public 事件、不产出动作（遗言与赛后感想除外）。<br>3. **存档根目录定为 `data/games/{game_id}/`**（§8.1）：gitignore 一条 `data/` 覆盖全部运行产物，未来加 logs/exports 不必再补规则，根目录不被污染。<br>4. **新增 §7.7 并发与限流**：同时投票意味着最坏 9 席并发，故加全局信号量 `agent.max_concurrency`（默认 4）与可选的 provider 级 `max_concurrency`；明确「白天讨论串行、投票/出刀/感想可并发」；429 走 `network_retries` 退避，耗尽走兜底。<br>5. **弃权改为显式合法动作**：新增 `abstain()` 工具（`vote.allow_abstain` 控制是否暴露），用于区分「模型主动弃权」（正常策略）与「模型卡住被兜底」（须打 `fallback: true`）。event type 新增 `vote_abstain`。<br>6. **补合法性约束配置**：`seer.can_check_self` / `seer.can_check_dead` / `werewolf.can_kill_teammate` / `werewolf.can_kill_self`（默认均 false），此前设计未写，引擎实现时会无从判断。<br>7. **修正两处文档内部矛盾**：§6.2 残留引用了 v0.3 已删除的 `thinking.include_own_in_context`（统一指向 `thinking.replay`）；板子配置中 `hunter.shot_on_poison` 与同组 `shoot_on_lynch` / `shoot_on_kill` 命名不一致，统一为 `shoot_on_poison`。<br>8. **§13 开放问题拆为「仍待定」与「已定（不再讨论）」两张表**，把历次已拍板的结论集中归档，避免重复讨论。 |
| 2026-10-07 | v0.6 | M1 规则引擎开工前补齐的规则空白与前两版的措辞歧义：<br>1. **女巫解药语义明确**（§4.5）：解药作用对象**固定为「今夜刀口」**，女巫只决定救或不救，**不能另选救人目标**，故 `witch_save()` 无参数。`self_save` / `self_save_first_night_only` **仅在刀口恰好是女巫本人时生效**；刀口是别人时不受限。今夜空刀则解药无从使用。<br>2. **狼人出刀平票规则明确**（§4.7）：原措辞「平票狼各发言一轮 / 座位号最小的狼决定刀口」含糊且需要额外一轮动作，现明确为——**全体存活狼人**再发言一轮 → 重投（候选项限定为平票目标）→ 二次平票取**平票候选中座位号最小者**（确定性兜底，不追加第三轮）。<br>3. **新增 §4.10 阶段枚举**：列出全部 18 个 Phase 的英文值。明确**猎人夜间死亡在夜间就触发开枪**（`night_hunter_shoot`），结果计入当夜死亡名单随 `day_announce` 公布——与 §4.3 步骤 5 一致。区分「需 LLM 行动的阶段」与「引擎自动结算推进的阶段」（`night_resolve` / `day_announce` / `day_resolve`）。<br>4. **新增 §4.11 投票事件可见性**：为落实「投票期间互不看见」，`vote_cast` / `vote_abstain` 在逐个提交时 **visibility = spectator**（任何 agent 都看不到），计票完成后才发 `vote_result`（白天 public / 狼人出刀 private:wolf）。即 agent 只能看到计完票的结果，看不到谁投了谁。同理夜间 `death` 事件为 spectator，到 `day_announce` 才以 public 的 `night_deaths_announced` 公布名单且不公布死因；白天被票/被枪的 `death` 直接 public。<br>5. **新增 event type `roles_revealed`**（§8.3）：赛后感想前向全体揭示真实身份，public。<br>6. **目录结构调整**（§11）：新增根级 `config.py`（Pydantic schema + 加载与启动校验）、`engine/actions.py`、`engine/events.py`，把状态/动作/事件三类模型拆开，避免 `state.py` 过度膨胀。 |
| 2026-10-07 | v0.7 | M2 编排层与持久化落地，同时把「策略缝」与「同时投票调度」写成显式约定：<br>1. **新增 §5.3 策略缝**：编排层不依赖 LLM，只依赖极窄的 `Policy.decide(state, legal) -> Action`。`legal` 只含单个席位的合法动作；M2 用 `RandomPolicy` 顶替 LLM 把链路跑通，M3 换 `AgentRuntime` 时编排层不改。<br>2. **同时投票的调度约定**（§5.3）：一个阶段内所有待行动席位基于**同一份状态快照**决策，先决策者的选择不进入后决策者上下文，动作收齐后才逐个交引擎结算。「投票期间互不看见」靠这一条保证，而非事后过滤。<br>3. **新增 §5.4 兜底链**：非法动作 → 引擎 `IllegalActionError` → 编排层记 `Rejection` 并改用 `FirstLegalPolicy`（取最小目标）继续；M3 起先在工具循环里带原因回给模型重试，用尽才走兜底。任何兜底必须留痕。<br>4. **持久化从 M3 提前到 M2**（§12）：CLI 跑完一局不落盘就没法复盘，且用户反复要求「一切产出都要落盘」。M3 只需要在事件里补 LLM 与 CoT 字段。<br>5. **§8.1 补 `index.json` 的字段定义**（game_id / started_at / seed / models / winner / days / event_count / path），§9.2 的 `meta.json` 补 `finished_at` 与 `survivors`，并把示例里的 `winner: werewolf` 修正为枚举值 `wolf`。<br>6. **§11 目录结构**补齐 `orchestrator/`（policies / game_loop / render / cli）与 `storage/`（atomic / event_log / meta / index）。<br>7. 新增 `engine.state.ROLE_LABELS`：角色中文名集中放在引擎侧，供终端与后续前端复用，避免各处重复硬编码。 |
| 2026-10-07 | v0.8 | API key 支持两条来源，优先级「配置文件明文 > 环境变量」：<br>1. `ProviderConfig` 新增 `api_key: SecretStr | None`；`api_key_env` 改为可留空（默认 `""`）。<br>2. 新增 `resolve_api_key(provider)`：明文 → 环境变量 → 都没有则抛 `ConfigError`（`required=False` 时返回 None 供启动校验用）。<br>3. `_validate_api_key_env` 改名 `_validate_api_keys`：只在两条来源**都没有**时才报错，且错误信息同时给出 export 与写明文两条修复命令。<br>4. 新增 `mask_key()` 与 `api_key_report()`：启动日志输出脱敏来源（`sk-a***7890`），明文覆盖环境变量时明确标注「（环境变量已设置但被明文覆盖）」。<br>5. **安全约束**：明文用 `SecretStr` 承载，`repr` / `model_dump` 不回显；异常信息永不回显 key；key 绝不写入事件流、`meta.json` 或任何持久化 JSON。<br>6. `load_models(check_env=)` 参数改名 `check_keys=`（此前无外部调用点，改名无成本）。<br>7. §3.2 补 key 来源表与新的报错示例，R10 风险条目改写为「gitignore + SecretStr + 脱敏日志」三道防线。 |
| 2026-10-07 | v0.9 | M3 接入 LLM：Agent 运行时落地，含记忆工具（原 M4 提前）：<br>1. **§5.3.1 策略缝扩展**：新增可选批量缝 `decide_all(state, legal) -> list[Decision]`。`Decision` 携带 `thinking` / `llm` 元信息 / `side_events`（记忆读写、兜底留痕）。编排层优先用它，没有则退回逐个 `decide()`，M2 的同步测试因此不受影响。<br>2. **§5.3.2 并发**：主循环保持同步，LLM 策略在 `decide_all` 内部用一次 `asyncio.run` + `gather` 并发打完本阶段所有席位，受全局与 provider 级信号量约束。M5 服务化时把它改成 `await` 即可，编排层其余不动。<br>3. **新增 `agents/`**：`client`（AsyncOpenAI 薄封装）/ `mock`（脚本化客户端，开发期不碰真 key）/ `thinking`（CoT 捕获与回传）/ `tools`（function schema）/ `loop`（多轮 ReAct）/ `context`（每席位独立历史）/ `prompts`（中文模板）/ `policy`（LLMPolicy）。<br>4. **prompt 模板移到 `agents/prompts.py`**（§7.6）：`engine/` 必须保持零 I/O 纯净性。<br>5. **CoT 三来源已实现**：原生 `reasoning_content`（按 `thinking.field` 配置）→ 正文 `content` → `think()` 工具；回传走**独立字段**决不拼进 `content`；遇 400 按 `replay_on_error` 剥离重试并降级本 agent 的 `replay`；`fail` 策略不重试。<br>6. **事件 schema 补 `llm` 字段**（§8.2）：`LlmMeta` 定义在 `engine/events.py`（纯数据，无 LLM 依赖），记录 provider / model / 延迟 / token / 工具轮数 / 是否兜底。<br>7. **记忆工具随 M3 一起做**（原计划 M4）：`memory_write` / `memory_read`，条目数与字数上限按 `agent.memory` 配置，事件以 `private:self` 落盘。<br>8. **上下文压缩延后**：M3 用增量追加（每次只追加「自上次行动以来的新事件」），`context.max_public_events` 的滑动窗口留到 M8。 |
| 2026-10-07 | v0.10 | M5 服务化（FastAPI + WebSocket），同时并入 M4 文档修正：<br>1. **架构**：React 前端 → FastAPI/WS → Orchestrator → (规则引擎 | Agent 运行时)，引擎与 LLM 互不知晓。<br>2. **关键设计决策**：不把 `GameRunner` 整体 async 化，而是让每局跑在**独立守护线程**里；实时推送**不依赖事件循环**——worker 线程把事件批次放进每个订阅者的普通 `queue.Queue`（线程安全），WebSocket 处理器在自己的事件循环上用 `loop.run_in_executor(None, q.get)` 取批次下发。这样兼容 TestClient（每次请求新 loop）与 uvicorn（单 loop 常驻）。<br>3. **新增 `server/` 包**：`app.py`（路由 + 全开 CORS + `create_app(config_dir, root=None)` 工厂，启动即 `load_board`/`load_game` 校验）、`ws.py`（`GameManager` / `GameHandle`：后台线程跑局、锁保护下 `events.extend` + 写 `event_log` + 遍历 `subscribers` 做 `q.put`、跑完归档 `meta`/`contexts`/`index` 再发 `done`/`error`）、`schemas.py`（`CreateGameRequest` / `CreateGameResponse`）、`__init__.py`。<br>4. **REST 路由**：`POST /games`（构造策略、捕获 `ConfigError`→400、返回 `game_id` + `status`）、`GET /games`、`GET /games/{id}`、`GET /games/{id}/events?seat=`（按 seat 用 `ViewFilter`）。WS：`GET /games/{id}/ws?seat=`（backfill 经 `ViewFilter` 过滤后回灌，再实时推流，终态 `done`/`error` 关闭）。<br>5. **信息隔离在观战流落地**：WS 与 REST 均通过 `ViewFilter` 按 seat 过滤，物理剔除 `thinking`、随 `death` 事件实时关闭私有频道；与 `replay_view` / `view_for` / `filter_events_for` 同源。<br>6. **`engine/visibility.py` 新增 `ViewFilter`**：持有 `seat/role/alive`，`apply(events)` 增量过滤，遇 `death` 且 `actor==seat` 则 `alive=False`，返回的 `thinking` 恒为 None。`replay_view` 改为 `ViewFilter(seat, role).apply(events)`。<br>7. **M4 文档修正并入**：README 路线图补回 M4（已随 M3 实现）；DESIGN §11 里程碑表 M4 行加注「已随 M3 实现（见 §14 v0.9）」、§5.3.2 并发段落改写（明确 M5 实际未整体 async 化，而是后台线程 + 独立队列解耦，引用本版）、§11 目录结构 `server/` 展开为 `app.py / ws.py / schemas.py / __init__.py`。<br>8. **测试**：`tests/test_server.py` 4 个用例（创建并回放 / WS 完整推流 / 座位视角过滤私有频道 / 缺失对局 404），后台线程对局需 `_wait_done` 轮询 `handle.done` 后才读回放；pytest 105 passed、ruff、mypy 全绿。<br>9. **依赖**：`fastapi` 0.142.2 / `uvicorn` 0.54.0 加运行依赖，`httpx` 0.28.1 加测试依赖。 |
| 2026-10-07 | v0.11 | M6 前端观战（React 19 + Vite + TS + Tailwind v4 + zustand），位于 `web/`：<br>1. **技术栈与目录**：Vite + React 19 + TypeScript + Tailwind v4 + zustand（`useGameStore` 按 `seq` 追加事件）。TS 类型手写对齐 `engine/events.py`（DESIGN §10.4 的「Pydantic 生成 TS」留作后续，当前以注释约定保持 `type`/`visibility` 枚举英文一致）。<br>2. **大厅（Lobby）**：三种开局——Mock 客户端（默认，无需 key）/ 随机策略 / 指定单一模型（需配置 key）；可选随机种子；历史对局列表一键进入观战。`API_BASE` 默认 `http://localhost:8000`，可由 `VITE_API_BASE` 覆盖。<br>3. **对局房间（Room）**：左栏 9 座位卡片（存活/出局、`godView` 显身份与模型名）；中栏按 `day:phase` 分组的时间轴事件流（狼人频道等特殊底色 + 可见性徽标，`render.text` 作中文展示文本）；右栏视角切换 + 阶段/统计（调用次数、token、兜底数）。<br>4. **CoT 面板**：上帝视角下每条发言/决策可展开查看对应 agent 思考（「仅你可见」标记，附 `source` / `replayed`）；切换某玩家视角时服务端按 `seat` 过滤，CoT 与私有频道全部不传（已由 `tests/test_server.py::test_websocket_seat_view_filters_private_channels` 验证）。<br>5. **实时连接**：`useGameConnection` 维护 WebSocket，先回灌历史再实时追加；非 `done` 断线自动重连（重连后服务端重发全量历史）；控制消息 `done`/`error` 与事件靠 `seq` 字段区分，前端按 `seq` 去重追加，避免重复或漏事件。<br>6. **质量门**：`tsc --noEmit` + `vite build` 全过（41 模块，~75KB gzip）；REST 契约联调通过（单局 180 事件，含 `role_assigned`/`death`）；WS 行为与 `tests/test_server.py` 一致。M7 回放页（时间轴拖动/倍速）尚未单独做，但已完成「历史列表 + 进入已完成对局 + 视角切换」的等效能力。 |
| 2026-10-07 | v0.12 | M8 上下文压缩落地（`context.max_public_events` 滑动窗口，§7.5）：<br>1. **`agents/context.py` 实现滑动窗口压缩**：累计渲染的公开事件超过 `max_public_events` 时，从**最早回合**开始丢弃该回合 user 消息里的「自上次行动以来：…」事件文本（只丢事件文本、保留局面与指令，不破坏 user/assistant/tool 消息结构）。<br>2. **模板摘要**：被丢弃的行里值得保留的（`DIGEST_TYPES` = 死亡公告 / 投票结果 / 身份揭晓 / 每轮首句）累积成一行「（前情提要：…）」插在 system 之后，最多 `DIGEST_MAX_LINES = 20` 行；纯模板生成，不额外调 LLM。<br>3. **原地压缩**：直接改写 `messages`，故 `contexts/{seat}.json` 快照即「模型实际看到的内容」；`max_public_events <= 0` 视为不压缩。<br>4. **接线**：`LLMPolicy._ensure_contexts` 把 `game.context.max_public_events` 传给 `make_context` → `AgentContext`。<br>5. **测试**：`tests/test_agents.py` 新增 3 例（超窗丢弃最早事件且死亡/首句进摘要、任意回合数下保留行数不超过窗口、非正值时禁用）。全量 **110 passed**，ruff / format / mypy 全绿。 |
| 2026-10-07 | v0.13 | M8 异常提示落地（后端补信号 + 前端四处提示）：<br>1. **补齐后端信号**（此前只落字符串、未成事件）：`agents/loop.py` 在非法动作时发 `action_rejected`（§8.3 早有此类型，此前从未真正发出），在 CoT 回传被拒剥离重试时发 `llm_downgrade`；两者均 spectator，随 `pending_events` 走正常编号落盘。<br>2. **网络重试改为自管**：`agents/client.py` 不再用 SDK 的 `max_retries`（置 0），自己重试并把次数记进 `LLMResponse.network_retries`——可重试状态为连接类错误（无 status）+ 408/409/425/429/5xx，指数退避上限 4s，4xx 不重试；经 `TurnOutcome.network_retries` 写进 `LlmMeta.network_retries`（§7.7）。<br>3. **事件类型**：§8.3 增加 `llm_downgrade`。<br>4. **前端异常提示**（`web/`）：① 顶部 `NoticeBanner` 置顶显示连接错误与「重连中」（`ConnStatus` 新增 `reconnecting`，断线时置位）；② 事件流对 `action_fallback` / `action_rejected` / `llm_downgrade` / `game_aborted` 加特殊底色与徽标；③ 座位卡标出出现过兜底/非法动作的席位；④ 侧栏统计扩展为 6 项（调用 / token / 兜底 / 非法动作 / CoT 降级 / 网络重试），事件行补「重试N」。<br>5. **测试**：`tests/test_agents.py` 新增 4 例（降级事件、重试计数、400 不重试、`LlmMeta.network_retries` 透传），并更新兜底用例断言（现含 4 条 `action_rejected`）。全量 **114 passed**，ruff / format / mypy 全绿；前端 `tsc --noEmit` + `vite build` 通过（42 模块，~76KB gzip）。 |
| 2026-10-07 | v0.14 | 前端 TS 类型改为**自动生成**（此前手写对齐，§10.4）：<br>1. **补齐 API 契约模型**（`server/schemas.py`）：新增 `GameListItem` / `GameMeta`（列表项与 `meta.json`）、`WsDone` / `WsError` / `WsMessage`（`RootModel[Event \| WsDone \| WsError]` 联合）。`ws.py` 的 `list_games` / `meta_for` 改为构造这些模型，`app.py` 的 `/games`、`/games/{id}` 加 `response_model` —— 契约在运行期被真正校验，不再是「只写在文档里的形状」；行内 `import json` 提到模块顶部。<br>2. **新增 `scripts/gen_ts_types.py`**：收集 `engine/events.py`（`Event` / `Visibility` / `Thinking` / `LlmMeta` / `RenderText`）与 `server/schemas.py` 的模型 → `model_json_schema(ref_template="#/$defs/{model}")` → 合并 `$defs` → 转成 `web/src/api/types.ts`。自研转换器只覆盖用到的 schema 子集（object / enum / const / anyOf / array / `$ref` / additionalProperties），零额外依赖；写入用显式 `newline="\n"`，读取做 CRLF 归一化（本机 `core.autocrlf=true` 会把工作区写成 CRLF）。<br>3. **防漂移**：`--check` 只校验不写盘；`tests/test_ts_types.py` 4 例（逐字一致 / `--check` 返回 0 / 关键类型齐全 / 无 `$ref`·`$defs` 漏出）。前端侧加 `npm run gen:types` 与 `npm run check:types`。<br>4. **`engine/events.py`**：三处行内注释（`Thinking.source`、`Event.type`、`Event.actor`）改为 `Field(description=...)`，让说明随 schema 带进生成的 TS（注释不再只存在于 Python 源码里）。<br>5. **`web/src/api/types.ts` 变为生成物**，首行带「请勿手改」横幅；字段形状与原手写版一致（响应/事件字段全部非可选——后端 `model_dump` 总会带上默认值；仅 `CreateGameRequest` 按 `required` 加 `?`），前端代码无需改动即编译通过。<br>6. **质量门**：全量 **118 passed**（+4），ruff / format / mypy 全绿；前端 `tsc --noEmit` + `vite build` 通过（42 模块，75.75KB gzip）。 |
| 2026-10-07 | v0.15 | 首次真实 LLM 联调（本机 LM Studio `qwen/qwen3.5-9b`）后修的观战可读性问题：<br>1. **终端渲染器补齐 agent 侧事件处理器**（`orchestrator/render.py`）：`memory_write` / `memory_read` / `action_fallback` / `action_rejected` / `llm_downgrade` 此前没有 `_on_*`，落入兜底分支 `[{type}] {payload}`，导致**席位信息丢失**（观众看不出记忆是谁写的）。现在统一走 `_seat()` + `_channel()`，例如「（仅本人可见） 1 号 记下「首刀目标」：…」。事件里 `actor` 本来就带着（`agents/loop.py` 构造 `PendingEvent` 时已填 `context.seat`），缺的只是渲染。<br>2. **上帝视角打印内心活动**：`render()` 在打印动作行之前先打印该事件的 `thinking`（`〔内心〕N 号（source）：…`）。**不需要额外视角判断**——玩家视角的 `thinking` 在 `filter_events_for` / `ViewFilter` 里已被物理剔除（恒为 `None`），所以「有就打印」天然只对观众可见。新增 `--no-cot`（不看）与 `--cot-chars`（单条上限，默认 600 字，`0` = 全文）——本地小模型的 CoT 常达数千字且大量重复，不截断会把终端刷爆。<br>3. **修正前端阶段标签错配**（`web/src/lib/derive.ts`）：`PHASE_LABELS` 的 key 与后端 `engine/state.py` 的 `Phase` 枚举大面积不符（`night_wolf`→实为 `night_wolf_chat`/`night_wolf_vote`、`day_discuss`→`day_discussion`、`day_last_words`→`last_words`、`finished`→`game_over`），导致阶段分隔条掉出英文原值；已按后端枚举逐字对齐并补齐 runoff / `hunter_shoot` 等缺失项。新增 `eventText()`，让记忆 / 兜底 / 降级事件在网页也显示可读文本（与终端同一套口径）。<br>4. **守卫测试**：`tests/test_ts_types.py` 新增「`PHASE_LABELS` 必须覆盖全部 `Phase` 值」（解析 `derive.ts` 逐 key 比对，`hunter_shoot` 这类后缀要用行首精确匹配，否则会被 `night_hunter_shoot` 误判为已覆盖）；`tests/test_orchestrator.py` 新增 5 例（记忆事件带席位、异常事件带席位与原因、内心活动展示与排序、`--no-cot` 关闭、超长 CoT 截断与 `cot_chars=0` 不截断）。全量 **124 passed**，ruff / format / mypy 全绿；前端 `tsc --noEmit` + `vite build` 通过（42 模块，75.99KB gzip）。<br>5. **本地模型测试配置**：新增 `configs/models.yaml`（已被 gitignore，不入库）指向 `http://localhost:1234/v1` 的 `qwen/qwen3.5-9b`；单 provider、明文占位 key、`max_concurrency: 1`、`thinking.field: reasoning_content`（实测）、`replay: false`（省 token 且避开入参兼容问题）、`timeout_s: 300`。 |
| 2026-10-07 | v0.16 | 真实对局复盘后修的两处问题（另收紧一处隐患）：<br>1. **板子配置写进 system prompt**（§7.6）：新增 `agents/prompts.py::board_brief(board)`，把身份分布、夜间/白天流程、投票与平票细则、遗言规则、胜负条件从 `BoardConfig` 渲染成「# 本局板子」一节，插在「# 你是谁」之后；同时把原本写死的「一共 9 名玩家」改为按 `board_total(board)` 计算。**真正要解决的是模型幻觉**：日志里 8 号预言家在无警长的板子上自己脑补出「警上发警徽」，根因不是「没声明无警长」，而是「从没告诉过它本局的板子长什么样」——只堵一条（无警长）下次还会冒出别的（自爆、翻牌、警徽流）。故一并显式声明**当前规则下无警长和警徽竞选**，以及**出局不翻牌、不公布死因**：前者在板子 schema 里根本没有字段（§1.2 非目标），后者 `death.reveal_role` 尚未接线，都读不出配置，但都必须告知模型。板子是公开信息，所有席位渲染结果逐字相同。<br>2. **归档后删除 `events.jsonl`**（§8.1）：`EventLog.archive()` 写完 `events.json` 立即 `unlink` jsonl，不再长期保留两份等价数据（实测 203KB + 184KB）。顺序固定「先写 json，再删 jsonl」——写 json 失败时 jsonl 仍在。<br>3. **测试**：`tests/test_agents.py` 新增 3 例（prompt 含板子、板子改了 prompt 跟着改、板子一节对所有席位逐字相同以杜绝夹带私货）；`tests/test_storage.py` 新增 1 例（无 jsonl 时也能归档 / 可重复归档）、`test_append_then_archive_keeps_every_event` 补断言 jsonl 已删；`tests/test_orchestrator.py::test_cli_writes_archive` 断言由「jsonl 存在」改为「jsonl 不存在」。全量 **128 passed**（+4），ruff / format / mypy 全绿。 |
| 2026-10-07 | v0.17 | 复盘时发现的三处**配置未接线**（记录在案，暂不改行为）：`death.last_words_on_night_death` / `death.last_words_on_first_night` / `death.reveal_role` 三个字段在 `configs/board.example.yaml` 与 schema 里都存在，但**引擎从未读取**——当前行为恒为「夜间死者无遗言、出局不翻牌」。为此 `board_brief` 只写引擎实际兑现的规则（写死「只有被投票出局的人有遗言」「不公布身份与死因」），避免 prompt 承诺了引擎做不到的事。后续要么接线、要么从 schema 里删掉，二者必居其一。 |
| 2026-10-07 | v0.18 | 后端根路径加指路牌（`GET /`）：只启动后端、用浏览器打开 `http://127.0.0.1:8000` 会看到裸的 `{"detail":"Not Found"}`——后端只提供 API，界面在前端 5173，这个 404 极易被误读成「服务没起来」。现在根路径返回一张中文 HTML 指路牌（指向 `npm run dev` / 5173、`/docs`、`/games`、`/openapi.json`），`include_in_schema=False` 不污染 OpenAPI；`server.app.main()` 启动时也先打一行「观战界面在前端，请另开终端 cd web && npm run dev」。测试：`tests/test_server.py` 新增 1 例（200 + `text/html` + 含 5173 与 /docs + 不出现在 openapi paths）。全量 **129 passed**（+1）；README 快速开始改为显式的「开两个终端」并说明 8000 只是 API。 |
| 2026-10-07 | v0.19 | 修「读取历史对战」失败的两个叠加原因（一个装不上、一个逻辑缺）：<br>1. **缺 WebSocket 协议实现**：`uvicorn` 不带 WS 实现，观战用的 WS 升级请求被直接打回 `404 Not Found`，日志只留 `Unsupported upgrade request` / `No supported WebSocket library detected`——现象与「对局不存在」一模一样，极易误诊。依赖里补 `websockets>=17.2`（pyproject 内加注释说明「别去掉」）。注意 `TestClient` 走的是 asgi 内部调用、不经过真实 WS 握手，所以 129 个单测全绿也照样漏掉这个坑——只能靠真实 uvicorn 才能发现。<br>2. **归档对局无法回放**：`GameHandle` 只存在于**本进程**开过的对局，服务一重启，`data/games/*` 里的历史对局就没有句柄，`game_ws` 直接回一句 `找不到对局`——而 `/games` 列表里明明列着它。现在 `game_ws` 在无句柄时改走 `events_for()` 读归档、按 `seat` 过滤后一次性放完再发 `done`，对前端而言与「一场已结束的直播」同构，不必为此单开 REST 通道。配套新增 `GameManager.total_seats()`（内存句柄按板子算、归档按发牌事件算）用于**建立连接前**拦掉越界座位号；抽出 `_role_for_seat()` 供 `seat_role` / `events_for` 共用，顺带修掉座位越界时 `next(...)` 抛 `StopIteration` 的老毛病（取不到即退化为平民视角——只少给信息，绝不漏私有频道）。<br>3. **验证**：单测 +3（重启后归档回放且与 REST 同源同量、归档回放按视角过滤、未知对局报 error），全量 **132 passed**；另用真实 uvicorn + 真实 `websockets` 客户端对归档对局 `20261007-123836-e50545` 跑通端到端（上帝视角 97 事件 + `done`；`?seat=5` 55 事件、无 `role_assigned`），这正是单测覆盖不到的那条路径。 |
| 2026-10-07 | v0.20 | 修「网页大厅选『指定模型』就启动失败」——两个原因叠在一起，且**错误信息根本到不了前端**：<br>1. **`build_policy` 依赖 `argparse.Namespace`**：CLI 与服务层共用它，服务层构造 `Namespace(random=…, model=…, seed=…)` 时少传了 `config_dir`，而 `--model` 分支要 `load_models(args.config_dir)` → `AttributeError` → **500**。（mock / random 分支走不到那一行，所以此前 6 个服务层测试全绿也没暴露。）<br>2. **模型名不存在时抛 `KeyError`**：`specs_from_models` 用裸 `KeyError` 表示「models.yaml 里没有这个模型」，同样冒成 500。用户只要抄错一次名字（最常见是漏掉 `qwen/qwen3.5-9b` 的 `qwen/` 前缀）就会撞上。<br>3. **为什么用户只看到一句 `Failed to fetch`**：未捕获异常由 `ServerErrorMiddleware` 在 **CORS 中间件外层**生成 500 响应，该响应**不带 `Access-Control-Allow-Origin`**，浏览器据此直接判定为网络层失败，响应体根本到不了前端——于是「500 + 原因」在界面上退化成「连不上」。**设计约束：所有可预期的失败都必须走 `HTTPException`**（在 CORS 内层被 `ExceptionMiddleware` 处理，响应带 CORS 头），绝不让异常裸奔。<br>4. **修复**：`build_policy` 改为**显式关键字参数**（`game, total_seats, *, model, random, seed, config_dir`），取消 Namespace 鸭子类型——少传/写错字段从此是 mypy 报错而非运行时 500；`specs_from_models` 找不到模型改抛 `ConfigError` 并**列出可用模型**；CLI 的 `load_models` / `build_policy` 两处也补 `except ConfigError`（中文提示 + 退出码 1，不再甩 traceback）；前端 `client.ts::jsonOrThrow` 解析 `{"detail": …}`，提示不再带花括号与引号。<br>5. **测试**：`tests/test_server.py` +3（指定模型能起局、缺 models.yaml → 400、模型名写错 → 400 且列出可用值），`tests/test_agents.py` 的 `KeyError` 断言改为 `ConfigError` 并断言含可用模型清单，全量 **135 passed**。另在**真实 uvicorn**（`TestClient` 不走真实 CORS，覆盖不到本问题）下验证：错模型名 → `400` + `access-control-allow-origin: *` + 中文可用模型列表。 |
| 2026-10-07 | v0.21 | 第二次真实对局（`20261007-133149-af3f3c`）复盘后的两项修复：观战文案，以及**25 次假「网络重试」**。<br>1. **每个事件都必须带 `render.text`**（§8.2 新增约定）：此前只有部分引擎事件有展示文本，其余落到前端的 `JSON.stringify(payload)` 兜底——界面上真实出现「进入阶段：night_wolf_chat」与赤裸的 `{"target":8}`。现在补齐全链路：`phase_enter` 用新增的 `engine.state.PHASE_LABELS` 渲染；`vote_cast`（按动作类型分「N 号选择刀 X 号」/「N 号投给 X 号」）、`vote_abstain`、`vote_result`（日夜双方 × 单人出局 / 无人出局 / 二次平票 / 座位号兜底）、`vote_tie`、`wolf_kill`、`witch_pass` 全部补齐；**agent 侧事件**（`memory_write` / `memory_read` / `action_rejected` / `llm_downgrade` / `action_fallback`）也给 `PendingEvent` 加了 `render` 字段并逐条写文案。前端 `eventText()` 由此从「按 type switch 十几个分支」退化为「取 `render.text`，取不到才兜底」——**翻译责任回到产生事件的那一层**，前端不再各自解释 payload。<br>2. **阶段文案收口为一份**：新增 `engine.state.PHASE_LABELS` + `phase_label()` 作为 UI 文案唯一事实来源；终端渲染器改为 import（删掉本地副本）；`agents/prompts.py` 里同名的表**改名** `PROMPT_PHASE_LABELS` 并注明它面向模型、措辞故意不同（"狼人决定袭击目标" vs "夜晚 · 狼人出刀"）；前端那张表仍在 TS（没法 import Python），守卫测试从「只比 key」升级为 **key 与 value 逐字比对**。<br>3. **修 25 次假重试**（真 bug）：`asyncio.run` 每个阶段换一个 event loop，而 httpx 的连接池绑定在创建它的 loop 上，跨 loop 复用抛 `RuntimeError: Event loop is closed`（`status_code` 为 None）。旧判定是「没状态码就重试」，于是**每个阶段的第一条请求必定假重试一次**——96 次调用报出 25 次重试，正好等于阶段数 26 减一，且每次都是该阶段第一个出场的席位。观战界面把它标成橙色「重试」，看起来像网络差，实为连接池用错了 loop。三层修：① `OpenAICompatClient._sdk()` 按 loop 缓存 SDK 客户端，换 loop 换一套；② `LLMPolicy._decide_batch` 在 `finally` 里 `aclose()` 掉本阶段客户端（留着不关，GC 时会往已关闭的 loop 上安排关闭动作，控制台每次刷一段 `Event loop is closed` 噪音）；③ **重试判定收紧**为「有状态码看 `RETRYABLE_STATUS`；无状态码只认 `APIConnectionError` / `APITimeoutError` / httpx·httpcore·ssl 系异常」，其余立刻抛出——编程错误不能再被重试掩盖（§7.7）。<br>4. **兜底结论（不是设计问题）**：本局 3 次兜底（3/96≈3%）全是「模型未在限定轮数内给出合法动作」，而 `action_rejected` 为 **0**——模型根本没调动作工具（只在正文里说话），不是参数写错，属模型能力问题。顺带确认 `prompt_tokens` 是多轮**累加**值（4 轮 ≈ 7000×4 ≈ 31.5k），不是上下文膨胀。<br>5. **测试与验证**：`test_full_game.py` 新增「60 局内没有任何事件缺 `render.text`」（新增事件类型忘写文案会立刻红）；`test_agents.py` +4（记忆 / 异常事件带 render、编程错误不重试、跨 loop 重建客户端、同 loop 内复用）；`test_ts_types.py` 阶段标签守卫升级。全量 **138 passed**（+3），ruff / format / mypy / TS 类型同步全绿；前端 `vite build` 42 模块 75.86KB gzip。另用**本地假 OpenAI 服务端**做端到端验证：26 个阶段 78 次请求全部一次成功、总重试 0、无 stderr 噪音。 |
| 2026-10-07 | v0.22 | 接着复盘同一局（`20261007-133149-af3f3c`），发现「兜底」里**有一半确实是设计问题**：<br>1. **根因一：阶段指令没点名工具**（`prompts.phase_instruction`）——全表只有「夜间·狼人协商」写了「说出你的建议」而不给工具名（「白天·加时发言」同样），而该阶段的动作工具 `speak` 在 §7.2 的表里本来就是明写的。9B 模型于是老老实实写完一段发言就结束回合（它以为自己已经说过话了）。**本局 3 次兜底中的 1 次（`night_wolf_chat` 席位 5）正落在这一条上**。修复：两条分支都点名 `speak`，并在 docstring 里把「每条分支都要点名工具」写成约束。<br>2. **根因二：只说话的回复被静默丢弃**（`agents/loop.py`）——原分支直接 `context.messages.append({"role":"user", "content":"你必须通过调用工具来行动，请调用一个工具。"})` 然后 `continue`，**没有把模型这次发言写进历史**。后果有两个：① 历史退化成**连续多条 `user`**，本局实测 `5.json` 堆出 8 条连续 user、`1.json` 6 条，那句提示在 `5.json` 里出现 **9 次**；② 模型看不到自己刚说过什么，只能把同一段话原样再说一遍，4 轮全白烧。修复：先 `_record_spoken_reply()` 把发言写进历史（正文为空时不写，否则是服务端会拒收的空 assistant 消息），再 `_ask_for_tool()` 追加**点名本回合可用工具**的纠正提示（如「…本回合可选：speak、memory_write、memory_read。」），且同一条提示不重复堆叠。<br>3. **剩余的兜底才是模型能力问题**：本局另 2 次（`day_discussion` 席位 1 与 5，同一批次 1 ms 内触发）该阶段的指令本来就点了名，属 9B 模型自身不稳定；`action_rejected` 仍为 **0**，即从未出现「调了工具但参数非法」。<br>4. **测试**：`tests/test_agents.py` +3 —— `test_speaking_phases_instruction_names_the_tool`（四个说话类阶段都必须点名 `speak`）、`test_spoken_reply_is_kept_before_the_tool_reminder`（历史里不得出现连续两条 `user`，且每条纠正提示都排在自己那段发言之后）、`test_empty_replies_do_not_stack_the_same_reminder`（空回复时同一句提示只留一条）。全量 **141 passed**（+3），ruff / format / mypy / TS 类型同步全绿。 |
