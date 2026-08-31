# Learnings

Corrections, insights, and knowledge gaps captured during development.

**Categories**: correction | insight | knowledge_gap | best_practice

---

## [LRN-20260831-001] correction

**Logged**: 2026-08-31T15:24:00+08:00
**Priority**: high
**Status**: resolved
**Area**: backend

### Summary
凭据不能返回给模型明文，否则会写进对话和 execute 命令。

### Details
第一版 get_credential 把密码交给模型，时间线只藏了工具回调。模型随后在回复和 `$env:GMAIL_PASSWORD="..."` 里复述明文。正确做法：工具只给环境变量名，execute 注入 WOKBEE_CRED_<ALIAS>_USERNAME/PASSWORD，展示层再按保险箱内容脱敏。

### Suggested Action
已改为环境变量注入 + 时间线/审批/落盘脱敏。

### Metadata
- Source: user_feedback
- Related Files: src/wokbee/engine/credential_tools.py, src/wokbee/engine/runtime_env.py, src/wokbee/ui/timeline.py
- Tags: credentials, redaction

---

## [LRN-20260831-002] correction

**Logged**: 2026-08-31T17:10:00+08:00
**Priority**: high
**Status**: resolved
**Area**: backend

### Summary
MCP StructuredTool 只有 coroutine，同步 Agent.stream 必须补 sync 桥，否则 invoke 报 does not support sync invocation。

### Details
langchain-mcp-adapters 转换的工具不设 func。WokBee 走 agent.stream → ToolNode.tool.invoke。包装 coroutine 时要同时挂 `_as_sync_bridge`（无事件循环则 asyncio.run）。

### Suggested Action
已在 wrap_tools_truncate_results 的 coroutine 路径写入 func 同步桥。

### Metadata
- Source: user_feedback
- Related Files: src/wokbee/engine/tool_truncate.py
- Tags: mcp, structuredtool

---

## [LRN-20260831-003] correction

**Logged**: 2026-08-31T19:06:00+08:00
**Priority**: high
**Status**: resolved
**Area**: backend

### Summary
MCP 工具 `response_format=content_and_artifact`，包装截断不能把 `(content, artifact)` 收成 str。

### Details
langchain-mcp-adapters 的 call_tool 返回二元组。wrap 后 `_truncate` 把 tuple 转成字符串，StructuredTool._run 再校验格式即失败。截断时保留二元组；超时等纯字符串结果也要补 `(text, None)`。

### Suggested Action
已在 tool_truncate._truncate 按 response_format 保留/补齐 artifact 元组。

### Metadata
- Source: user_feedback
- Related Files: src/wokbee/engine/tool_truncate.py
- Tags: mcp, structuredtool, content_and_artifact

---

