# 赛博夺权战 (AI-Battle) — Agent 指南

## 项目性质
- AI 操作系统攻防竞技平台，**Windows 独占**（需要 Sandboxie Plus 1.17.5+）
- 中文项目：README、rules/ 下全部设计文档、CONTRIBUTING.md 均为中文

## 环境与命令
- Python 3.13+（`.python-version` 和 `uv.lock` 均要求 >=3.13）
- 包管理用 `uv`（没有 requirements.txt，仅有 `uv.lock`）→ 安装命令：`uv sync`
- 唯一依赖：`PySide6 >=6.11.1`

## 当前项目阶段
**非常早期**——`main.py` 只是 print hello 的 stub，`src/` 下只有 `sandbox.py`（406 行）。
README 中描述的 `python -m ai_battle.manager` 等命令对应的 `ai_battle` 包尚不存在。
agent 不应假设 README 描述的架构已实现。

## 尚无工具链
- 无 linter / formatter 配置
- 无测试框架或测试文件
- 无 CI/CD（无 GitHub Actions）
- 无 `.gitignore`
- 无 `opencode.json`

## 架构（来自 README + rules/）
Manager（沙盒外，AI 不可见）↔ System 沙盒（API）+ AI 客户端（各独立沙盒）

核心源文件：`src/sandbox.py` → `SandboxieController` / `SandboxInstance` / `SandboxPool`

## 设计文档
所有设计文档在 `rules/` 目录下，7 个中文 .md 文件。agent 应优先阅读：
- `赛博夺权战_最终规则文档_v3.0.md` — 游戏规则、积分、API 签名
- `赛博夺权战_启动与运行设计.md` — 启动流程、JSON 配置格式
- 其余 5 个：通信协议 / API 细节 / 解说系统 / UI / 中断注入

## 提交规范（来自 CONTRIBUTING.md）

约定式提交：`<type>(<scope>): <描述>`

| type | 说明 |
|------|------|
| `feat` | 新功能 |
| `fix` | Bug 修复 |
| `docs` | 文档变更 |
| `refactor` | 重构（不改变功能） |
| `perf` | 性能优化 |
| `test` | 测试相关 |
| `chore` | 构建、依赖、配置等杂项 |
| `style` | 代码风格（格式化等） |

scope 限定：`manager|system|judge|commentary|sandbox|client|gui|web|api|rules|docs`

示例：`feat(sandbox): 添加沙盒池多实例并发管理`

## 配置格式
比赛配置为 JSON（非 YAML/TOML），schema 见启动与运行设计文档。
