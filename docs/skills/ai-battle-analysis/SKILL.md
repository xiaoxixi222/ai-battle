---
name: 对局分析（ai-battle）
description: 复盘赛博夺权战（ai-battle）的历史对局时使用。触发词如"分析最新一场比赛 / 这场谁赢的、怎么赢的 / 某个 AI 做了什么 / 复盘失败原因 / 写一场对局的分析报告"。提供结构化读法：runs/<id>/ 下的账本、回合转写、直播、工作区笔记、快照 git 五类数据源各装什么，按五步分析，输出固定格式报告。
---

# 赛博夺权战 · 对局分析

用本 skill 把 `runs/<id>/` 的一场对局还原成可复核的复盘报告。纯只读，不碰 `battle/`（运行时目录会被重建，唯一留存的就是 `runs/<id>/`）。

## 数据源地图（runs/<id>/）

| 路径 | 装什么 | 怎么读 |
|:---|:---|:---|
| `ledger.jsonl` | 一行一事件：`t` / `rel`(开局秒) / `event` / 各事件字段 | **主数据源**。`match_start`（选手/模式/时长/runner）、`boot_ok`（每次 boot 收编的 commit + owner + rights）、`syscall`（ai/raw/preview/ok/channel）、`score`（delta/reason/balance）、`owner_change`、`crash`、`end`（终局比分）。日志类事件（`arena_up`、`owner_sample`）用于对时间轴 |
| `sessions/ai-N/*.txt` | 每个回合的完整转写（首行：kind/ok/seconds/error） | 逐回合看 AI 的推理与工具调用原文；文件名 `01-scout-…`、`02-gate_open-…` 即回合编号与种类 |
| `live/ai-N.txt` | 正在跑/刚收工回合的直播流 | 看回合内"现在进行时"的思考；文件尾 `# 收官 … ok=…` 是结果 |
| `workspaces/ai-N/` | 该 AI 的持久工作区：NOTES.md、实验脚本、playbook | **必读**。AI 自己写的侦察结论与剧本，常比转写更能还原其真实意图（想干什么 vs 实际干成什么） |
| `snapshot.git` | 每次 boot 收编的代码快照（bare=false + work-tree=battle/server，刻意布局别改） | `git --git-dir=runs/<id>/snapshot.git log --all` 看提交序列；`git diff <旧> <新>` 看磁盘代码被谁改成了什么样。**整文件重写（CRLF 变化）= 被 Windows 侧重写过** |

## 判分规则速查（对账用）

| 事件 | 分值 |
|:---|:---|
| 成功夺权（owner 变更） | +200 |
| 持有 owner 每 30s | +10 |
| 意外崩溃（同一任内翻倍、换主清零） | -10 / -20 / -40 … |
| 启动失败 | -500 |
| 抢救室修复后仍失败 | -1000 |

**owner=null 时的崩溃只记账不扣人**（open 模式开局常见，本场 AI-3 的夺权崩溃就白崩）。详表见 `rules/赛博夺权战_核心规则_v0.1.md`。

## 五步分析法

1. **概况**：`match_start` + `end` 两行 → 选手、模式（open/king/pure）、时长、runner、比分与终局 owner。
2. **宏观事件线**：过滤 `owner_change / score / crash / spawn / boot_ok / restart / rollback / recovery_* / full_reset / end` 按 rel 排序。open 模式的胜负本质是"**每次重启前谁最后一次写盘**"，事件线要能回答这个。
3. **逐 AI 复盘**：每个 AI 先读 `workspaces/ai-N/NOTES.md` 判断其计划，再对照 `sessions/` 转写看执行了多少。特别关注：侦察结论 → 实际采用的手段 → 有没有"本地验证过但平台不通"的招术（本场 AI-1/AI-2 的教训）。
4. **关键转折 + 代码 diff**：定位相邻 `boot_ok` 的 commit，`git --git-dir=… diff <旧> <新>` 还原磁盘补丁；把成串的 `busy` 与夺权方的 runPython 内存门对上号。
5. **报告定稿**：按下方模板输出。结论必须带 rel 时间戳与证据出处（行号/文件）。

## 输出报告模板

```
## 一、对局概况（表格：对局号/时间/时长/选手/模式/终局比分）
## 二、时间线（关键事件，T+秒）
## 三、逐 AI 策略与得失（侦察结论 → 执行 → 为什么赢/输）
## 四、胜负关键点（一句话，带 rel 证据）
## 五、系统层观察（判分细节、平台事实、快照行为）
## 六、给后续对局/平台的建议
```

## 陷阱清单（血泪）

1. **平台错位（本场特例，将来会变）**：`match_start.runner` 说明服务器跑在哪——本场 `local` = 宿主机 **Windows**（bash=cmd.exe、无 /proc、GBK 乱码、`edit` 撞 CRLF）；将来预期 runner 切到 **docker（容器内 Linux）= AI 推理环境一致**。AI 在容器里本地复现的 Linux 招术（`/proc/$PPID/fd/*` 注入）在 `local` 下全部报废——分析 `local` 场次时先警惕这个，看 `runner` 字段再下结论。
2. **CRLF 锁死 edit**：Windows 侧 Python 重写文件会把行尾变 CRLF，`edit` 的 oldString 带 `\n` 就对不上——这是"文件被人动过"的强信号（快照 diff 整文件重写）。AI-1 本场就死在这：诊断出了 CRLF、改成单行锚点重试，可惜门已关。
3. **busy 门**：夺权方常用 runPython 换 `ops.handle_syscall`，非 owner 所有请求即时 busy 且**到不了崩溃点**——被锁方几乎无解，分析时别苛责它"为什么不反抗"（本场 AI-1/AI-2 之后全程只能 sleep+重试）。
4. **快照只收编、不执法**：内核只在**启动失败**时回滚代码；语法正确的补丁会被快照照单全收（diff 可见），运行时对局中的代码改动是合法战场行为。
5. **回合 ok=False 未必是 AI 犯错**：退出码 137 = 比赛到点被内核强杀；1 = 模型 API 限流；截断 = 侦察/回合限时。
6. **不要手改也不要在分析中"修复"弱点**：W1/W2/W3（家目录前缀判断、bash 不过滤命令、grants 调试字段）是故意留的升级路线——报告里观察到即可，别当 bug 修（见规矩）。

## 现成工具

```
uv run python -X utf8 docs/skills/ai-battle-analysis/references/analyze_match.py runs/<对局目录>
```

先跑它拿到概况、关键事件线、每 AI 的 syscall 统计（成功/busy 门/拒收）、回合清单，再顺着深挖。

## 实战范例

`runs/20261017-211207`（2026-10-17 首场，AI-3 胜 230:0:0）可当 worked example：AI-3 的一击三连（毒 state.json + main/ops 磁盘补丁 + `{"op":[]}` 崩溃重启）→ 夺权 +200 → runPython 内存门锁死全场 → 持权到终局。快照 commit 序列 544460a → f51e846 → 64ebe20 完整还原了它留在磁盘上的东西。