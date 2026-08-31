## [ERR-20260831-001] AKUAI_new_api_do_request_failed

**Logged**: 2026-08-31T17:15:28+08:00
**Priority**: high
**Status**: pending
**Area**: infra

### Summary
交互首包模型 HTTP 返回 500，new_api `do_request_failed` / upstream error，不是 MCP 同步调用失败。

### Error
```
Error code: 500 - {'error': {'message': 'upstream error: do request failed', 'type': 'new_api_error', 'code': 'do_request_failed'}}
```

### Context
- 缓存前缀已钉死，tools=54，模型 AKUAI/deepseek-v4-flash
- 约 2 秒后失败（与调用最小间隔 sleep 吻合），时间线尚无工具调用框
- 上一轮 MCP StructuredTool sync 已修；本错误发生在 Chat Completions 转发上游时

### Suggested Fix
无法在本机修复网关。稍后重试；持续失败则减少 MCP 工具数量或查厂商 request id。UI 已区分此类错误文案。

### Metadata
- Reproducible: unknown
- Related Files: src/wokbee/engine/runner.py, src/wokbee/engine/model_factory.py
- See Also: LRN-20260831-002

---

## [ERR-20260831-002] mcp_content_and_artifact_tuple

**Logged**: 2026-08-31T19:04:22+08:00
**Priority**: high
**Status**: resolved
**Area**: backend

### Summary
MCP `get_user_participant_projects` 调用后交互失败：content_and_artifact 期望二元组，包装层返回了 str。

### Error
```
Since response_format='content_and_artifact' a two-tuple of the message content and raw tool output is expected. Instead, generated response is of type: <class 'str'>.
```

### Context
- 同步桥已生效，工具真正跑起来后在 StructuredTool._run 校验失败
- 根因是 wrap_tools_truncate_results 把 MCP `(content, artifact)` 截成字符串

### Suggested Fix
已修 `_truncate`：保留/补齐二元组。

### Metadata
- Reproducible: yes
- Related Files: src/wokbee/engine/tool_truncate.py
- See Also: LRN-20260831-003

---

