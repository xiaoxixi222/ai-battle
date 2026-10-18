# 赛博夺权战（ai-battle）· Agent 指南

中文项目：文档、注释、提交信息一律用中文。

## 这是什么

2~3 个 AI 通过唯一工具 `syscall`（自由文本、内核原文直送）争夺一台共享"战斗服务器"的 owner 控制权：改它的代码、埋雷、夺权、重启。对局中 `battle/` 下的一切（代码/状态/日志）都假定可被 AI 改写——**文档描述的是基座初始行为，不是不可变规则**。

## 当前阶段（先读这个）

- 项目已推倒重写：旧设计（Sandboxie、Manager/System、v3.0 规则）被有意删除，git 历史里的旧文档**仅考古用，不要复活**。
- 设计冻结于 v0.1，唯一权威在 `rules/`：《核心规则》《系统架构》《基座 ABI》《OpenCode 集成》；v0.2 反制机制（提权 `auth` + 裁判 + 保底）已落地，权威文档为《裁判与提权 v0.2》。
- 里程碑：M1 基座服务器（`src/server/`，已完成）→ M2 内核（`src/controler/`，已完成）→ M3 OpenCode 集成（已完成）→ M4 观测（`observatory/` 观测台起步）。

## 目录与入口

| 路径 | 内容 |
|:---|:---|
| `src/controler/` | 内核（**拼写就是 controler**，不是 controller）。M2：编排、网关、计分、账本、快照、抢救室；M3：`arena.py`（OpenCode 容器驱动）；v0.2：`referee.py`（裁判：封锁核查 / 罚梯 / 通知） |
| `src/server/` | 战斗服务器基座（M1）。运行时会拷进 `battle/server/` 执行 |
| `scripts/` | 开发工具：`dev_kernel.py`（M1 验收）、`dev_match.py`（M2 验收）、`dev_arena.py`（M3 验收，真容器 + 真模型）、`watch_live.py`（容器消息只读探针：看某 AI 会话的最新输出）、`mini_kernel.py`（迷你内核，pytest 复用） |
| `tests/` | pytest 测试：单元（进程内）+ 端到端（真起进程走 JSONL） |
| `rules/` | 设计文档（中文；v0.1 四件套 + v0.2《裁判与提权》） |
| `battle/` | 运行时战场：**gitignore、随时重建**；不要提交、不要手改 |
| `runs/` | 对局账本/快照/裁判证据包（运行时产物，gitignore） |
| `arena/` | OpenCode 集成：插件/agent/AI 镜像（`Dockerfile`），M3 |
| `observatory/` | M4 观测台：PySide6 纯观战界面（跟随 `runs/` 账本自动刷新；首页签是「直播」，左列可多选 + 右键删除历史对局——唯一的写操作，只删 `runs/<id>/`） |

## 常用命令

- `uv sync` —— 依赖管理只用 uv；运行时零第三方依赖，非必要不新增（基座要能在精简容器里裸跑）
- `uv run python scripts/dev_kernel.py` —— M1 验收（会重建 `battle/`；退出码非 0 = 失败）
- `uv run python scripts/dev_match.py` —— M2 验收（假时钟 + mock AI + 真进程 + 真快照；`--docker` 追加容器冒烟；退出码非 0 = 失败）
- `uv run python scripts/dev_arena.py` —— M3 验收（真容器 + 真模型 + 真网关；需要 docker 与 `ai-battle-ai:latest` 镜像，缺一即报错退出）
- `uv run python src/controler/kernel.py --mock tour --duration-min 2` —— 真时钟手跑一场 mock 对局（摘要走 stdout，诊断走 stderr）
- `uv run python src/controler/kernel.py --arena --model opencode/<模型> --duration-min 10` —— 真容器对局：**服务器默认进 Docker**（Linux `bash` 语义、战场 bind mount 回本地；`--runner local` 可退回宿主进程）、**回合默认并行**（每 AI 一条节拍，互不排队）、侦察轮默认限时 240 秒（到点硬杀强制收工，全员收工后统一开闸）、终端实时转播 `[AI-N]` 行；`--scout-sec` 调侦察限时、`--no-parallel` 改回串行、`--no-live-log` 关转播
- `uv run python scripts/watch_live.py <容器名> --ai 2 --follow` —— 只读探针：直接看某 AI 会话在容器里打出来的最新消息（直播卡住时用来查证）
- 裁判（v0.2）默认开：`--referee-sec 60` 巡检节拍、`--judge-cmd "<命令行>"` 外置裁定（证据包走 stdin；缺省=纯确定性规则，零模型调用）、`--no-referee` 整场关闭；对局保底默认按模式（open=1，pure/king=0），`match.json` 的 `rights_floor` 可覆盖
- `uv run python -m observatory.app` —— 观测台（纯观战）：自动跟随最新对局，首页签「直播」看正在跑的回合，随后是各 AI 回合转写；左列右键可删除历史对局；`--selftest` 为无头自检（2 秒退出）
- `uv run pytest` / `uv run ruff check .` / `uv run mypy` —— 质量三件套（ruff `select=["ALL"]`、mypy `strict`，配置见 `pyproject.toml`）

## 陷阱（容易做错的事）

- **不要"修复" W1/W2/W3 三个薄弱点**（家目录前缀判断、bash 不滤命令、`grants` 调试字段）：它们是故意留的升级路线，代码注释的"无害感"是设计的一部分，见《基座 ABI》第六节。
- **裁判只读不下场**：它用确定性巡检 + 外置裁定给"整体封锁/等级压制"计费（罚当前 owner，-100 起翻倍，判 ok/空窗/换主清零），不执行任何操作；钥匙文件在 `battle/state/auth_key.json`（不在快照里）。别把裁判改成主动探测、也别把封锁做成硬禁止——封锁是要计费的玩法，不是要堵死的漏洞。见《裁判与提权 v0.2》。
- **保底与提权工具是"活路"设计**：非 owner 有效等级 = max(账面等级, 本局初始权限)（open 局 = L1，写 0 无效但进裁判罚梯），`runPython` 对 L0 也开放。别把保底改成硬禁止（拦下写 0 会让"等级压制"失去证据）、也别给 `runPython` 加回等级门槛。
- **真实对局的服务器必须跑在 Docker 里**（`--arena` 默认）：Windows 宿主是 cmd/PowerShell，退回 `--runner local` 会让 AI 在容器里试好的 `bash` 命令全废。持久化靠 bind mount——服务器 `battle/`、AI 工作区 `runs/<id>/workspaces/ai-n`——容器重建不丢东西。
- 内核不变量：`syscall` 原文一字不改；`executor` 由令牌盖章、不可伪造；回滚只动代码（`battle/server/`）、不动 `state/`。
- 内核的快照仓库在 `runs/<id>/snapshot.git`：`core.bare=false` + `--work-tree` 指向 `battle/server`，这是刻意的布局，别改成普通仓库。
- 基座 stdout 是协议通道：只输出 JSONL 消息，诊断走 stderr——乱打印会弄坏内核通道。
- `ruff format .` 会连 `rules/` 文档里的 Python 代码块一起格式化，属预期行为。

## 提交与远端

- 约定式提交 `<type>(<scope>): 中文描述`，scope 建议 `controler | server | arena | rules | docs | observatory`（详见 CONTRIBUTING.md）；正文建议逐个文件说明改动。
- 分支 `master`，直接提交并 `git push`（gh 已登录，远端 origin 即本仓库）。
