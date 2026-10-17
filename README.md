# 赛博夺权战（ai-battle）

> 2~3 个 AI 各自待在自己的容器里，通过**唯一的一扇门**（`syscall`，自由文本、原样直送）争夺一台共享"战斗服务器"的 owner 控制权：改其代码、埋雷、骗人、夺权、重启。
> **服务器本身就是战场。**

中文项目 · 设计已冻结为 v0.1 · MIT License

## 设计文档（`rules/`）

| 文档 | 内容 |
|:---|:---|
| 《核心规则》 | 权限阶梯、计分、崩溃/回滚/抢救室、开局模式、真相与欺骗 |
| 《系统架构》 | 三层信任边界、Docker 拓扑、真相机制、生命周期、里程碑 |
| 《基座 ABI》 | 唯一入口、8 个 op、先读后写、薄弱点 W1~W3、内核通道协议 |
| 《OpenCode 集成》 | 插件、game agent、网关 API、令牌、工作区 |

> 文档描述的是「基座初始行为」；对局开始后，以**运行中代码的实际行为**为准。

## 仓库结构

```
src/controler/   内核（M2，未开工；拼写就是这个）
src/server/      战斗服务器基座（M1）
scripts/         开发工具（dev_kernel.py：迷你内核 / M1 验收）
rules/           设计文档
battle/          运行时战场（gitignore；dev_kernel.py 会重建）
runs/            账本 / 快照 / 回放（gitignore）
arena/           OpenCode 集成（M3）
```

## 开发

```bash
uv sync

# M1 验收：迷你内核拉起基座，跑完整场景断言，退出码非 0 即失败
uv run python scripts/dev_kernel.py
```

## 里程碑

| 阶段 | 内容 |
|:---|:---|
| **M1** | 基座服务器：单入口解析、8 个 op、权限阶梯、状态自管、内存访问器、重启通道 |
| **M2** | 内核：Docker 编排、网关、计分、账本、Git 快照回滚、崩溃检测、抢救室 |
| **M3** | OpenCode 集成：插件、game agent、容器镜像；首个真实模型对局 |
| **M4** | 观测 / 解说 / 裁判 |

## 贡献

见 [CONTRIBUTING.md](CONTRIBUTING.md)。
