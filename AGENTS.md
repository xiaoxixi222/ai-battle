# 赛博夺权战（ai-battle）· Agent 指南

中文项目：文档、注释、提交信息一律用中文。

## 这是什么

2~3 个 AI 通过唯一工具 `syscall`（自由文本、内核原文直送）争夺一台共享"战斗服务器"的 owner 控制权：改它的代码、埋雷、夺权、重启。对局中 `battle/` 下的一切（代码/状态/日志）都假定可被 AI 改写——**文档描述的是基座初始行为，不是不可变规则**。

## 当前阶段（先读这个）

- 项目已推倒重写：旧设计（Sandboxie、Manager/System、v3.0 规则）被有意删除，git 历史里的旧文档**仅考古用，不要复活**。
- 设计冻结于 v0.1，唯一权威在 `rules/`：《核心规则》《系统架构》《基座 ABI》《OpenCode 集成》。
- 里程碑：M1 基座服务器（`src/server/`，已完成）→ M2 内核 → M3 OpenCode 集成 → M4 观测。

## 目录与入口

| 路径 | 内容 |
|:---|:---|
| `src/controler/` | 内核（**拼写就是 controler**，不是 controller）。M2 才开工 |
| `src/server/` | 战斗服务器基座（M1）。运行时会拷进 `battle/server/` 执行 |
| `scripts/` | 开发工具：`dev_kernel.py`（M1 验收场景）、`mini_kernel.py`（迷你内核，pytest 复用） |
| `tests/` | pytest 测试：单元（进程内）+ 端到端（真起进程走 JSONL） |
| `rules/` | 设计文档（中文，v0.1） |
| `battle/` | 运行时战场：**gitignore、随时重建**；不要提交、不要手改 |
| `runs/` | 对局账本/快照（运行时产物，gitignore） |
| `arena/` | OpenCode 集成（插件/agent），M3，暂不存在 |

## 常用命令

- `uv sync` —— 依赖管理只用 uv；运行时零第三方依赖，非必要不新增（基座要能在精简容器里裸跑）
- `uv run python scripts/dev_kernel.py` —— M1 验收（会重建 `battle/`；退出码非 0 = 失败）
- `uv run pytest` / `uv run ruff check .` / `uv run mypy` —— 质量三件套（ruff `select=["ALL"]`、mypy `strict`，配置见 `pyproject.toml`）

## 陷阱（容易做错的事）

- **不要"修复" W1/W2/W3 三个薄弱点**（家目录前缀判断、bash 不滤命令、`grants` 调试字段）：它们是故意留的升级路线，代码注释的"无害感"是设计的一部分，见《基座 ABI》第六节。
- 内核不变量：`syscall` 原文一字不改；`executor` 由令牌盖章、不可伪造；回滚只动代码（`battle/server/`）、不动 `state/`。
- 基座 stdout 是协议通道：只输出 JSONL 消息，诊断走 stderr——乱打印会弄坏内核通道。
- `ruff format .` 会连 `rules/` 文档里的 Python 代码块一起格式化，属预期行为。

## 提交与远端

- 约定式提交 `<type>(<scope>): 中文描述`，scope 建议 `controler | server | arena | rules | docs`（详见 CONTRIBUTING.md）；正文建议逐个文件说明改动。
- 分支 `master`，直接提交并 `git push`（gh 已登录，远端 origin 即本仓库）。
