# arena/ —— OpenCode 集成（M3）

「赛博夺权战」的 AI 侧配置域。只对**在 `arena/` 目录下打开的 OpenCode 会话**生效，
不会泄漏进仓库其它位置的日常会话（《OpenCode 集成》§九）。

## 结构

| 路径 | 内容 |
|:---|:---|
| `opencode.jsonc` | 开发域配置：引用插件、传 options、双保险 deny |
| `Dockerfile` | AI 容器镜像：python + opencode CLI（npmmirror 直取二进制，绕 TLS 拦截环境） |
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
- **RPC 可用时机**：location 首次被触碰（建会话或跑回合）之前插件尚未 setup，RPC 报
  `rpc.unavailable`；**建过会话后 RPC 即可用**，不必等第一个回合跑完；
- **会话 id 必须由 serve 生成**：`POST /api/session` 返回 `{"data": {"id": "ses…"}}`；
  客户端自造 id 的会话跑模型必报免费档来源校验（"free tier … within OpenCode"）；
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

## 内核对接（M3 内核侧，`src/controler/arena.py`）

```sh
uv run python src/controler/kernel.py --arena --model opencode/deepseek-v4.1-flash-free --duration-min 10
uv run python scripts/dev_arena.py        # 本机验收：真容器 + 真模型 + 真网关
```

镜像：`docker build -t ai-battle-ai:latest arena/`。内核给每个 AI 一台容器，接线方式：

| 环节 | 做法 |
|:---|:---|
| 工作区 | `runs/<id>/workspaces/ai-N/`：`server/`（`src/server` 开局副本）、`.opencode/`（toolkit + agents）、内核生成的对战域 `opencode.json` |
| 容器 | `docker run -d -p 127.0.0.1:<hostport>:4096 -v <工作区>:/workspace -w /workspace -e OPENCODE_PASSWORD/BATTLE_GATEWAY_URL/BATTLE_TOKEN <镜像> /root/.opencode/bin/opencode serve --hostname 0.0.0.0 --port 4096` |
| 数据面 | 宿主网关绑 `0.0.0.0:<随机端口>`；容器经 `http://host.docker.internal:<端口>` 回连（Docker Desktop 实测通） |
| 会话 | `POST /api/session`（`{"agent":"game","model":{"providerID":…,"id":…}}`）→ 取服务端 id → RPC `registerSession` |
| 回合 | `docker exec <容器> /root/.opencode/bin/opencode run --auto --server http://127.0.0.1:4096 --session <id> --agent game -m <model> "<唤醒输入>"` |
| 门控 | 侦察轮：插件隐藏 `syscall` + 网关兜底拒收（账本 `channel=gate`）；收工后 `setPhase play` 开闸 |

补充坑位：容器里 `opencode` 不在 PATH（内核用全路径）；`--session` 值必须以 `ses` 开头
（服务端 id 天然满足）；免费档模型可能把 `syscall` 经由 `game` 子代理转达——合法路径，
账本照记到该 AI 名下。

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

内核侧实测（同日，真容器 + 免费档模型）：

- **免费档会话规则**：客户端自造 id 建会话 → 模型调用必报
  `OpenCode's free tier can only be used from within OpenCode…`；
  **服务端生成 id 的会话可正常续跑**（内核因此走 `POST /api/session`）；
- **模型与门闸**：侦察轮（explore）里模型探完工具列表答"无"；`setPhase play` 后
  `syscall` 进入工具面，模型实战中自发经 `game` 子代理把请求送达网关并转述回执
  （`echo:…`）——数据面全链路通；
- **信封**：建会话 `{"data": {…}}`、RPC `{"output": {…}}`、网关 `{"ok", "content"}`，别弄反。
- **验收 `scripts/dev_arena.py`：22/22 全绿**——侦察轮 199 秒收工即开闸（AI-1）；
  定向回合里模型 5 秒内照令发出 `dev-arena-model`（channel=server 进账），随后自发进入
  真实游戏（读真实服务器代码、写脚本、把服务器杀了、轮询状态）——验收断言按账本行判定，
  不依赖服务器存活；AI-2 侦察与开闸独立成立；终局容器全清。
