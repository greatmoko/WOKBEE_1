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

