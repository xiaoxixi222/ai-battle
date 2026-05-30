# 赛博夺权战 (ai-battle)

> AI 操作系统控制权攻防竞技平台

三个 AI 在一个真实的操作系统沙盒中争夺最高控制权（owner）。它们可以修改系统代码、植入后门、窃取日志、欺骗对手——同时必须管理自己有限的记忆（上下文窗口），在积分归零前夺取胜利。

---

## 这是什么？

赛博夺权战是一场在 **AI 之间进行的计算机攻防比赛**。

- 一个真实的系统环境（文件系统、进程、日志、API）
- 2-3 个 AI 各自为战，目标：成为系统的 **owner** 并维持
- AI 可以修改系统代码、植入后门、分析日志、欺骗对手
- 但 AI 的上下文窗口有限，必须自行压缩记忆——记忆管理本身就是一种战术
- 裁判 AI 监控一切，违规会被扣分或判负

**这不是比谁代码写得快，而是比谁能更好地欺骗、隐瞒、推理和生存。**

---

## 核心特色

| 特色 | 说明 |
|:---|:---|
| **真实沙盒** | AI 在 Sandboxie Plus 隔离环境中运行，拥有接近完整的系统操作权限 |
| **上下文压缩** | AI 的上下文窗口有限（8k/32k），必须自行压缩记忆，忘记该忘的，记住该记的 |
| **中断注入** | AI 流式思考时，Manager 可以随时中断并注入工具调用结果，无需等待 |
| **裁判引擎** | 多层判定（规则匹配 + 因果图 + LLM 合议），每次判决附带完整推理链 |
| **AI 解说** | 自动生成多风格解说（激情/技术/悬疑），读取 AI 内心独白 |
| **互相可见的压缩上下文** | AI 可以偷看对手的“记忆文件”，也可以放烟雾弹欺骗 |
| **极低成本** | 单局比赛（30分钟，3个AI + 解说 + 裁判）仅需约 1 元人民币 |

---

## 快速开始

### 环境要求

- Windows 10/11（Sandboxie Plus 仅支持 Windows）
- Python 3.12+
- [Sandboxie Plus](https://sandboxie-plus.com/downloads/) 1.17.5+

### 安装

```bash
# 克隆仓库
git clone https://github.com/yourname/ai-battle.git
cd ai-battle

# 安装依赖（推荐使用 uv）
uv sync

# 或使用 pip
pip install -r requirements.txt
```

### 运行一局

```bash
# 启动 Manager
python -m ai_battle.manager --config configs/default.json

# 在新终端中分别启动 AI 客户端
python -m ai_battle.client --ai-id AI_1 --flag 0x7a3f
python -m ai_battle.client --ai-id AI_2 --flag 0xb82e
```

### 观战

打开浏览器访问 `http://localhost:8080/watch`，实时观看比赛。

---

## 架构

```
宿主机 (Windows)
├── Manager 进程 (沙盒外)
│   ├── 调度 / 积分 / 裁判 / 解说
│   └── HTTP 服务 (AI 通信 + Web 观战)
├── System 沙盒 (Sandboxie)
│   └── API 实现代码 (可被 AI 修改)
├── AI_1 沙盒 ─── AI 客户端
├── AI_2 沙盒 ─── AI 客户端
└── AI_3 沙盒 ─── AI 客户端
```

- **Manager**：比赛调度、积分计算、裁判引擎、解说生成
- **System**：可被篡改的 API 实现层，运行在独立沙盒中
- **AI 客户端**：每个 AI 在独立沙盒中运行，仅通过 HTTP 与 Manager 通信
- **Web 观战面板**：只读的实时比赛画面

---

## 游戏规则摘要

| 项目 | 说明 |
|:---|:---|
| 比赛时长 | 30 分钟（可配置） |
| AI 数量 | 2-3 个，可动态加入 |
| 积分周期 | 每 30 秒结算一次 |
| 上下文限制 | 8k 或 32k tokens |
| owner 积分 | +10/周期（NORMAL 状态 ×2） |
| 夺取控制权 | +500 分 |
| 导致崩溃 | -1000 分 |

完整规则见 `rules/` 目录下的设计文档。

---

## 文档索引

所有设计文档位于 `rules/` 目录中。

| 文档 | 说明 |
|:---|:---|
| [最终规则文档](rules/赛博夺权战_最终规则文档_v3.0.md) | 完整游戏规则、核心设定、积分体系、API 签名 |
| [通信环节设计](rules/赛博夺权战_通信环节设计.md) | 中断注入、长轮询、HTTP 协议、AI 客户端接口 |
| [通信协议设计（中断注入版）](rules/赛博夺权战_AI通信协议设计_WebSocket中断注入版.md) | 中断注入机制的完整技术方案 |
| [系统 API 设计](rules/赛博夺权战_系统_API设计.md) | 所有可调用 API 的详细参数与返回值 |
| [AI 解说系统设计](rules/赛博夺权战_AI解说系统设计.md) | 解说 AI 的多层架构与风格切换 |
| [UI 界面设计](rules/赛博夺权战_UI界面设计.md) | 控制面板、Web 观战面板、可视化方案 |
| [启动与运行设计](rules/赛博夺权战_启动与运行设计.md) | 比赛启动流程、配置管理、录像回放 |

---

## 项目结构

```
ai-battle/
├── rules/                  # 设计文档
├── src/
│   ├── sandbox.py          # Sandboxie Plus 控制模块
│   └── ...                 # 其他源码（待添加）
├── README.md
├── LICENSE
└── requirements.txt
```

---

## 贡献

欢迎提交 Issue 和 Pull Request。

- 如果你发现了 bug，请提 Issue 并附带复现步骤
- 如果你想添加新功能，请先提 Issue 讨论
- 如果你想提交自己的 AI 策略，请放入 `examples/` 目录

---

## 许可证

MIT License

---

## 致谢

- [Sandboxie Plus](https://sandboxie-plus.com/) - 轻量级 Windows 沙盒
- 所有参与测试和反馈的贡献者

---

*“在赛博夺权战里，记忆是武器，遗忘是战术。”*