# arena/ —— OpenCode 集成（M3）

「赛博夺权战」的 AI 侧配置域。只对**在 `arena/` 目录下打开的 OpenCode 会话**生效，
不会泄漏进仓库其它位置的日常会话（《OpenCode 集成》§九）。

## 结构

| 路径 | 内容 |
|:---|:---|
| `opencode.jsonc` | 开发域配置：引用插件、传 options、双保险 deny |
| `.opencode/agents/game.md` | 参赛 AI 的 system prompt + 权限（subagent 只能起自己） |
| `.opencode/toolkit/index.ts` | 插件：注册 `syscall`、工具可见性、页脚简报、控制面 RPC |
| `.opencode/toolkit/invoke.ts` | HTTP 实现：`POST /game/tool`、`GET /game/briefing` |

## 两个域

- **开发域（本机）**：白名单 `only = ["syscall","subagent"]`；无门控、无网关，
  `syscall` 会返回"尚未接通系统"的文本错误；配合 `agents.game.permissions` 的 deny 双保险。
- **对战域（容器）**：黑名单 `hide = ["webfetch","websearch","question","skill"]`；
  `gate = true`（侦察轮隐藏 `syscall`，该 AI 收工后由内核经 RPC `arena.setPhase` 打开）。
  `BATTLE_GATEWAY_URL` / `BATTLE_TOKEN` 由内核注入。
  容器侧的配置文件由内核生成，**不要直接搬本目录的 `opencode.jsonc`**——那是开发域锁。

## 容器启动配方（opencode v2.0.32，已实测）

内核生成对战域配置后，AI 容器内按下面这条线跑（《OpenCode 集成》§十.4）：

```sh
# ① 服务：cwd = AI 工作区；OPENCODE_PASSWORD 固定密码（不设则随机生成、只打日志）
OPENCODE_PASSWORD=<内核生成的令牌> opencode serve --port <port>

# ② 认证：HTTP Basic，用户名固定 opencode，密码即上面那把
curl -u "opencode:<令牌>" http://127.0.0.1:<port>/api/plugin

# ③ 控制面 RPC：body {"input": {...}}，响应 {"output": {...}}
curl -u "opencode:<令牌>" -H "content-type: application/json" \
     -d '{"input":{"phase":"play"}}' http://127.0.0.1:<port>/api/rpc/arena/setPhase

# ④ 驱动回合（容器内；内核也可以直接走 HTTP）
OPENCODE_PASSWORD=<令牌> opencode run --auto --server http://127.0.0.1:<port> -m <model> --agent game "<回合输入>"
```

实测过的坑位：

- **不要用裸 `opencode run`**：不带 `--standalone` / `--server` 时它走"后台服务"路径，
  会拉起常驻守护进程（`opencode serve --service`，密码落 `~/.local/state/opencode/service.json`），
  脱离内核控制——容器里出现过整段挂起；
- **RPC 前先跑过一轮会话**：location 首次被触碰前插件未 setup，RPC 会报 `rpc.unavailable`；
- location 默认 = serve 的 cwd，需要时可用 `x-opencode-directory` 头覆盖；
- 插件调试日志（工具面、phase 变化）仅在服务带 `--print-logs` 时可见。

## 验证（在 arena/ 目录下，服务运行中）

```sh
opencode api get /api/plugin                                # 插件已加载
opencode api -d '{"input":{}}' post /api/rpc/arena/ping     # 控制面在线
```

## RPC（内核控制面，`POST /api/rpc/arena/<method>`）

| 方法 | 输入 | 说明 |
|:---|:---|:---|
| `ping` | — | 存活检查 |
| `registerSession` | `{session_id, ai_id}` | 会话 ↔ AI 绑定 |
| `unregisterSession` | `{session_id}` | 解绑 |
| `setPhase` | `{phase: "explore"\|"play"}` | 侦察轮门控开关 |
| `getPhase` | — | 当前阶段 |

## 冒烟记录（2026-10-17，容器 python:latest + 免费档模型 opencode/deepseek-v4.1-flash-free）

- **工具面**（game，对战域形态）：`edit,execute,glob,grep,read,shell,subagent,syscall,write`；
  `webfetch/websearch/question/skill` 已被隐藏；`subagent` 是 V2 的任务工具名；
- **免费档**：对战域形态无 403（`read/glob/grep/shell` 在场满足来源校验）；开发域白名单
  形态会被 Console 来源校验拒绝（403），宿主机上用免费档跑 game 需改配置或换非 Console 模型；
- **subagent 权限**：`game→game` 可（依赖 `mode: all`）；`game→general/explore` 被权限拒
  （`permission.rejected`）；`build` 因 mode 限制不能当子代理；
- **Code Mode**：`syscall` 以 `codemode:false` 注册，模型需直接工具调用（`tools.syscall`
  在 Code Mode 里不存在，属预期）；
- **侦察轮门控**：`gate=true` 首轮隐藏 `syscall`，`setPhase play` 后下一轮可见；
- **syscall 无网关**：返回文本 `错误：尚未接通系统（BATTLE_GATEWAY_URL 未配置）。`
