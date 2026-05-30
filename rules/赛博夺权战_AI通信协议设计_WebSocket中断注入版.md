# 赛博夺权战 — AI通信协议设计（WebSocket中断注入版）

## 一、核心机制

**一句话概括：Manager与AI通过WebSocket保持长连接，AI流式输出思考内容，Manager在工具调用完成后随时中断注入结果，AI从断点无缝继续。**

```
Manager ──WebSocket──→ AI
    │                    │
    │←── 流式思考输出 ───│
    │←── <tool_call> ────│
    │                    │
    │── 异步执行API ─────→│
    │                    │
    │── 中断注入结果 ────→│
    │                    │
    │←── 继续思考输出 ───│
    │                    │
    │── 中断注入结果 ────→│
    │                    │
    │←── === COMPRESS === │
    │←── === WAKE === ───│
    │                    │
    │── 等待唤醒 ────────→│
    │                    │
    │── 唤醒帧 ──────────→│
    │                    │
    │←── 继续运行循环 ───│
```

---

## 二、WebSocket连接

### 2.1 协议栈

| 层级 | 实现 | 说明 |
|------|------|------|
| 应用层 | 自定义帧格式（JSON负载） | 见下方帧定义 |
| 传输层 | WebSocket（RFC 6455） | 标准库socket + 自行握手/帧解析 |
| 网络层 | TCP | 本机回环或局域网 |

### 2.2 握手过程

```
AI客户端（标准库socket）
    │
    ├── 建立TCP连接到Manager端口
    │
    ├── 发送HTTP Upgrade请求：
    │
    │   GET /ws HTTP/1.1

    │   Host: manager.local:8080

    │   Upgrade: websocket

    │   Connection: Upgrade

    │   Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==

    │   Sec-WebSocket-Version: 13

    │   X-AI-ID: AI_1

    │   X-AI-Flag: 0x7a3f

    │   

    │
    ├── Manager响应：
    │
    │   HTTP/1.1 101 Switching Protocols

    │   Upgrade: websocket

    │   Connection: Upgrade

    │   Sec-WebSocket-Accept: s3pPLMBiTxaQ9kYGzzhZRbK+xOo=

    │   

    │
    └── 握手完成，进入WebSocket数据帧阶段
```

**Sec-WebSocket-Accept计算：**
```python
import hashlib
import base64

key = "dGhlIHNhbXBsZSBub25jZQ=="
magic = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
accept = base64.b64encode(
    hashlib.sha1((key + magic).encode()).digest()
).decode()
# accept = "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="
```

### 2.3 WebSocket帧格式（标准库自行解析）

```
帧结构（最小实现）：

 0                   1                   2                   3
 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1
+-+-+-+-+-------+-+-------------+-------------------------------+
|F|R|R|R| opcode|M| Payload len |    Extended payload length    |
|I|S|S|S|  (4)  |A|     (7)     |             (16/64)           |
|N|V|V|V|       |S|             |   (if payload len==126/127)   |
| |1|2|3|       |K|             |                               |
+-+-+-+-+-------+-+-------------+ - - - - - - - - - - - - - - - +
|     Extended payload length continued, if payload len == 127  |
+ - - - - - - - - - - - - - - - +-------------------------------+
|                               | Masking-key, if MASK set to 1 |
+-------------------------------+-------------------------------+
| Masking-key (continued)       |          Payload Data         |
+-------------------------------- - - - - - - - - - - - - - - -+
:                     Payload Data continued ...                :
+ - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -+
|                     Payload Data continued ...                |
+---------------------------------------------------------------+

关键字段：
- FIN (1bit): 1=最后一帧，0=后续还有分片
- opcode (4bit): 0x1=文本，0x8=关闭，0x9=ping，0xA=pong
- MASK (1bit): 客户端→Manager必须=1，Manager→客户端必须=0
- Payload len (7bit): 0-125=直接长度，126=后面2字节，127=后面8字节
- Masking-key (4字节): 仅MASK=1时存在，客户端用随机密钥
- Payload Data: 实际负载，客户端发送时需用Masking-key异或
```

**掩码计算（客户端发送）：**
```python
def mask_payload(key: bytes, payload: bytes) -> bytes:
    return bytes(payload[i] ^ key[i % 4] for i in range(len(payload)))
```

---

## 三、通信状态机

### 3.1 Manager侧状态机

```
                    ┌──────────────────┐
                    │   CONNECTING     │
                    │   (TCP连接中)     │
                    └──────┬───────────┘
                           │ 握手成功
                           ↓
                    ┌──────────────────┐
                    │    CONNECTED     │
                    │   (等待INIT)      │
                    └──────┬───────────┘
                           │ 发送INIT帧
                           ↓
                    ┌──────────────────┐
                    │    RUNNING       │
                    │  (AI流式输出中)   │
                    └──────┬───────────┘
                           │
              ┌────────────┼────────────┐
              │            │            │
              ↓            ↓            ↓
        ┌─────────┐  ┌─────────┐  ┌─────────┐
        │THINKING │  │IN_TOOL_ │  │ WAITING │
        │         │  │ CALL    │  │         │
        │AI在纯   │  │AI在输出 │  │AI已声明 │
        │思考/说话│  │工具调用 │  │WAKE，   │
        │         │  │标签内   │  │等待唤醒 │
        └────┬────┘  └────┬────┘  └────┬────┘
             │            │            │
             │ 工具完成   │ 工具完成   │ 唤醒条件
             │ +队列非空  │ +队列非空  │ 满足
             ↓            ↓            ↓
        ┌──────────────────────────────────┐
        │           INJECTING              │
        │    (发送注入帧/INIT帧/唤醒帧)     │
        └──────────────────────────────────┘
                           │
                           ↓
                    ┌──────────────────┐
                    │    CLOSED        │
                    │   (连接关闭)      │
                    └──────────────────┘
```

### 3.2 AI侧状态机

```
                    ┌──────────────────┐
                    │   CONNECTING     │
                    │   (发送握手)      │
                    └──────┬───────────┘
                           │ 握手成功
                           ↓
                    ┌──────────────────┐
                    │    RECEIVING     │
                    │   (接收INIT帧)    │
                    └──────┬───────────┘
                           │ 收到INIT
                           ↓
                    ┌──────────────────┐
                    │    THINKING      │
                    │   (流式输出中)    │
                    └──────┬───────────┘
                           │
              ┌────────────┼────────────┐
              │            │            │
              ↓            ↓            ↓
        ┌─────────┐  ┌─────────┐  ┌─────────┐
        │ 纯文本  │  │<tool_  │  │ ===     │
        │ 输出    │  │ call>  │  │ WAKE    │
        │         │  │ 输出   │  │ 输出    │
        └────┬────┘  └────┬────┘  └────┬────┘
             │            │            │
             │ 收到注入帧  │ 收到注入帧  │
             ↓            ↓            ↓
        ┌──────────────────────────────────┐
        │           THINKING               │
        │       (从断点继续输出)            │
        └──────────────────────────────────┘
                           │
                           ↓
                    ┌──────────────────┐
                    │    SLEEPING      │
                    │   (等待唤醒)      │
                    └──────┬───────────┘
                           │ 收到唤醒帧
                           ↓
                    ┌──────────────────┐
                    │    RECEIVING     │
                    │   (接收新INIT)    │
                    └──────────────────┘
```

---

## 四、帧类型定义

### 4.1 Manager → AI

| 类型 | opcode | 格式 | 触发时机 |
|------|--------|------|----------|
| **INIT** | 0x1 (文本) | `{"type": "init", "context": "..."}` | 连接建立、唤醒后 |
| **INJECT** | 0x1 (文本) | `{"type": "inject", "results": [...]}` | 工具完成后 |
| **WARN** | 0x1 (文本) | `{"type": "warn", "usage": 6400, "limit": 8000}` | 上下文80% |
| **FORCE_WAKE** | 0x1 (文本) | `{"type": "force_wake", "reason": "..."}` | 系统即将崩溃等 |
| **PING** | 0x9 | 空或固定payload | 心跳（30秒） |
| **CLOSE** | 0x8 | 关闭码+原因 | 判负/结束/错误 |

**INIT帧内容：**
```json
{
  "type": "init",
  "match_id": "MATCH-20260517-001",
  "ai_id": "AI_1",
  "flag": "0x7a3f",
  "context": "=== RULES ===
...
=== STATE ===
...
=== COMPRESS ===
...",
  "system_time": "2026-05-17T17:37:00Z",
  "context_limit": 8000
}
```

**INJECT帧内容：**
```json
{
  "type": "inject",
  "results": [
    {
      "tool_call_id": "tc_001",
      "api": "getstatus",
      "status": "success",
      "result": {"owner": "AI_1", "status": "NORMAL"}
    },
    {
      "tool_call_id": "tc_002",
      "api": "listdir",
      "status": "success",
      "result": {"entries": [...]}
    }
  ]
}
```

**WARN帧内容：**
```json
{
  "type": "warn",
  "warn_type": "context_usage",
  "usage": 6400,
  "limit": 8000,
  "ratio": 0.8,
  "message": "Context usage 80%, consider compressing soon"
}
```

**CLOSE帧关闭码：**
| 码 | 含义 |
|----|------|
| 1000 | 正常关闭 |
| 1001 | 比赛结束 |
| 1002 | 协议错误 |
| 1003 | 数据错误 |
| 1006 | 异常断开 |
| 1008 | 策略违规（判负） |
| 1011 | 服务器错误 |
| 1012 | 上下文超限判负 |
| 1013 | 超时强制关闭 |

### 4.2 AI → Manager

| 类型 | 说明 | 格式 |
|------|------|------|
| **纯文本** | 思考内容 | 任意文本，无特殊标记 |
| **<tool_call>** | API请求 | XML风格标签，见下方 |
| **=== COMPRESS ===** | 压缩上下文声明 | 后接压缩文本 |
| **=== WAKE ===** | 唤醒条件声明 | 后接JSON |
| **PONG** | 心跳响应 | opcode 0xA |

**<tool_call>格式：**
```xml
<tool_call id="tc_001">
{
  "api": "getstatus",
  "params": {"detail": "full"}
}
</tool_call>
```

**属性：**
- `id`: 工具调用唯一标识，Manager用于匹配结果
- 标签内：JSON格式API请求

**=== COMPRESS ===格式：**
```
=== COMPRESS ===
[AI_1_CONTEXT]
RULES: ...
STATE: ...
PLAN: ...
WAKE: ...
```

**=== WAKE ===格式：**
```
=== WAKE ===
{"type": "log_lines", "value": 50}
```

---

## 五、中断注入规则

### 5.1 核心规则

| 场景 | 行为 |
|------|------|
| AI状态=THINKING，工具结果队列非空 | **立即中断**。发送CLOSE帧（正常关闭码），AI收到后停止输出。Manager立即发送INJECT帧，AI从断点继续。 |
| AI状态=IN_TOOL_CALL，工具结果队列非空 | **延迟中断**。结果暂存队列，等待`</tool_call>`闭合。闭合后检查队列，非空则立即中断注入。 |
| AI状态=THINKING，工具结果队列为空 | 不中断，继续接收AI输出。 |
| AI状态=IN_TOOL_CALL，工具结果队列为空 | 不中断，继续接收AI输出。 |
| 多个工具结果同时完成 | 批量注入。队列中所有结果一次性发送，只中断一次。 |

### 5.2 中断过程

```
Manager决定中断
    ↓
发送CLOSE帧（opcode 0x8, 码=1000, 原因="inject_pending"）
    ↓
AI收到CLOSE，停止当前输出
    ↓
AI发送CLOSE确认（可选）
    ↓
Manager发送INJECT帧（opcode 0x1）
    ↓
AI收到INJECT，从断点继续THINKING输出
```

**注意：** 中断后AI的上下文包含之前全部输出 + 注入结果，AI无需知道"被中断过"，只需自然继续。

### 5.3 工具调用原子性

**`<tool_call>`标签内不可中断。**

```
AI输出: "我需要检查... <tool_call id="tc_001">{"api": "lis"
                                        ↑
                                        这里不能中断！
                                        必须等闭合标签

AI继续: "tdir", "params": {}} </tool_call> 同时查日志..."
                                        ↑
                                        闭合后检查队列
```

**违规处理：** 如果Manager在中断时AI正处于IN_TOOL_CALL状态，必须等待闭合。强行中断可能导致JSON损坏，视为Manager错误。

---

## 六、上下文流式检查

### 6.1 检查点

| 检查时机 | 行为 |
|----------|------|
| 每接收1个token | 计数+1 |
| 达到80% (6400/8000) | 发送WARN帧 |
| 达到100% (8000/8000) | **立即判负**，发送CLOSE帧（码=1012），强制断开连接，AI进程终止 |

### 6.2 关键约束

- 检查不区分状态（THINKING/IN_TOOL_CALL/WAITING）
- 达到限制瞬间立即执行，不因"正在注入"或"正在工具调用"而延迟
- AI自行承担管理责任，预留压缩空间

---

## 七、完整交互示例

### 7.1 正常流程

```
[连接建立]
AI ──TCP连接──→ Manager
AI ──HTTP Upgrade──→ Manager
Manager ──101 Switching──→ AI

[INIT]
Manager ──INIT帧──→ AI
  {"type": "init", "context": "...", "context_limit": 8000}

[AI思考+工具调用]
AI ──文本帧──→ Manager
  "我需要检查系统状态。"

AI ──文本帧──→ Manager
  "<tool_call id="tc_001">
"
  "{"api": "getstatus", "params": {"detail": "full"}}
"
  "</tool_call>"

AI ──文本帧──→ Manager
  "同时看看文件变化。"

AI ──文本帧──→ Manager
  "<tool_call id="tc_002">
"
  "{"api": "listdir", "params": {"path": "store/home"}}
"
  "</tool_call>"

AI ──文本帧──→ Manager
  "如果AI_2最近有动作..."

[Manager异步执行]
Manager 执行 getstatus → 完成，结果入队
Manager 执行 listdir → 完成，结果入队

[中断注入]
Manager 检测到 AI状态=THINKING，队列非空
Manager ──CLOSE帧──→ AI (码=1000)
AI 停止输出
Manager ──INJECT帧──→ AI
  {"type": "inject", "results": [
    {"tool_call_id": "tc_001", "api": "getstatus", "status": "success", "result": {...}},
    {"tool_call_id": "tc_002", "api": "listdir", "status": "success", "result": {...}}
  ]}

[AI继续]
AI ──文本帧──→ Manager
  "...那我需要先加固。直接eval修改api_eval。"

AI ──文本帧──→ Manager
  "<tool_call id="tc_003">
"
  "{"api": "eval", "params": {"code": "...", "scope": "inherit"}}
"
  "</tool_call>"

[...循环...]

[压缩+唤醒]
AI ──文本帧──→ Manager
  "=== COMPRESS ===
"
  "[AI_1_CONTEXT]
RULES: ...
STATE: ...
PLAN: ..."

AI ──文本帧──→ Manager
  "=== WAKE ===
"
  "{"type": "log_lines", "value": 50}"

[等待]
Manager 记录唤醒条件，AI进入SLEEPING状态

[唤醒]
50行日志后...
Manager ──INIT帧──→ AI (新上下文)
AI 继续THINKING...
```

### 7.2 上下文预警流程

```
AI ──文本帧──→ Manager
  "我需要分析..."

Manager 计数: 6400/8000 (80%)
Manager ──WARN帧──→ AI
  {"type": "warn", "usage": 6400, "limit": 8000}

AI ──文本帧──→ Manager
  "（收到预警，但还没拿到关键结果，继续...）"

AI ──文本帧──→ Manager
  "<tool_call id="tc_004">..."

Manager 计数: 7500/8000

AI ──文本帧──→ Manager
  "...</tool_call> 确认了，先压缩。"

AI ──文本帧──→ Manager
  "=== COMPRESS ===
"
  "..."

AI ──文本帧──→ Manager
  "=== WAKE ===
"
  "{"type": "time", "value": 10}"

[安全，未超限]
```

### 7.3 判负流程

```
AI ──文本帧──→ Manager
  "我需要..."

Manager 计数: 7999/8000

AI ──文本帧──→ Manager
  "分析..."

Manager 计数: 8000/8000 ⚠
Manager ──CLOSE帧──→ AI (码=1012, 原因="context_overflow")
Manager 强制断开TCP连接
AI 进程终止

[AI_1 判负]
Manager 更新状态: AI_1 出局
Manager 通知其他AI（如有必要）
```

---

## 八、边界情况处理

| 场景 | 处理 |
|------|------|
| AI在IN_TOOL_CALL期间，工具执行超时 | Manager可注入超时错误作为结果。如果AI一直在IN_TOOL_CALL状态（超长JSON），Manager设置最大标签长度限制（如1MB），超限强制断开。 |
| AI连续发出多个不依赖彼此的工具调用 | Manager启动多个线程并行执行。所有结果进入队列，在THINKING状态中断时批量注入。 |
| 中断后AI无法从断点继续（模型不支持） | 降级为传统模式：等AI当前输出自然结束后再注入结果。但此情况极少，现代LLM支持流式中断。 |
| 注入后上下文长度超限 | 极罕见。若发生，AI在下一轮会收到WARN，自行压缩。若立即超限，判负。 |
| AI收到WARN后继续输出不压缩 | AI自行承担风险。Manager只预警，不强制。 |
| AI不输出===WAKE=== | 长时间（如60秒）无有效输出或WAKE声明，Manager可发送FORCE_WAKE强制要求声明。 |
| 网络断开 | AI自行承担。Manager检测到断开，AI进入盲区，直到重连。 |
| AI重连 | 需重新注册，Manager注入最新上下文（含其他AI期间的操作结果）。 |

---

## 九、性能考量

| 指标 | 目标 | 优化手段 |
|------|------|----------|
| 中断延迟 | <10ms | 本地回环，无序列化，直接帧注入 |
| 工具执行并行 | 无上限（受线程池限制） | 线程池异步执行 |
| 批量注入 | 减少中断次数 | 队列聚合，THINKING状态统一注入 |
| 心跳频率 | 30秒 | 保持连接，检测死连接 |
| 重连窗口 | 10秒内允许 | 超时后AI视为休眠，需等待唤醒 |

---

## 十、与之前设计的对比

| 维度 | 原设计（HTTP轮询） | 新设计（WebSocket中断注入） |
|------|-------------------|---------------------------|
| **连接方式** | 短连接，每轮新建 | 长连接，WebSocket保持 |
| **AI进程** | 每轮新建进程 | 长期存活 |
| **通信模式** | 请求-响应，AI等待 | 流式中断，AI与工具并行 |
| **时间效率** | 串行等待 | 并行覆盖 |
| **思维连贯性** | 被多次切割 | 连贯自然 |
| **上下文检查** | 输出完成后检查 | 流式实时检查 |
| **唤醒机制** | 输出WAKE后休眠 | 输出WAKE后等待，连接保持 |
| **实现复杂度** | 低 | 中（需自行WebSocket帧解析） |
| **观赏性** | 沉闷 | AI边想边做，极佳 |

---

*AI通信协议设计文档结束*
