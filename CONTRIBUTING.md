# 贡献指南 (CONTRIBUTING.md)

感谢你对 **赛博夺权战 (ai-battle)** 的关注！这个项目目前由个人维护，但也非常欢迎社区的参与。无论是报告 Bug、提出新功能、改进文档还是直接提交代码，你的帮助都很有价值。

在开始之前，请花几分钟阅读这份贡献指南，它能帮助我们更高效地协作。

---

## 如何贡献？

### 报告 Bug

如果你发现了 Bug，请在 [Issues](https://github.com/xiaoxixi222/ai-battle/issues) 中提交，并尽量包含以下信息：

- **Bug 描述**：发生了什么？你期望发生什么？
- **复现步骤**：怎样可以稳定复现？
- **环境信息**：操作系统、Python 版本、Sandboxie Plus 版本、项目版本（或 commit hash）
- **相关截图或日志**：如果有的话

### 提出新功能

如果你有好的想法，也欢迎提 Issue。请说明：

- 这个功能解决什么问题？
- 你设想的实现方式（非必须，但有帮助）
- 是否愿意自己实现

### 提交代码 (Pull Request)

1. **Fork 本仓库** 到你的 GitHub 账号
2. **创建分支**：`git checkout -b feat/你的功能描述` 或 `fix/你的修复描述`
3. **进行修改**：遵循代码风格和提交规范
4. **提交**：使用约定式提交格式（见下文）
5. **推送**：`git push origin feat/你的功能描述`
6. **发起 Pull Request**：在 GitHub 上发起 PR，描述你的修改内容

---

## 开发环境

```bash
# 克隆仓库
git clone https://github.com/xiaoxixi222/ai-battle.git
cd ai-battle

# 创建虚拟环境并安装依赖（推荐 uv）
uv sync

# 或使用 pip
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

项目运行在 Windows 上，依赖 Sandboxie Plus，请确保已安装 [Sandboxie Plus 1.17.5+](https://sandboxie-plus.com/downloads/)。

---

## 提交信息规范

本项目采用 **约定式提交 (Conventional Commits)** 格式。

```
<type>(<scope>): <简短描述>

[可选的详细说明]
```

### type（类型）

| 类型 | 说明 |
|:---|:---|
| `feat` | 新功能 |
| `fix` | Bug 修复 |
| `docs` | 文档变更 |
| `refactor` | 重构（不改变功能） |
| `perf` | 性能优化 |
| `test` | 测试相关 |
| `chore` | 构建、依赖、配置等杂项 |
| `style` | 代码风格（格式化等） |

### scope（影响范围）

常见模块：

| 范围 | 说明 |
|:---|:---|
| `manager` | Manager 核心 |
| `system` | System 沙盒 API 实现 |
| `judge` | 裁判引擎 |
| `commentary` | 解说 AI |
| `sandbox` | 沙盒控制模块 |
| `client` | AI 客户端 SDK |
| `gui` | 控制面板 GUI |
| `web` | Web 观战面板 |
| `api` | 通信协议 |
| `rules` | 游戏规则文档 |
| `docs` | 项目文档 |

### 示例

```
feat(sandbox): 添加沙盒池多实例并发管理
fix(sandbox): 修复管道死锁导致 AI 输出卡死
docs: 更新 README 安装步骤
```

详细说明与示例请参考 [提交格式文档](rules/提交信息格式.md)（如果有的话）或直接看已有提交历史。

---

## 代码风格

- **Python 代码**：遵循 [PEP 8](https://peps.python.org/pep-0008/) 风格，使用 4 空格缩进
- **命名**：类名使用大驼峰 `SandboxController`，函数和变量使用小写下划线 `start_process`
- **注释**：重要的逻辑和复杂部分请添加注释，API 类模块应包含文档字符串
- **文件编码**：使用 UTF-8
- **导入顺序**：标准库 → 第三方库 → 项目内部模块，每组之间空一行

目前项目未强制使用 linter，但建议你在提交前使用 `ruff` 或 `flake8` 检查。

---

## 行为准则

请保持友善、包容。任何形式的骚扰、侮辱性言论或歧视性行为都不会被容忍。

尊重他人的时间和劳动，讨论时专注于技术本身。

---

## 许可证

本项目采用 **MIT License**。你提交的任何代码都将被视为在该许可下发布。请确保你有权授予此许可。

---

*如果你有任何疑问，欢迎在 Issue 中提出。*
