# Feature Requests

Capabilities requested by the user.

---

## [FEAT-20260831-001] local_credential_vault

**Logged**: 2026-08-31T14:47:00+08:00
**Priority**: high
**Status**: in_progress
**Area**: backend

### Requested Capability
在本机安全录入其它系统的账号密码，供 Agent 登录时取用。

### User Context
Agent 经常要登录外部系统；需要人可管理、Agent 可取用。不采用环境变量当保险箱。厂商 API Key 本轮不迁入。

### Complexity Estimate
medium

### Suggested Implementation
AES-256-GCM 信封 + Windows 凭据管理器存主密钥；AIConfig 凭据库页；list_credentials / get_credential（高危审批）；时间线脱敏。

### Metadata
- Frequency: first_time
- Related Features: AIConfig, approval_policy

---

