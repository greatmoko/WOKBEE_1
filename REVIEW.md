# WokBee 系统审查报告

> 审查日期：2026-09-03
> 范围：TokBee 对话、WokBee 项目 Agent、AutoBee 定时任务、消息网关（飞书/微信）、UI 层
> 方法：AST 静态分析（未定义名 / 未用导入 / 重复定义 / 裸 except）+ 4 路并行子代理深读 + 逐条核对源码确认
> 结论：未发现会导致语法错误的明显缺陷；以下为按优先级排列的真实 Bug 与优化点，全部标注 `file:line` 便于定位。

---

## 0. 修复记录

### 设置页功能更新（2026-09-03）

- Settings 页的「删除非置顶项目」按钮已改为与「清空非置顶聊天」一致的危险操作样式。
- Settings 页新增「一键重置记忆」：确认后清空全局 Agent 记忆仓库（SQLite `memory` 表），并将 `overview.md` 恢复为系统初始版本；项目目录中的 `memory/experiences/` 与 `scripts/` 不受影响。

> 修复日期：2026-09-03。下方 H1 / H2 / H3 / H4 / H5 / M1 / M7 已修复，其余条目仍待处理。所有修复均经 `py_compile` / 运行时冒烟测试验证。

| 编号 | 状态 | 变更文件 | 说明 |
|------|------|----------|------|
| H1 | ✅ 已修复 | `provider_store.py` | API Key 落盘改为 AES-256-GCM 信封加密，主密钥复用保险箱的 Keyring 后端（Windows 凭据管理器）；兼容旧明文自动迁移；`resolve()` 对已移除厂商返回 `None`。新增 `_seal_key` / `_open_key` / `_get_master_key`。 |
| H2 | ✅ 已修复 | `executor.py`、`script_runner.py`、`subprocess_util.py` | `subprocess.run(timeout=)` 全部替换为 `run_cancellable`；成功判定同时检查 `stderr` 失败哨兵；`run_cancellable` 额外修复了「孙进程占用管道时 `close()` 阻塞导致挂死」的根本问题（仅在线程结束后才显式关流）。 |
| H3 | ✅ 已修复 | `chat_view.py`、`workspace.py` | 被遮蔽的 `QThread.finished` 信号改名为 `name_ready` / `compact_done`，内置完成信号恢复；emit/connect 同步更新。 |
| H4 | ✅ 已修复 | `ai_client.py` | 429 限流重试计入 `attempt` 预算（`attempt < max_retries - 1`），并记录 `last_exc` 供最终报错；不再受 `rate_limit_count < 100` 无界约束。 |
| H5 | ✅ 已修复 | `feishu.py` | 每个频道改用独立 asyncio 事件循环（并重定向 SDK 模块级 loop 指针）；`stop()` 关闭 WS、取消任务、`loop.close()`；新增 `_close_loop`。修复重启时幽灵消息 / "event loop already running" 误报 / 资源泄漏。 |
| M1 | ✅ 已修复 | `runner.py` | 新增 `_events_lock` + `_snapshot_run_events()`，`_emit` 的追加与所有读取/迭代均加锁，消除「取消后流线程并发写 `_run_events` 导致 changed-size / 丢事件」。流线程在取消后于下一个 chunk 退出（受在途模型调用时长约束）。 |
| M7 | ✅ 已修复 | `wechat.py` | 新增 `_ctx_lock` 守护 `bot._ctx_cache`，poll 线程写与 send 线程读统一串行化，消除 dict 并发读写竞态。 |

> ⚠️ 重要：修复 H2 过程中发现 `run_cancellable` 自身存在同一挂死缺陷（孙进程占用 stdout 管道时，`finally` 中 `close()` 阻塞到孙进程退出）。该缺陷一并修复，否则脚本超时修复不完整。详见下方 H2 附注。

---

## 1. 高优先级（稳定性 / 安全）

### H1. API Key 明文落盘 —— ✅ 已修复
- **位置**：`src/tokbee/core/provider_store.py:64`
- **问题**：`to_dict()` 把 `api_key` 明文写入 `~/.wokbee/providers.json`（经 `safe_write_json` 落盘）。任何能读取本机文件的程序 / 备份 / 恶意软件都能直接提取所有厂商密钥。
- **建议**：落盘时加密（Windows 可用 DPAPI 或 `keyring`），仅存密文、加载时内存解密；项目已有 `src/wokbee/core/credential_crypto.py` 可复用。同时 `resolve()`（`provider_store.py:416-441`）在厂商被移除后仍用缓存密钥调用，应一并修正。
- **已修复**：`ProviderSettings.to_dict()` 经 `_seal_key()` 用 AES-256-GCM 信封加密 `api_key` 后落盘（主密钥复用保险箱 Keyring 后端）；`from_dict()` 经 `_open_key()` 解密并兼容旧明文自动迁移。`resolve()` 对已从「我的厂商」移除的项直接返回 `None`，不再用缓存密钥继续调用。

### H2. Windows 下脚本超时不生效 → 可永久挂死 —— ✅ 已修复
- **位置**：`src/autobee/engine/executor.py:141`、`src/wokbee/engine/script_runner.py:306`
- **问题**：两者均用 `subprocess.run(..., timeout=...)`。当脚本（如 `node`/`pwsh` 启动孙进程）继承 stdout/stderr 管道时，`subprocess.run` 内部的 `communicate()` 永不返回，超时形同虚设，任务卡在「运行中」。项目自带的 `run_cancellable`（`src/tokbee/core/subprocess_util.py`，墙钟超时 + Job Object 杀进程树）正是为解决此问题而写，但这两处未使用。
- **建议**：改用 `run_cancellable`，并把 `script_runner.py:320` 的成功判定同时检查 `stderr` 中的失败哨兵（当前只看 `out`）。
- **已修复**：
  - `executor._run_script` 与 `script_runner.run_one_script` 改用 `run_cancellable`（墙钟超时 + 杀进程树），并移除不再需要的 `subprocess` / `nowin` / `_decode_bytes` 死代码；为 `run_one_script` / `run_script_phase` / `run_pipeline_until_ai_or_end` 增加 `cancel_event` 透传。
  - `script_runner` 成功判定同时检查 `out` 与 `err` 中的「脚本执行失败」哨兵（修正后 `exit(0)` + 哨兵写 stderr 也会判失败）。
  - **附注（额外修复）**：`run_cancellable` 自身在孙进程占用管道时，`finally` 中 `proc.stdout.close()` 会阻塞到孙进程退出，超时仍会挂死。已改为仅在对应读取线程结束后才显式 `close()`，从根本上消除挂死。测试：孙进程挂 30s 的场景从「阻塞 30s」降到「约 3.7s 正常返回」。

### H3. `QThread.finished` 信号被同名自定义信号遮蔽 —— ✅ 已修复
- **位置**：`src/tokbee/ui/views/chat_view.py:360`、`:381`、`src/wokbee/ui/workspace.py:86`
- **问题**：在 `QThread` 子类中定义 `finished = Signal(...)`，覆盖 Qt 内置的无参完成信号，导致 `run()` 返回后的线程完成事件丢失 / 语义错位，依赖标准 `finished` 的清理与完成检测失效。
- **建议**：改为不冲突的命名（如 `name_ready` / `compact_done`），并同步更新 emit/connect 点（`chat_view.py:1904`、`workspace.py:913`）。
- **已修复**：`_AiNameWorker.finished` → `name_ready`，`_CompactWorker.finished` → `compact_done`（两个文件），emit/connect 全部同步更新（`chat_view.py:374,419,1904,2601`、`workspace.py:138,913`）。内置 `finished` 信号恢复正常。

### H4. 429 限流重试无上限 —— ✅ 已修复
- **位置**：`src/tokbee/core/ai_client.py:169-175`（同步）、`:303-309`（流式）
- **问题**：429 分支 `continue` 不递增 `attempt`，只受 `rate_limit_count < 100` 约束，且每次重新新建线程、不复用请求。`retry_interval` 较大时可单次请求挂 100 分钟。
- **建议**：429 路径也计入 `attempt`（或单独设更低上限，如 `min(rate_limit_count, max_retries)`），并复用已有请求。
- **已修复**：429 分支现在同时递增 `attempt` 与 `rate_limit_count`，条件收紧为 `attempt < max_retries - 1`，使 429 与其他可重试错误共用同一重试预算（总调用次数 ≤ `max_retries`），不再受 100 次无界约束；同时记录 `last_exc` 供最终报错展示真实原因。

### H5. 飞书长连接从不清理、复用模块级事件循环 —— ✅ 已修复
- **位置**：`src/wokbee/gateway/feishu.py:74-137`
- **问题**：`stop()` 仅 `loop.stop()`，不 cancel asyncio 任务也不关 WebSocket；而 `loop` 是 `lark_oapi.ws.client` 的模块级单例。重启后旧通道任务仍会恢复，导致：幽灵 / 重复消息处理、资源泄漏，以及快速重启时 "event loop already running" 被误报为连接失败（`feishu.py:113`）。
- **建议**：每个频道使用独立事件循环（`asyncio.new_event_loop()`），`stop()` 时 cancel 全部任务并关闭 WS、`loop.close()`；不复用共享单例。
- **已修复**：`_run_ws` 现在为每频道创建独立事件循环并把 SDK 的模块级 `loop` 指针重定向到该循环；`stop()` 关闭 WS（`client._disconnect()`）、取消全部任务并 `loop.close()`（新增 `_close_loop`）。彻底断开、可安全重启，消除幽灵消息 / 误报 / 资源泄漏。

### H6. 消息去重集合无界增长
- **位置**：`src/wokbee/gateway/manager.py:249-255`
- **问题**：`_in_flight` 是只 add 从不 prune 的 set，常驻内存泄漏，且随时间变慢；仅单进程内有效，重启后飞书 WS 重投无法去重。
- **建议**：存 `(message_id, monotonic_time)`，按时间窗口（如 10 分钟）剪枝或按容量淘汰最旧项。

---

## 2. 中优先级（并发 / 健壮性）

### M1. 取消后后台流线程被遗弃，共享状态无锁 —— ✅ 已修复
- **位置**：`src/wokbee/engine/runner.py:1056-1104`、`:656`
- **问题**：取消时若 `agent.stream()` 阻塞在模型 HTTP 调用中，线程被直接放弃（daemon），仍会继续写 `_run_events`、`_cache_tracker` 等；`_run_events` 是无锁 `list`，主线程并发 `list(...)` / 迭代（`:2004,1603,1800,1830,2027`）可能报 "list changed size during iteration" 或丢事件。
- **建议**：给 `_run_events` / `_cache_tracker` 加锁；取消时对线程做有界 `join` 并真正中断在途模型调用，而非直接放弃。
- **已修复**：新增 `_events_lock` 与 `_snapshot_run_events()` 辅助方法，`_emit` 的追加与所有读取 / 迭代（含 `run` / `run_chat` 的 reset）统一加锁，消除并发写读导致的崩溃与丢事件。取消后的流线程在下一个 chunk 处检查 `_cancel` 主动退出（其时长受在途模型调用上限约束，有界 `join(8)` 保留）。

### M2. 节流补丁标志与补丁动作不同步
- **位置**：`src/wokbee/engine/model_factory.py:75-81`
- **问题**：标志在锁内先置位 `True`，而 monkey-patch 在锁外执行；并发调用方可能看到标志即提前返回，构造出未经节流补丁的模型。
- **建议**：将补丁与置位整体移入 `with _THROTTLE_LOCK:` 内。

### M3. 同项目 run/chat 共享同一 `InMemorySaver` 未串行化
- **位置**：`src/wokbee/engine/runner.py`（`_CHECKPOINTERS`/`_AGENTS` 按 `project.id` 全局键控）
- **问题**：`run()` 调用 `_reset_run_state` 替换 saver 时，若同一项目正有聊天流在中途迭代该 saver，会破坏 checkpoint / 使流中断。
- **建议**：按项目串行化，或 run 与 chat 使用独立的 checkpointer 实例。

### M4. `kill_all_cancellable_runs()` 进程级误杀
- **位置**：`src/tokbee/core/subprocess_util.py:111-123`
- **问题**：取消任一执行会杀掉进程内所有在册的子进程；且 Popen 与注册之间存在取消漏掉新进程的窗口。
- **建议**：按 run id / cancel_event 限定作用域，仅杀匹配的进程；注册与创建保持原子。

### M5. `provider_store.from_dict` 裸 `int()` 可致启动崩溃
- **位置**：`src/tokbee/core/provider_store.py:50-51`、`:166`
- **问题**：`int(d.get("context_window") or 0)` 遇到非数字字符串会抛 `ValueError`，而 `:166` 的 `except` 未捕获 `ValueError`，`providers.json` 损坏时整个应用无法启动。另 `:52` 的 `enabled=bool(...)` 对字符串 `"false"` 判定为 `True`。
- **建议**：捕获 `ValueError`，并复用 `models._as_int`/`_as_bool` 的防御式解析。

### M6. 调度器线程池 / 锁 map 泄漏
- **位置**：`src/autobee/engine/scheduler.py:113-121`、`src/autobee/engine/executor.py:45-73`
- **问题**：`ThreadPoolExecutor` 在 `shutdown()` 中未调用 `.shutdown()`，非 daemon 线程退出时滞留；`_project_locks` 按 project_id 创建后从不移除，长期运行无界增长。
- **建议**：`shutdown()` 时关闭线程池并排空在途任务；锁 map 增加淘汰或复用。

### M7. 微信 poll/send 线程竞态 —— ✅ 已修复
- **位置**：`src/wokbee/gateway/wechat.py:173-174`（poll 线程写 `bot._ctx_cache`）、`:222-226`（send 线程读并阻塞 HTTP）
- **问题**：同一 `bot._ctx_cache` 与同一 `bot.client` 被两个线程无锁并发访问，send 的阻塞 HTTP 与 10s 长轮询争用连接，可能产生不一致的 context token 与 HTTP 会话竞争。
- **建议**：对 `_ctx_cache` / `bot.client` 加 `threading.Lock` 统一串行化。
- **已修复**：新增 `_ctx_lock` 守护 `bot._ctx_cache`，poll 线程写入（`:176`）与 send 线程读取（`:227`）统一加锁。核对 SDK 源码后确认 `ILinkClient.poll` / `send_text` 均为无共享连接的独立 HTTP 请求（`self.opts` 不可变、仅 `self.cursor` 在 poll 线程内自读写），故无需对 `bot.client` 全网段加锁（那会在 10s 长轮询期间阻塞发送）。

### M8. 群聊空允许列表 = 放行所有人 + 自动审批
- **位置**：`src/wokbee/gateway/feishu.py:256-297`、`src/wokbee/gateway/dispatcher.py:50-53`、`router.py:49-51`
- **问题**：飞书处理未校验是否群聊 / 是否 @机器人；空 `allow_from` 意味着任何人可触发 `AgentRunner`，且以 `skip_high_risk=True` 自动审批运行——等于群内任意成员可远程执行本机 Agent / 命令。
- **建议**：群聊要求显式 @提及，或对空名单采用「群内默认拒绝」；至少增加阻止群消息的配置开关。

---

## 3. 低优先级 / 性能（集中在 UI 线程）

### L1. 每 400ms 无条件重算整段 token 用量
- **位置**：`src/tokbee/ui/views/chat_view.py:885`、`src/wokbee/ui/workspace.py:747`
- **问题**：常驻计时器空闲时也在 UI 线程全量遍历消息估算 token，造成持续 CPU 抖动。
- **建议**：仅在消息新增 / 草稿变更 / 模型切换等事件时重算，或降频并移出 UI 线程。

### L2. 每次发送重新读盘 + base64 全部历史附件
- **位置**：`src/tokbee/ui/views/chat_view.py:1636-1661`、`:1715-1757`
- **问题**：`_on_send` 与 `_history_to_api_messages` 在 UI 线程同步读文件并 base64 编码，图片/文档多的会话每次发送明显卡顿。
- **建议**：在 worker 线程完成附件读取编码，并按附件缓存 base64。

### L3. `timeline._make_row` 每次 append O(n) 扫全气泡 → O(n²)
- **位置**：`src/wokbee/ui/timeline.py:1277-1284`
- **问题**：每次追加事件都遍历 `_bubbles` 清理已销毁引用，长运行下逐次变慢。
- **建议**：改为渲染/清空时一次性剪枝。

### L4. 侧栏搜索无防抖，每按键重建所有会话项
- **位置**：`src/tokbee/ui/views/chat_view.py:620`、`src/wokbee/ui/workspace.py:372`
- **问题**：`textChanged` 直接触发 `refresh()` 重建所有项，输入每字符都全量重建。
- **建议**：加防抖计时器。

### L5. Worker 对象从不 `deleteLater` → 内存累积
- **位置**：`src/wokbee/ui/workspace.py`（AgentWorker / LessonWorker / _CompactWorker / _RefineMetaWorker）、`src/tokbee/ui/views/chat_view.py`（_AIChatWorker / _AiNameWorker / _CompactWorker）、`src/tokbee/ui/views/provider_view.py:1030`
- **问题**：QThread 作为子对象常驻，只把字段置 `None`，每次运行都累积；应用关闭时可能 "QThread: Destroyed while thread is still running"。
- **建议**：所有 worker 的 `finished` 连接 `worker.deleteLater`。

### L6. `QThread.wait()` 阻塞 UI 线程
- **位置**：`src/tokbee/ui/views/chat_view.py:1136`、`src/wokbee/ui/workspace.py:1661-1666`
- **问题**：主线程同步等待 worker，网络调用不可取消时 UI 冻结可达数秒（退出时最多 ~20s）。
- **建议**：改为基于 `finished` 信号的异步等待，或并行/缩短等待。

### L7. `runner` 每轮整文件读 `events.jsonl` + 全量 `get_state`
- **位置**：`src/wokbee/engine/runner.py:1443-1451`（`_recent_events_digest`）及多处全量消息扫描
- **问题**：每次聊天轮次从磁盘读整个事件文件，随运行增长为 O(n)；`get_state` 亦逐轮全量遍历。
- **建议**：从文件尾部读最后 N 条；缓存已见消息索引，只扫描新增。

### L8. 其它
- `runner.py` 事件热路径对每个事件同步脱敏 + 深拷贝（`:650-658`）→ 改为写时间线时脱敏 / 减少拷贝。
- `src/wokbee/ui/gateway_workspace.py:564-572`：微信 worker 是 QObject 非 QThread，跳过 `deleteLater` 分支导致从不释放；`_set_qr_state_connecting` 替换计时器前未停止旧的。
- `src/autobee/engine/wecom.py`：无 45009 限流重试，每次推送新建连接 → 可复用客户端 + 退避重试。
- `src/tokbee/core/file_reader.py`：读取文件无大小上限，超大附件会内存飙升。
- `src/tokbee/core/ai_client.py:138-158,276-364`：取消时 daemon 线程与流式生成器在提前退出时泄漏 socket/线程 → 需保证 `gen.close()` 与请求取消。
- `src/wokbee/gateway/wechat.py:157-163`：`_cursor_file` 死代码（SDK 无此属性，恒为 None）。
- `src/autobee/engine/scheduler.py:234-245`：进度每 tick 全量重写 `autobee.json` → 限频/合并落盘。
- `src/autobee/core/store.py:66-76`：首次排序为死代码。
- `src/autobee/engine/nl_builder.py`：Cron 未强制 5 字段，6/7 字段输出 UI 无法描述。

---

## 4. 建议的修复路线图

| 阶段 | 内容 | 涉及文件 | 状态 |
|------|------|----------|------|
| 1（高） | 脚本超时改用 `run_cancellable` | `executor.py`、`script_runner.py` | ✅ 已完成 |
| 1（高） | 重命名遮蔽的 `finished` 信号 + worker `deleteLater` | `chat_view.py`、`workspace.py` | 🕒 信号重命名已完成 / worker `deleteLater` 待处理 |
| 1（高） | API Key 加密落盘（DPAPI/keyring） | `provider_store.py`、`credential_crypto.py` | ✅ 已完成 |
| 2（高） | 429 重试计入上限 + provider 解析容错 | `ai_client.py`、`provider_store.py` | 🕒 429 已完成 / 解析容错待处理 |
| 2（高） | 飞书连接生命周期 + `_in_flight` 剪枝 | `feishu.py`、`manager.py` | 🕒 连接生命周期已完成 / `_in_flight` 剪枝待处理 |
| 3（中） | `_run_events` 加锁、不遗弃流线程、节流补丁原子化 | `runner.py`、`model_factory.py` | 🕒 `_run_events` 加锁已完成 / 节流补丁原子化待处理 |
| 3（中） | 同项目 run/chat 串行化、`kill_all` 按 run 限定 | `runner.py`、`subprocess_util.py` | ⏳ 待处理 |
| 3（中） | 调度器线程池/锁 map 释放、微信线程加锁、群聊名单限制 | `scheduler.py`、`executor.py`、`wechat.py`、`feishu.py` | 🕒 微信线程加锁已完成 / 其余待处理 |
| 4（性能） | 移除/降频 UI 常驻计算、附件重读、气泡剪枝、侧栏防抖 | `chat_view.py`、`workspace.py`、`timeline.py` | ⏳ 待处理 |

---

## 5. 附录：已核查排除的误报

以下项经逐条核对确认**非真实 Bug**，保留在文档中以免后续误改：

- `workspace.py:697` 的 `LessonWorker` 未定义 —— 文件含 `from __future__ import annotations`（`:8`），仅为字符串注解，不触发运行时 NameError。
- AST「未定义名」扫描的 `r`/`p`/`x`/`t`/`_k`/`msg`/`sid`/`f`/`_sid`/`btn`/`v` 等 —— 均为 lambda 形参（分析脚本未收集 lambda 参数，属误报）。
- 「重复函数定义」的大量结果 —— 多为同一文件内多个类复用的方法名（如 `__init__`、`get`/`set`/`to_dict`），正常现象。
- 无裸 `except` 语句；`compileall` 全量通过。
- `ai_throttle.py`、`gateway/base.py`、`router.py`、`provision.py`、`dispatcher.py` 未发现真实缺陷。
