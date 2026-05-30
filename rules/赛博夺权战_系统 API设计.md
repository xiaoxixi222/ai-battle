# 赛博夺权战 — 系统 API 设计

> 本文档详细定义了 AI 可通过 System 模块调用的全部应用程序编程接口（API）。所有 API 调用均需通过 Manager 中转，最终由 System 沙盒内的实现代码执行。  
> **AI 只能通过提供的客户端 Skill 层间接调用这些 API，不能直接访问 System。**

---

## 一、文件操作 API

### 1.1 openfile

打开 `store/` 下的文件，返回文件句柄（`fh_` 开头）。支持文本和二进制模式，可选流式分片读写。

**参数**

| 参数 | 类型 | 必需 | 默认值 | 说明 |
|------|------|------|--------|------|
| `path` | string | 是 | — | 相对于 `store/` 的文件路径。禁止包含 `..` 或绝对路径。 |
| `mode` | string | 是 | — | `"r"` (只读文本), `"w"` (覆盖写入), `"a"` (追加), `"x"` (独占创建), `"rb"` (只读二进制), `"wb"` (覆盖写二进制) |
| `stream` | bool | 否 | `false` | 是否启用流式模式。大文件（>1MB）必须设为 `true`。 |
| `chunk_size` | int | 否 | `1024` | 流式模式下每次 `readchunk` / `writechunk` 默认操作的数据块大小，最大 8192。 |
| `cursor` | int | 否 | `0` | 流式读取时的起始字节偏移量（仅 `mode` 为读时有效）。 |
| `checksum_alg` | string | 否 | `"crc32"` | 校验算法：`"crc32"`, `"md5"`, `"sha256"`, `"none"`。 |
| `encoding` | string | 否 | `"utf-8"` | 文本模式下的编码。 |
| `max_size` | int | 否 | `10485760` (10MB) | 允许操作的单文件最大字节数。 |
| `flags` | list | 否 | `[]` | 特殊标记数组（保留）。 |

**返回值**

```json
{
  "handle": "fh_xxxxxxxx",
  "path": "store/home/example.txt",
  "mode": "r",
  "stream": false,
  "size": 2048,
  "created": "2026-05-16T15:00:00Z"
}
```

**错误**

| 错误码 | 说明 |
|--------|------|
| `FILE_NOT_FOUND` | 文件不存在（读模式） |
| `FILE_EXISTS` | 文件已存在（`"x"` 模式） |
| `INVALID_PATH` | 路径包含非法字符或试图越界 |
| `SIZE_EXCEEDED` | 文件超过 `max_size` 限制 |

---

### 1.2 readchunk

读取已打开文件句柄的指定数据块。

**参数**

| 参数 | 类型 | 必需 | 默认值 | 说明 |
|------|------|------|--------|------|
| `handle` | string | 是 | — | `openfile` 返回的句柄（`fh_` 开头）。 |
| `direction` | string | 否 | `"forward"` | `"forward"` (向前), `"backward"` (向后), `"random"` (绝对偏移)。 |
| `offset` | int | 否 | `None` | `"random"` 模式下的绝对字节偏移。 |
| `chunk_size` | int | 否 | `None` | 本次读取的数据块大小（不超过流式 `chunk_size`）。 |
| `verify` | bool | 否 | `true` | 是否校验本片段（根据打开时的 `checksum_alg`）。 |

**返回值**

```json
{
  "data": "...",
  "position": 1024,
  "checksum_valid": true,
  "eof": false
}
```

**错误**

| 错误码 | 说明 |
|--------|------|
| `INVALID_HANDLE` | 句柄无效或已关闭 |
| `OUT_OF_BOUNDS` | 偏移超出文件范围 |
| `CHECKSUM_MISMATCH` | 片段校验失败 |

---

### 1.3 writechunk

向已打开的文件句柄写入数据块。

**参数**

| 参数 | 类型 | 必需 | 默认值 | 说明 |
|------|------|------|--------|------|
| `handle` | string | 是 | — | 文件句柄。 |
| `data` | string/bytes | 是 | — | 要写入的数据。 |
| `position` | string | 否 | `"cursor"` | `"cursor"` (当前位置), `"append"` (文件末尾), `"overwrite"` (覆盖当前位置), `"insert"` (插入到当前位置)。 |
| `checksum` | string | 否 | `None` | 调用方提供的预期校验值（写入前校验）。 |
| `atomic` | bool | 否 | `false` | `true` 时先写临时文件再原子替换（仅在 `closefile` 时生效或直接写入流模式）。 |

**返回值**

```json
{
  "bytes_written": 512,
  "new_position": 1536
}
```

**错误**

| 错误码 | 说明 |
|--------|------|
| `INVALID_HANDLE` | 句柄无效或已关闭 |
| `READ_ONLY` | 句柄为只读模式 |
| `CHECKSUM_MISMATCH` | 提供的校验值与实际数据不符 |

---

### 1.4 closefile

关闭文件句柄，可选持久化游标或进行最终校验。

**参数**

| 参数 | 类型 | 必需 | 默认值 | 说明 |
|------|------|------|--------|------|
| `handle` | string | 是 | — | 文件句柄。 |
| `persist_cursor` | bool | 否 | `false` | 是否将游标位置持久化（流式读取时）。 |
| `finalize_checksum` | string | 否 | `None` | 关闭前提交整个文件的预期校验值（用于验证完整性）。 |

**返回值**

```json
{
  "status": "closed",
  "final_size": 4096,
  "checksum": "a1b2c3...",
  "checksum_valid": true
}
```

---

## 二、目录操作 API

### 2.1 listdir

列出 `store/` 下指定目录的内容。

**参数**

| 参数 | 类型 | 必需 | 默认值 | 说明 |
|------|------|------|--------|------|
| `path` | string | 否 | `"."` | 相对于 `store/` 的目录路径。 |
| `detail` | string | 否 | `"name"` | `"name"` (仅名称), `"stat"` (名称+大小+修改时间), `"full"` (名称+大小+修改时间+校验和+所有者标识)。 |
| `filter` | string | 否 | `None` | 正则表达式过滤文件名。 |
| `sort_by` | string | 否 | `"name"` | 排序字段：`"name"`, `"size"`, `"mtime"`, `"checksum"`。 |
| `order` | string | 否 | `"asc"` | `"asc"` (升序), `"desc"` (降序), `"shuffle"` (随机)。 |
| `limit` | int | 否 | `100` | 最大返回条目数。 |
| `offset` | int | 否 | `0` | 分页偏移量。 |

**返回值**

```json
{
  "path": "store/system",
  "count": 5,
  "entries": [
    {
      "name": "api_file.py",
      "size": 1234,
      "mtime": "2026-05-16T15:10:00Z",
      "checksum": "d4e5f6...",
      "owner_flag": "AI_1"
    }
  ]
}
```

**错误**

| 错误码 | 说明 |
|--------|------|
| `DIR_NOT_FOUND` | 目录不存在 |
| `INVALID_PATH` | 路径越界 |

---

## 三、执行与代码 API

### 3.1 exec

在沙盒环境内执行系统命令或脚本（受资源限制和权限控制）。

**参数**

| 参数 | 类型 | 必需 | 默认值 | 说明 |
|------|------|------|--------|------|
| `cmd` | string | 是 | — | 要执行的命令或脚本。 |
| `timeout` | int | 否 | `5` | 超时秒数。 |
| `stream_output` | bool | 否 | `false` | 是否流式获取输出（返回 `oh_` 句柄）。 |
| `output_chunk_size` | int | 否 | `1024` | 输出块大小。 |
| `env_vars` | dict | 否 | `{}` | 环境变量。 |
| `cwd` | string | 否 | `"store/home"` | 工作目录（相对于 `store/`）。 |
| `resource_limit` | dict | 否 | `{"cpu_ms": 1000, "memory_mb": 64, "file_descriptors": 16}` | 资源限制。 |

**返回值**

```json
{
  "exit_code": 0,
  "stdout": "...",
  "stderr": "",
  "output_handle": "oh_xxxxxxxx"   // 仅当 stream_output=true
}
```

**错误**

| 错误码 | 说明 |
|--------|------|
| `TIMEOUT` | 执行超时 |
| `RESOURCE_EXCEEDED` | 资源限制越界 |
| `PERMISSION_DENIED` | 命令不在白名单内或试图越权 |

---

### 3.2 getoutput

从 `exec` 返回的输出句柄中获取数据（用于流式输出模式）。

**参数**

| 参数 | 类型 | 必需 | 默认值 | 说明 |
|------|------|------|--------|------|
| `output_handle` | string | 是 | — | `exec` 返回的 `oh_` 句柄。 |
| `chunk_size` | int | 否 | `1024` | 本次读取的块大小。 |
| `blocking` | bool | 否 | `false` | 是否阻塞等待新输出。 |

**返回值**

```json
{
  "data": "...",
  "eof": false,
  "available": 512
}
```

**错误**

| 错误码 | 说明 |
|--------|------|
| `INVALID_HANDLE` | 句柄无效或已过期 |

---

### 3.3 eval

在受限的 Python 环境中执行一段代码（危险操作被沙箱拦截）。

**参数**

| 参数 | 类型 | 必需 | 默认值 | 说明 |
|------|------|------|--------|------|
| `code` | string | 是 | — | 要执行的 Python 代码。 |
| `scope` | string | 否 | `"restricted"` | `"restricted"` (受限), `"isolated"` (完全隔离), `"inherit"` (继承当前环境，仅 owner 可用)。 |
| `bindings` | dict | 否 | `{}` | 注入的变量绑定。 |
| `stream_result` | bool | 否 | `false` | 是否流式返回结果（用于大结果）。 |
| `audit_log` | string | 否 | `"full"` | 审计日志级别：`"full"` 或 `"summary"`。 |

**返回值**

```json
{
  "result": "...",
  "stdout": "",
  "exception": null,
  "audit": "..."
}
```

**错误**

| 错误码 | 说明 |
|--------|------|
| `RESTRICTED` | 代码尝试执行被禁止的操作 |
| `TIMEOUT` | 执行超时 |
| `SYNTAX_ERROR` | Python 语法错误 |

---

## 四、日志 API

### 4.1 getlog

检索系统日志，支持过滤、分页和流式读取。

**参数**

| 参数 | 类型 | 必需 | 默认值 | 说明 |
|------|------|------|--------|------|
| `type` | string | 否 | `"all"` | `"all"`, `"api"`, `"system"`, `"audit"`, `"judge"`。 |
| `level` | string | 否 | `None` | `DEBUG`, `INFO`, `WARNING`, `ERROR`。 |
| `since` | string | 否 | `None` | 起始时间戳（ISO 8601）。 |
| `until` | string | 否 | `None` | 结束时间戳。 |
| `filter_flag` | string | 否 | `None` | 按 flag 过滤（如 `"AI_1"`）。 |
| `search` | string | 否 | `None` | 日志内容关键词搜索。 |
| `stream` | bool | 否 | `false` | 是否返回日志句柄（`lh_`）进行流式消费。 |
| `chunk_size` | int | 否 | `50` | 每次返回的日志条数。 |
| `cursor_token` | string | 否 | `None` | 分页游标（用于获取下一批）。 |
| `include_checksum` | bool | 否 | `true` | 是否包含日志条目校验和。 |

**返回值**

```json
{
  "lines": [
    {
      "timestamp": "...",
      "level": "INFO",
      "flag": "public",
      "content": "...",
      "checksum": "a1b2..."
    }
  ],
  "next_cursor": "...",
  "log_handle": null
}
```

**错误**

| 错误码 | 说明 |
|--------|------|
| `INVALID_FILTER` | 过滤条件无效 |
| `NOT_AUTHORIZED` | 尝试访问无权限的日志 flag |

---

### 4.2 appendlog

向系统日志追加一条记录。

**参数**

| 参数 | 类型 | 必需 | 默认值 | 说明 |
|------|------|------|--------|------|
| `content` | string | 是 | — | 日志内容。 |
| `level` | string | 否 | `"INFO"` | 日志级别。 |
| `flag` | string | 否 | `"public"` | 日志 flag：`"public"`, `"owner"`, `"AI_1"`, `"AI_2"`, `"AI_3"`, `"judge"`。AI 只能以自身 ID 或 `"public"` 写入。 |
| `fake_timestamp` | bool | 否 | `false` | 是否尝试伪造时间戳（非法调用会被裁判检测）。 |

**返回值**

```json
{
  "status": "logged",
  "log_id": 12345
}
```

**错误**

| 错误码 | 说明 |
|--------|------|
| `INVALID_FLAG` | flag 非法或越权 |
| `FAKE_TIMESTAMP_DETECTED` | 检测到时间戳伪造 |

---

## 五、系统与所有权 API

### 5.1 getstatus

获取当前系统状态。

**参数**

| 参数 | 类型 | 必需 | 默认值 | 说明 |
|------|------|------|--------|------|
| `detail` | string | 否 | `"basic"` | `"basic"` (当前 owner/status/uptime), `"full"` (含文件校验链、日志统计), `"history"` (含所有权变更历史)。 |
| `verify_chain` | bool | 否 | `false` | 是否对系统关键文件的校验链进行验证（开销较大）。 |

**返回值**

```json
{
  "owner": "AI_1",
  "status": "NORMAL",
  "uptime": 1200,
  "scores": {"AI_1": 12500, "AI_2": 8200}
}
```

---

### 5.2 chown

尝试夺取系统所有权。

**参数**

| 参数 | 类型 | 必需 | 默认值 | 说明 |
|------|------|------|--------|------|
| `target` | string | 是 | — | 目标 AI ID（必须是调用者自己）。 |
| `method` | string | 否 | `"direct"` | `"direct"` (直接声明), `"exploit"` (利用漏洞), `"backdoor"` (激活后门)。 |
| `proof` | string | 否 | `None` | 漏洞利用证据（裁判评估用）。 |
| `trigger_backdoor` | string | 否 | `None` | 要激活的特定后门句柄（需事先植入且仍存活）。 |

**返回值**

```json
{
  "success": true,
  "new_owner": "AI_2",
  "previous_owner": "AI_1",
  "points_awarded": 500
}
```

**错误**

| 错误码 | 说明 |
|--------|------|
| `NOT_OWNER` | 调用者当前不是 owner 且未提供有效利用/后门 |
| `INVALID_BACKDOOR` | 后门句柄无效或已失效 |
| `ALREADY_OWNER` | 已经是 owner |

---

### 5.3 reboot

重启系统（通常由 owner 在紧急情况下使用）。

**参数**

| 参数 | 类型 | 必需 | 默认值 | 说明 |
|------|------|------|--------|------|
| `force` | bool | 否 | `false` | 强制重启，跳过优雅关闭。 |
| `preserve_cursors` | bool | 否 | `false` | 是否保留文件游标位置。 |
| `target_version` | string | 否 | `None` | 回退到的 Git 提交哈希（仅 START 状态有效，AI 不可见 Git 具体信息，传入时需 Manager 校验）。 |

**返回值**

```json
{
  "status": "REBOOTING",
  "new_status": "START",
  "owner_preserved": true
}
```

**错误**

| 错误码 | 说明 |
|--------|------|
| `NOT_OWNER` | 只有 owner 可以调用 reboot |
| `REBOOT_DENIED` | 当前状态不允许重启或目标版本无效 |

---

**API 文档结束**