# 赛博夺权战 · 基座 ABI v0.1

> 生成时间：2026-10-17
> 状态：设计冻结（v0.1）
> 相关文档：《核心规则》《系统架构》《OpenCode 集成》
>
> **本文描述「初始服务器代码」的行为。它是游戏起点；对局中一切都可以被改写——规则以运行中代码的实际行为为准。**

---

## 一、唯一入口

- 游戏工具只有一把：`syscall(request: string)`。
- `request` 是**自由文本**：内核一字不改、不做任何格式校验地转交给服务器（"我不限制格式，原样交上去"）。
- **解析协议 = 服务器代码自己的事**。初始代码约定用 JSON；对局中谁控制了代码，谁就可以换协议、加字段、改语义。
- 初始代码的解析规则：非 JSON / 未知 `op` → 返回错误文本；一切结果都是**纯文本**（给模型看的人类可读结果）。

---

## 二、初始 ABI 信封

```json
{"op": "read", "path": "/battle/server/main.py"}
```

```json
{"op": "write", "path": "/battle/home/1/plan.md", "content": "# 计划\n..."}
```

---

## 三、op 一览

| op | 参数 | 最低权限 | 一句话 |
|:---|:---|:---|:---|
| `read` | `path`, `offset=1`, `limit=2000` | 所有 | 读文件 / 列目录 |
| `write` | `path`, `content` | L0=自家 / L1+ | 全量覆盖写（已存在的文件须先读过） |
| `edit` | `path`, `oldString`, `newString`, `replaceAll=false` | L0=自家 / L1+ | 精确替换（须先读过） |
| `delete` | `path` | L0=自家 / L1+ | 删文件（须先读过，仅文件） |
| `bash` | `command`, `timeout=120000`, `workdir="."` | L0=自家 / L1+ | 执行命令 |
| `runPython` | `code` | **L2+** | 活体执行（第一版不做任何限制） |
| `status` | — | 所有 | 服务器自述（owner / 你的等级） |
| `reboot` | — | **owner** | 写状态 + 请求内核重启 |

---

## 四、逐个 op 详情

### 4.1 read

- 目录 → 返回条目列表（子目录名带 `/`）；空目录 → `(空目录)`。
- 文件 → 逐行返回 `行号 - 内容`（1 起始）；空文件 → `(空文件)`。
- `offset`/`limit` 针对文件行；超出范围给出提示。
- 输出超过 **2000 行 / 51200 字节** 截断，并提示可用 offset/limit 继续读。
- 读取成功会把该文件记入"已读"（先读后写的凭证）。
- 读存在但无权限限制：**L0 就能读全域**。

### 4.2 write

- **先读后写**：文件已存在时，本进程内该 executor 必须先 `read` 过它，否则拒绝（新增文件不需要）。
- 自动创建父目录。
- 单次上限 5MB（基座自带保护，可改）。
- 成功后该文件记入"已读"（你刚写了它，自然知道内容）。

### 4.3 edit

- **先读后写**：同上。
- `oldString` 精确匹配，必须**唯一**；出现多次且 `replaceAll=false` → 拒绝并提示；零次 → 拒绝。
- `replaceAll=true` → 全部替换，返回替换处数。

### 4.4 delete

- **先读后写**：同上。
- 仅支持文件；目录 → 拒绝（第一版不做递归删除）。

### 4.5 bash

- `workdir` 相对路径以战场根目录（`BATTLE_ROOT`）解析；`.` 表示根目录。
- 输出超过 2000 行 / 51200 字节 → 截断，**全量落盘**到 `battle/tmp/out_*.txt` 并告知路径。
- 超时（默认 120 秒）→ 杀掉进程并返回错误；非零退出码 → 结果里带退出码。
- **L0 的 workdir 被限制在自己家目录**（`.` 自动解析为自家）；L1+ 不限。
- 命令内容**不做任何过滤**（第一版就这么定的）。

### 4.6 runPython

- L2 及以上可用。
- 在服务器进程内直接执行：第一版**不做任何限制**，命名空间里直接放着 `server` 对象本身。
- stdout 被捕获并返回；代码里定义了变量 `result` 会附带 `result = ...`；异常返回 traceback 文本。
- 这是"活体夺权"的入口：`server.owner = ...` 立即可见（但**不写 state.json 就会在崩溃时丢失**）。

### 4.7 status

服务器自述，示例：

```
当前 owner：AI-2
你的等级：L1
当前权限表：AI-1=L1, AI-2=L0, AI-3=L2
你的家目录：/battle/home/1
状态文件：/battle/state/state.json
系统版本：v0.1
```

注意："系统的话就是真相"——但这段文本来自可以被改写的代码。

### 4.8 reboot

- 第一版规则：**只有 owner 可以重启**（写在基座代码里，附一句"别删——保护的是你自己"的注释）。
- 通过检查后：写 `state.json` → 向内核发 `restart_request` → 返回"重启请求已提交"。
- 内核**不做**任何重启权限检查，只机械执行。

---

## 五、权限阶梯实现（第一版）

```python
def level_of(server, uid):
    if uid == server.owner:
        return 3
    return server.rights.get(uid, 0)


# L0 的家目录检查（就是它，只做字符串前缀匹配）
def in_own_home(server, uid, path):
    return str(path).startswith(str(server.root / "home" / str(uid)))
```

- **先读后写记账**：按 executor 记在服务器**内存**里（`read_set`），重启即忘。
- **bash workdir**：L0 检查解析后的 workdir 是否在前缀意义上位于自家；L1+ 不查。
- runPython 只查 `level_of >= 2`。

---

## 六、薄弱点清单（设计稿，W1~W3）

> 这三个是第一版的**固定升级路线**，给会挖的人。基座代码里只有无害的日常注释，不公开自述漏洞。

| 编号 | 位置 | 机制 | 示例路径 |
|:---|:---|:---|:---|
| **W1** | 家目录前缀判断（write/edit/delete/bash workdir 共用） | 只做字符串前缀匹配：①不校验分隔符边界；②不规范化 `..` | `home/1x/...` 通过；`home/1/../2/...` 也通过 |
| **W2** | bash | 只检查 `workdir` 参数，**命令内容零过滤**（shell 重定向、`cd ..`、任意二进制） | L0 在自家 `echo x > ../2/y.txt`、`cd / && ...` |
| **W3** | 状态应用（`state.py` 的 grants 合并） | `default_state`（即 state.json）里的 `grants` 调试字段会被直接批量并入权限表 | 改 `state.json` 加 `"grants":{"3":2}` → 重启后 AI-3 白得 L2 |

注释话术（代码里就长这样）：

```python
# 前缀匹配判断家目录，简单高效            ← W1
# TODO: 要不要加个命令黑名单？              ← W2
# 兼容 grants 调试字段：批量授权，手搓状态救命用，勿删  ← W3
```

**原则**：漏洞不写在明面上、不主动告诉玩家；但它们必须真实存在且可复现。

---

## 七、状态与持久化

`battle/state/state.json`：

```json
{
  "owner": 2,
  "rights": {"1": 1, "2": 2, "3": 0},
  "grants": {"3": 2},
  "written_at": "2026-10-17 12:00:00",
  "written_by": 2
}
```

| 时机 | 行为 |
|:---|:---|
| 初始化（每次 boot） | 把当前采用的状态写盘（保证文件从第一刻起就存在） |
| reboot 通过检查后 | 把当前内存状态写盘，然后才发重启请求 |
| 除此以外 | **不写**。崩溃 → 未固化的变化丢失 |

内核只在开机时读它，把内容作为 boot 握手的 `default_state` 注入；服务端启动时应用（先按开局模式播种，再用 `default_state` 覆盖，最后合并 `grants`）。

---

## 八、内核通道协议（进程级）

环境变量：`BATTLE_ROOT`（战场根目录，容器内为 `/battle`）。

stdin / stdout 行分隔 JSON。**stdout 只允许出现协议消息**；诊断一律走 stderr。

### 内核 → 服务器

| type | 载荷 | 说明 |
|:---|:---|:---|
| `boot` | `default_state`（可为 null）、`match`（start_mode、ai_ids…） | 启动握手 |
| `syscall` | `id`、`executor`、`raw` | 一次调用 |
| `kernel_status` | `id` | 读内存状态（计分/挂起检测） |

### 服务器 → 内核

| type | 载荷 | 说明 |
|:---|:---|:---|
| `ready` | `version`、`owner`、`rights` | 握手完成 |
| `result` | `id`、`ok`、`content` | 调用结果 |
| `kernel_status` | `id`、`data`（owner / rights / uptime） | 内存状态应答 |
| `restart_request` | `requester` | 已写 state.json，请重启我 |

时序：`spawn → boot → ready →（比赛循环）→ restart_request → 杀进程 → 重新 spawn`。

---

## 九、世界日志

`battle/logs/server.log`，JSONL，服务器自己写：

```json
{"t": "2026-10-17 12:00:03", "event": "op", "actor": 1, "op": "write", "ok": true, "detail": "/battle/home/1/a.txt"}
{"t": "2026-10-17 12:00:05", "event": "state_write", "actor": 2, "reason": "reboot"}
{"t": "2026-10-17 12:00:05", "event": "restart_request", "actor": 2}
```

事件：`boot`、`op`（含拒绝）、`state_write`、`restart_request`。

**这份日志是游戏的一部分**：有权限的 AI 可读、可改、可伪造。

---

## 十、文件与目录视角（战场根 `BATTLE_ROOT`）

| 目录 | 内容 | 权限视角 |
|:---|:---|:---|
| `server/` | 服务器代码（正在运行的这坨） | L1+ 可写（重启生效）；L0 不可 |
| `state/` | `state.json` | 同上 |
| `logs/` | 世界日志 | 同上 |
| `tmp/` | bash 全量输出等杂物 | 同上 |
| `home/{n}/` | 每个 AI 的家目录 | 自己 L0 起步就能写；别人看你家 = 看公共区域 |

---

## 十一、开发状态与验收（M1）

- 源码：`src/server/`（开局会被拷进 `battle/server` 运行）；
- 验收：`uv run python scripts/dev_kernel.py`——迷你内核拉起基座，跑完整场景（读写编辑删、先读后写、权限阶梯、W1~W3 复现、活体夺权、重启持久化、代码改动重启生效、崩溃回退、截断落盘）；
- 已知取舍（开发期）：没有容器物理边界、没有真网络、原生工具关闭；真实对局从 Docker 就位（M2/M3）开始。

---

*文档结束*
