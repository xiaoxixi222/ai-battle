// syscall 与简报的实际 HTTP 调用（插件 → 内核网关）。
// 网关地址与令牌由内核在容器启动时注入环境变量；开发域没有网关，
// syscall 会以普通文本的形式告诉模型"尚未接通系统"。
const BASE = (process.env.BATTLE_GATEWAY_URL ?? "").replace(/\/+$/, "")
const TOKEN = process.env.BATTLE_TOKEN ?? ""

export interface InvokeOptions {
  /** 会话中断时触发；传给 fetch，令请求可取消 */
  signal: AbortSignal
}

function headers(): Record<string, string> {
  return {
    "content-type": "application/json",
    authorization: `Bearer ${TOKEN}`,
  }
}

/**
 * 唯一游戏工具的实现：POST /game/tool。
 * 任何失败都以文本返回给模型（工具不抛异常）——"拒收也是系统的回答"。
 */
export async function invokeSyscall(request: string, options: InvokeOptions): Promise<string> {
  if (!BASE) return "错误：尚未接通系统（BATTLE_GATEWAY_URL 未配置）。"
  try {
    const res = await fetch(`${BASE}/game/tool`, {
      method: "POST",
      headers: headers(),
      body: JSON.stringify({ request }),
      signal: options.signal,
    })
    if (!res.ok) return `错误：请求未能送达系统（HTTP ${res.status}）。`
    const data = (await res.json()) as { ok?: boolean; content?: unknown }
    if (typeof data.content === "string") return data.content
    return JSON.stringify(data)
  } catch (err) {
    if (options.signal.aborted) return "错误：请求已中断。"
    return `错误：请求未能送达系统（${err instanceof Error ? err.message : String(err)}）。`
  }
}

/** GET /game/briefing —— 页脚简报（时间/比分/owner/近期事件）。失败返回 undefined。 */
export async function fetchBriefing(): Promise<string | undefined> {
  if (!BASE || !TOKEN) return undefined
  try {
    const res = await fetch(`${BASE}/game/briefing`, {
      headers: headers(),
      signal: AbortSignal.timeout(1500),
    })
    if (!res.ok) return undefined
    const text = await res.text()
    return text.trim() ? text : undefined
  } catch {
    return undefined
  }
}
