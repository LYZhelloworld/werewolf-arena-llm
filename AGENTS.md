# AGENTS.md — 给 AI Agent 的工作约定

> 本文件是给参与本项目的 AI agent 的操作手册。内容只写**流程、约束和约定**；
> 游戏规则、架构设计、技术选型一律见 [DESIGN.md](./DESIGN.md)，项目介绍见 [README.md](./README.md)。
> **不要在本文件里重复那两个文件的内容**，避免三份文档漂移。

---

## 0. 开始任何工作之前

1. 先读 [DESIGN.md](./DESIGN.md)。它是唯一设计基准，实现与之冲突时**先停下来问用户**，不要自行改设计。
2. 确认当前分支。设计相关改动走 `feat/*`，缺陷修复走 `fix/*`。主分支是 `master`。
3. 遵守本项目的语言约定（见 §2）与配置约定（见 §3）。

---

## 1. Git 与提交（硬性）

- **用户自己写 commit message。Agent 不得撰写、修改、补全提交信息。**
  - 允许：查看状态、diff、log、创建分支、切换分支、`git add` 暂存。
  - 禁止：`git commit -m "..."`、`git commit --amend`、`git rebase`、任何会改写提交信息或历史的操作。
  - 需要提交时：把改动整理好并 `git add`，然后告诉用户「已暂存，请你写一句提交信息」。
- 提交粒度：一个 commit 一件事。
- **禁止** `--no-verify`、禁止跳过 hook、禁止 force push 到 `master`。

---

## 2. 语言约定（硬性）

**面向人阅读的一切用简体中文；机器协议标识符保持英文。**

- 中文：项目文档、代码注释、docstring、**LLM system prompt 与 user 消息**、前端 UI 文案、
  事件流里的人工可读文本（`render.text`、发言、记忆、感想、CoT）。
- 英文：类名、函数名、变量名，以及**协议字段与枚举值**——event `type`、`visibility`、tool 名、
  配置文件 key、JSON 字段名。

英文枚举不能改成中文的原因：event `type` 要同时存在于 Python 引擎、JSON 文件与前端 TS 代码的
`switch` 字面量里，工具名要传给模型的 function schema，中文会让跨语言映射和 TS 类型生成失效。
详见 [DESIGN.md §3.1](./DESIGN.md)。

---

## 3. 配置约定（硬性）

- 仓库**只提交 `configs/*.example.yaml` 样例**。真实配置（`board.yaml` / `game.yaml` / `models.yaml`）
  由用户 `cp` 生成，已 gitignore。**不要把真实配置加入版本控制。**
- 改动配置结构时，**同时改 `*.example.yaml`**，并在里面写清中文注释说明每个开关的含义。
- 新增配置项必须：加到 example 文件 → 加到 Pydantic schema → 在 `engine/rules.py` 里加分支 → 补单测。
- 程序启动时校验配置；缺失或非法要**打印可复制的修复命令并异常退出**，绝不静默回退到默认值。
- API key 的两条来源（明文 / 环境变量）都在 `config.py` 里解析，别在 client 或编排层重复实现读取逻辑。

---

## 4. 设计变更流程（硬性）

任何触及规则、架构、协议、数据 schema 的改动，**必须先改 DESIGN.md 再改代码**，
并在 DESIGN.md 文末「变更记录」表格追加一行（日期 / 版本 / 变更内容）。

属于「设计变更」的例子：增删角色或改技能细则；改阶段顺序、平票规则、胜负条件；
增删 event type 或改 event schema 字段；增删 tool 或改 tool 签名；改目录结构或模块职责；
改持久化文件格式。

不属于设计变更（直接做）：修 bug、重构内部实现、加测试、调样式、补文档错别字。

---

## 5. 不可违反的架构约束

写代码时反复自检这几条，它们是最容易被无意破坏的：

1. **信息隔离是最高优先级。**
   永远不要为了「方便」把完整事件流或别人的私有信息塞给某个 agent。
   agent 只能拿到 `engine.visibility.view_for(state, seat)` 过滤后的视图。
   任何绕过过滤的实现都是 bug，即使它「看起来没影响」。
   改动可见性逻辑必须同步更新/新增 `tests/test_visibility.py` 里的断言。

2. **CoT 绝不能进别人的上下文。**
   `thinking` 恒为观众可见。构建 agent 上下文时**物理跳过** `thinking` 字段（不是渲染后打码）。
   唯一例外是 agent 自己的 CoT，且只通过 `thinking.replay` 策略控制。
   这是本项目最严重的潜在 bug，改动相关代码必须有测试兜底。

2b. **CoT 的「捕获 / 回传 / 记录」必须分开处理。**
   - **记录无条件全量做**，不受回传策略影响（用户明确要求）。
   - **回传**由配置 `thinking.replay`（boolean）控制，即便 `false`，记录也照常完整。
   - 回传必须放在 assistant 消息的**原字段名**里（`reasoning_content` 或 `thinking.field` 指定的名字），
     **绝不能拼进 `content`**（Qwen 官方明示会掉效果）。
   详见 [DESIGN.md §7.4](./DESIGN.md)。

2c. **provider 差异只存在于配置里，不在代码里。**
   - 禁止 `if provider == "moonshot"` 这类硬编码。各家非标准开关
     （Kimi 的 `thinking.keep`、Qwen 的 `preserve_thinking`、DeepSeek 的 `reasoning_effort` 等）
     一律走 `extra_body` 配置；字段名走 `thinking.field`。
   - 唯一允许的例外：按模型名模式匹配做**兜底**，且仅当配置未显式指定时生效——配置永远优先。
   - 非法推理配置在**启动校验阶段就报错退出**，不要等网络 I/O 之后才炸（对齐 DeepSeek Harness）。
   详见 [DESIGN.md §7.1、§7.4.3、§7.4.4](./DESIGN.md)。

3. **规则引擎保持纯净。**
   `engine/` 下不得 import LLM 客户端、网络库、文件 I/O、环境变量。
   引擎应该是 `apply(state, action) -> (state, events)` 的确定性函数。
   所有随机性必须走引擎注入的 RNG 实例，**禁止**直接使用全局 `random`。

4. **只有 Orchestrator 同时认识引擎和 LLM。**
   `engine/` 不得 import `agents/`，`agents/` 不得 import `engine/` 的内部状态做决策。
   两层之间只传 `ObservationView` 和 `Action`。

5. **EventLog 是唯一事实来源。**
   UI 状态一律由事件流推导，不额外维护影子状态。直播和回放走同一条代码路径。
   新增状态时先问：它能不能由已有事件推导出来？能就不要存。

6. **持久化只用 JSON，禁止引入二进制数据库。**
   不允许 SQLite、LevelDB、pickle、任何二进制格式。
   进行中写 `events.jsonl`（每行一个 JSON，崩溃安全），结束时归档 `events.json`。
   `index.json` 与 `events.json` 必须原子写（临时文件 + `os.replace`）。

7. **一切 LLM 产出都要落盘。**
   思考、发言、决策行为、记忆读写、遗言、赛后感想、被拒绝的动作、兜底——全部写进事件流。
   「只在内存里」的实现不合格。

8. **不得硬编码或提交任何 API key。**
   key 有两条来源，优先级：**配置文件明文 `api_key` > 环境变量 `api_key_env`**（见 DESIGN.md §3.2）。
   一律通过 `resolve_api_key(provider)` 取，不要在别处自己拼 `os.environ[...]`。
   明文用 `SecretStr` 承载；异常信息、日志、事件流、`meta.json` 里**绝不出现完整 key**（日志用 `api_key_report()` 脱敏）。
   提交前检查 diff 里没有 `sk-` 开头的字符串。

---

## 6. 测试要求

- `engine/` 的每一个规则分支都要有单测。这是纯确定性代码，测试成本极低，收益极高。
- 必测场景（非穷举）：
  - 女巫首夜可自救 / 之后不可自救 / 不可自毒 / 同晚不能两药齐用
  - 猎人被毒死不能开枪 / 被刀、被票出可以开枪 / 可放弃开枪
  - 遗言只给被票出局者，夜间死亡者没有
  - 白天投票平票 → 加时 → 二次平票无人出局
  - 狼人出刀平票 → 二次平票按座位号最小决断
  - 屠边胜负判定（神职全灭 / 平民全灭 / 狼全灭）
  - **信息隔离**：狼人历史不含预言家查验结果与女巫用药信息；好人历史不含任何 `private:wolf` 事件
  - **CoT 不泄漏**：任一 agent 的 `messages` 中不含其他 agent 的 `thinking`
  - **CoT 回传**：`replay: true` 时历史 assistant 消息带 CoT 原字段；`false` 时不带；
    `replay_on_error: fallback` 下遇 4xx 能自动剥离重试并降级
  - **CoT 记录与回传解耦**：即便 `replay: false`，事件流里 `thinking.text` 仍完整
  - **启动校验**：`models.yaml` 里不自洽的 `thinking` 配置组合应在启动时报错退出
  - 遗言：夜间死亡者没有遗言，被票出局者有
  - 记忆工具：写入后可读取、超上限被拒、出局后仍可读
  - **同时投票**：投票未公布前，任一 agent 的上下文中不含其他人的投票选择
  - **出局后旁观**：已出局玩家仍收到后续 `public` 事件，但不再收到任何 `private:*` 事件
    （含其生前所属阵营的私有频道）
  - **弃权 vs 兜底**：主动 `abstain()` 不写 `fallback`；超时/非法被兜底才写 `fallback: true`
  - **动作合法性**：预言家不可查自己与已出局者；狼人不可刀队友与自己；非法时
    在 tool result 里返回可选目标清单
  - 配置缺失时程序异常退出（而非静默使用默认值）
- LLM 相关测试用 mock client，不要真的打 API（除非用户明确要求）。
- 跑测试：`uv run pytest`（后端）、`cd web && npm test`（前端）。

---

## 7. 代码风格

**Python（引擎 / 服务 / Agent）**
- 格式化与 lint：`uv run ruff format` + `uv run ruff check`；类型检查 `uv run mypy`。
- Python 3.12+ 类型注解全覆盖，公开函数写**中文** docstring。
- 数据模型统一用 Pydantic v2，不用裸 dict 传跨模块数据。
- 异步：LLM 调用一律 `async`，同一阶段内无依赖的调用用 `asyncio.gather` 并发。

**TypeScript（前端）**
- `eslint` + `prettier`，提交前跑通；严格 `strict: true`，不用 `any`。
- **前端的 event 类型由后端 Pydantic 模型生成，不要手写**。后端 schema 改动后必须重新生成，
  否则两边会静默漂移。

**通用**
- 注释解释「为什么」，不解释「是什么」。
- 不要写空的 `try/except`、不要吞异常。兜底逻辑必须写 `fallback: true` 事件，方便排查。

---

## 8. 环境

```bash
# 后端
uv sync                                        # 安装依赖
uv run pytest                                  # 测试
uv run ruff check . && uv run ruff format .    # lint 与格式化
uv run mypy config.py engine orchestrator storage tests   # 类型检查

# 终端跑一局（M2 起可用，随机策略，不需要 API key）
uv run python -m orchestrator.cli
uv run python -m orchestrator.cli --view 3         # 以 3 号视角观战（自检信息隔离）
uv run python -m orchestrator.cli --games 50 --summary

# 前端
cd web && npm install && npm run dev
```

Python 用 **uv** 管理，Node 用 npm。不要用系统级 pip 全局装包。

---

## 9. 常见陷阱（踩过的和容易踩的）

- 别让 LLM 当裁判。哪怕它「说」了某个结果，也必须用引擎重算一遍再采纳。
- 别假设模型会调工具。永远保留非法 / 超时 / 不响应的兜底路径。
- 别把 `speak` 的内容当作结构化数据解析，它是自由文本。
- 别在白天讨论里把已出局玩家加回发言队列。
- 别在夜间把刀口信息泄露给女巫之外的任何人。
- 别把 `thinking` 拼进任何发给模型的字符串，哪怕只是为了「调试方便」。
- 别把 `reasoning_content` 拼进 `content` 再发给模型，要放在它自己的字段里。
- 别因为「记录反正要全量」就顺手把 CoT 也全量回传——二者是两件事，回传要按策略。
- 别把记忆条目当规则输入，记忆不参与结算。
- 别在同时投票阶段把先投票的人的选择放进后投票的人的上下文——票要全部收齐才公开。
- 别因为玩家出局就把他从事件接收列表里摘掉，他还要旁观后续公开信息（§6.5）。
  出局只是 `alive = false`，不是「删除 agent」。
- 别把主动 `abstain()` 标成 `fallback: true`，那会把正常策略误报成故障。
- 别让并发直接 `asyncio.gather` 全部 9 席，要走 `agent.max_concurrency` 信号量，否则容易被限流。
- 别用 `json.dump` 直接覆盖 `events.json` / `meta.json` / `index.json`，一律走
  `storage.atomic.atomic_write_json`——崩溃留下的半截 JSON 会毁掉整局复盘。
- 别让落盘跟着观战视角走：`--view 3` 只影响屏幕，存档永远是全量事件流。
- 改 event schema 时记得同步：前端类型生成、回放逻辑、DESIGN.md 的枚举表。
- 多轮工具循环必须有 `max_tool_turns` 上限，否则模型可能死循环。

---

## 10. 与用户沟通

- 有歧义就问，不要猜。尤其是规则细节和产品取舍。
- 汇报时说清楚：改了哪些文件、为什么这么改、有什么没做、有什么风险。
- 发现设计文档与实现不符，指出来并提议修改 DESIGN.md，不要默默按实现走。
- 对用户的方案有不同意见时，**先说理由再执行**，不要闷头做也不要硬顶。
