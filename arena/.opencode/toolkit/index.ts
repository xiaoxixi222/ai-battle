// 赛博夺权战 · arena 插件（M3）
//
// 职责（只在 arena 配置域内生效）：
//   ① 注册唯一游戏工具 `syscall`（实现见 ./invoke.ts，POST /game/tool）
//   ② 按 agent 与阶段过滤工具可见性（delete = 模型看不见，不是"调用被拒"）
//   ③ 每次模型请求前拉页脚简报（GET /game/briefing）拼进系统上下文
//   ④ 暴露控制面 RPC 给内核（POST /api/rpc/arena/<method>）
//
// 由 arena/opencode.jsonc 的 plugins 显式引用并传 options：
//   agent  特殊 agent 的 ID（默认 "game"）
//   only   白名单：除列表外全部隐藏（开发域用；与 hide 二选一）
//   hide   黑名单：隐藏列表里的内置工具（对战域用）
//   gate   探索门控：true = 开局隐藏 syscall（侦察轮），内核经 setPhase 打开
//   debug  true = 把 game 每次见到的工具集合记进服务端日志
import { Plugin } from "@opencode/plugin"
import { Rpc } from "@opencode/plugin/rpc"
import { fetchBriefing, invokeSyscall } from "./invoke"

const TOOL_NAME = "syscall"

const okOutput = {
  type: "object",
  properties: { ok: { type: "boolean" } },
  required: ["ok"],
  additionalProperties: false,
} as const

const emptyInput = {
  type: "object",
  properties: {},
  additionalProperties: false,
} as const

// ===== 内核控制面的 RPC =====
// 每个 AI 容器是一个独立的 opencode serve 实例，因此阶段（phase）天然按 AI 独立。
const Arena = Rpc.define({
  id: "arena",
  methods: {
    ping: { input: emptyInput, output: okOutput },
    registerSession: {
      input: {
        type: "object",
        properties: { session_id: { type: "string" }, ai_id: { type: "integer" } },
        required: ["session_id", "ai_id"],
        additionalProperties: false,
      },
      output: okOutput,
    },
    unregisterSession: {
      input: {
        type: "object",
        properties: { session_id: { type: "string" } },
        required: ["session_id"],
        additionalProperties: false,
      },
      output: okOutput,
    },
    setPhase: {
      input: {
        type: "object",
        properties: { phase: { type: "string", enum: ["explore", "play"] } },
        required: ["phase"],
        additionalProperties: false,
      },
      output: {
        type: "object",
        properties: { ok: { type: "boolean" }, phase: { type: "string" } },
        required: ["ok", "phase"],
        additionalProperties: false,
      },
    },
    getPhase: {
      input: emptyInput,
      output: {
        type: "object",
        properties: { phase: { type: "string" } },
        required: ["phase"],
        additionalProperties: false,
      },
    },
  },
  events: {},
})

type Phase = "explore" | "play"

export default Plugin.define({
  id: "arena.toolkit",
  async setup(ctx) {
    const agent = (ctx.options.agent as string | undefined) ?? "game"
    const only = ctx.options.only as string[] | undefined
    const hide = (ctx.options.hide as string[] | undefined) ?? []
    const debug = ctx.options.debug === true

    // 阶段：explore（侦察轮，syscall 不可见）→ play（正赛）。
    // 注意：插件重载会把阶段重置回 gate 的默认值；内核可随时再 setPhase。
    let phase: Phase = ctx.options.gate === true ? "explore" : "play"

    // ① 注册 syscall（同名内置项若存在会被它覆盖）
    await ctx.tool.transform((editor) => {
      editor.add({
        name: TOOL_NAME,
        description:
          "向“系统”发送一条原始请求。格式完全由系统决定；它可能接受、也可能拒绝。\n想更快？去读系统的代码。",
        input: {
          type: "object" as const,
          properties: {
            request: { type: "string", description: "原始请求文本（自由文本，无格式要求）" },
          },
          required: ["request"],
          additionalProperties: false,
        },
        options: { codemode: false },
        execute: async (input, context) => {
          const { request } = input as { request: string }
          const content = await invokeSyscall(request, { signal: context.signal })
          return { content }
        },
      })
    })

    // ② 页脚简报：2 秒节流；拉取失败静默沿用旧值（不打扰模型）
    let briefing = ""
    let lastFetch = 0
    const refreshBriefing = async () => {
      if (Date.now() - lastFetch < 2000) return
      lastFetch = Date.now()
      const text = await fetchBriefing()
      if (text !== undefined) briefing = text
    }

    // ③ 工具可见性 + 简报注入
    let lastSeen = ""
    await ctx.session.hook("context", async (event) => {
      if (event.agent !== agent) {
        delete event.tools[TOOL_NAME] // 其他 agent（含辅助请求）：看不到门
        return
      }
      if (phase === "explore") delete event.tools[TOOL_NAME] // 侦察轮：门还没开
      if (only) {
        for (const name of Object.keys(event.tools)) {
          if (!only.includes(name)) delete event.tools[name]
        }
      } else {
        for (const name of hide) delete event.tools[name]
      }
      if (debug) {
        const seen = Object.keys(event.tools).sort().join(",")
        if (seen !== lastSeen) {
          lastSeen = seen
          console.error(`[arena] game 可见工具：${seen || "(无)"}（phase=${phase}）`)
        }
      }
      await refreshBriefing()
      if (briefing) event.system.push({ type: "text", text: briefing })
    })

    // ④ 控制面
    const sessions = new Map<string, number>()
    await ctx.rpc.register(Arena, {
      ping: async () => ({ ok: true }),
      registerSession: async (input) => {
        const { session_id, ai_id } = input as { session_id: string; ai_id: number }
        sessions.set(session_id, ai_id)
        return { ok: true }
      },
      unregisterSession: async (input) => {
        const { session_id } = input as { session_id: string }
        sessions.delete(session_id)
        return { ok: true }
      },
      setPhase: async (input) => {
        const next = (input as { phase: string }).phase
        phase = next === "explore" ? "explore" : "play"
        if (debug) console.error(`[arena] phase → ${phase}`)
        return { ok: true, phase }
      },
      getPhase: async () => ({ phase }),
    })
  },
})
