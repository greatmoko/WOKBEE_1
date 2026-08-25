# Reasonix 缓存方案调研 → WokBee 借鉴清单

> 日期：2026-08-25  
> 项目：[esengine/DeepSeek-Reasonix](https://github.com/esengine/DeepSeek-Reasonix)（MIT，DeepSeek-native coding agent）  
> 相关：`artifacts/prompt-cache-deepseek.md`

## 一句话

Reasonix **不是「打开了 DeepSeek 缓存」**，而是把 Agent Loop 做成 DeepSeek 前缀缓存喜欢的形状：**不可变前缀 + 只追加历史 + 易变草稿不上行 + 受控折叠 + 命中率可观测**。实测长会话可到 **90%–99%+** hit。

## 已核实（官方/仓库）

### Pillar 1 — Cache-First Loop

来源：[docs/ARCHITECTURE.md](https://github.com/esengine/DeepSeek-Reasonix/blob/main/docs/ARCHITECTURE.md)

上下文三区：

| 区 | 内容 | 规则 |
| --- | --- | --- |
| **Immutable Prefix** | system + tool_specs + few-shots | 会话开始算一次、hash 钉死，整场不变 |
| **Append-Only Log** | assistant / tool / user 历史 | 只追加；禁止重排、就地改、随意删中间 |
| **Volatile Scratch** | R1 thought、临时 plan | **永不作为上行 API 前缀**；要进历史须先蒸馏 |

不变量：
1. Prefix 一次计算、哈希锁定  
2. Log 序列化顺序固定  
3. Scratch 经蒸馏后才可 fold 进 log  

指标：`prompt_cache_hit_tokens / (hit + miss)`，**单轮 + 会话累计**都展示（避免用冷启动拖垮的 avg 误判「没生效」）。

并行工具：可读工具可并行，但 **tool result 仍按声明顺序写入历史**，保证字节形状稳定。

### 配套机制（成本/缓存）

来源：[benchmarks/real-world-cache](https://github.com/esengine/DeepSeek-Reasonix/blob/main/benchmarks/real-world-cache/README.md)

1. **Turn-end 裁剪大 tool result**（如 >3000 tokens 缩成摘要，需要再 read）——减「脏尾巴」拖累后续 miss，同时避免无序压缩破坏前缀。  
2. **Auto-compact**：逼近窗口时折叠；summary 请求尽量复用已缓存的 system/tools/history；折叠后主请求对 **新摘要段** 付一次 cold miss（可接受）。  
3. **前缀诊断**：miss 时归因 system 漂移 / tools 漂移 / log 改写(compaction) / 固有 tail miss。  
4. **子 Agent / planner 独立会话**：不把第二模型插进同一条消息链，避免互毁前缀。  
5. **记忆写入不立刻改 system**：磁盘更新，本会话用尾部 note；下会话再进 prefix（`WriteDoc` 注释语义）。

真实案例（2026-05-01）：输入 hit 4.35 亿 / miss 76.8 万 → **99.82%**；相对 0% cache 约省 **97%+** 费用。

哲学原话：*Cache stability isn't a feature you turn on; it's an invariant the loop is designed around.*

## 推断：对照 WokBee 现状

| Reasonix 做法 | WokBee 现状（`runner.py` 等） | 差距 |
| --- | --- | --- |
| Immutable system | 每 run 拼 title/goal/approval/`max_steps`/经验 digest 进 system | **高**：跨 run / 改目标即前缀变 |
| Frozen tool_specs | MCP 动态加载；顺序未强制排序 | **中高** |
| Append-only | LangGraph messages 大体追加；但 compact/slice、重建 agent 可能改写 | **中**（需审计 compaction） |
| VolatileScratch 不上行 | 无独立 scratch；推理若进 messages 会占前缀增长（通常仍可命中「旧前缀」） | **低–中** |
| hit/miss 双指标 UI | 未解析 DeepSeek `prompt_cache_*` | **高（可观测性为零）** |
| 大 tool 结果裁剪 | 工具回调整段进历史 | **中**（长跑费用杀手） |
| 独立子会话 | 同 checkpointer/同 thread 长链 | 按产品取舍 |

## 建议借鉴优先级（DeepSeek + WokBee）

### P0 — 立刻可做、收益最大

1. **ImmutablePrefix**：system 只留静态人格/禁令/工具用法；项目名、目标、审核、`max_steps`、经验摘要改为 **首条 user 或独立动态消息**（会话内也不再改 system）。  
2. **钉死 tools**：MCP/自定义工具按 `name` 稳定排序；一次「运行」内不增删工具集。  
3. **观测**：从 API usage 读 `prompt_cache_hit_tokens` / `miss`，时间线或状态栏显示 `cache 本轮% · 会话avg%`（照搬 Reasonix 双指标）。

### P1 — 长任务控费

4. **Tool result 上限**：单条 callback 超 N token 落盘 + 摘要进 messages（Reasonix turn-end cap）。  
5. **受控 compact**：折叠时保留「钉住的前缀」（system + 首条任务 user），只摘要中间段；禁止无序重排。  
6. **前缀漂移诊断**：对 system/tools 做 hash，miss 时打日志归因。

### P2 — 可选

7. 子任务/总结模型用独立短会话（flash），主会话前缀不被污染。  
8. 经验记忆：写盘不改本会话 system；需要时 append 一条 user note。

## 明确不要照搬的

- 「只支持 DeepSeek」产品锁定（WokBee 多厂商；但 **主力 DeepSeek 时启用 cache-first 路径** 即可）。  
- 整套 Go TUI / 并行工具调度可后做。  
- 不要误以为要接 Anthropic `cache_control`——Reasonix 证明 DeepSeek 只需 **形状正确**。

## 参考链接

- 仓库：https://github.com/esengine/DeepSeek-Reasonix  
- 架构：https://github.com/esengine/DeepSeek-Reasonix/blob/main/docs/ARCHITECTURE.md  
- 真实缓存案例：https://github.com/esengine/DeepSeek-Reasonix/blob/main/benchmarks/real-world-cache/README.md  
- 设计解读：https://segmentfault.com/a/1190000047798403  
- 前缀诊断 PR：https://github.com/esengine/DeepSeek-Reasonix/pull/3057  
