# Agent 缓存命中率调研简报（DeepSeek 主力）

> 日期：2026-08-25  
> 范围：WokBee Agent（`create_deep_agent` + OpenAI 兼容 `ChatOpenAI`）在 DeepSeek 上的上下文缓存

## 结论（一句话）

DeepSeek **默认开启硬盘上下文缓存**，无需 Anthropic 式 `cache_control`；低命中几乎都是 **前缀被每轮改写**，不是「没开缓存」。

## 已核实事实

### DeepSeek 机制（官方文档）

来源：[上下文硬盘缓存](https://api-docs.deepseek.com/zh-cn/guides/kv_cache)

| 点 | 说明 |
| --- | --- |
| 开关 | 全员默认开启，无需改接口 |
| 命中条件 | 从第 0 个 token 起的 **前缀** 完全匹配已落盘的「缓存前缀单元」 |
| 计费观测 | `usage.prompt_cache_hit_tokens` / `prompt_cache_miss_tokens` |
| 粒度 | 约 64 tokens 为单位；更短内容通常不缓存 |
| 保证 | 「尽力而为」，不保证 100% |
| TTL | 停用后数小时到数天自动清 |

近期规则强调：缓存前缀是 **独立完整单元**；第二轮若前缀分叉（`A+B` → `A+C`），可能要等到系统抽出公共前缀 `A` 后，第三轮 `A+D` 才能命中。

### 开源 / 业界共识（跨厂商，同样适用 DeepSeek）

- 缓存靠 **稳定前缀**；动态内容（时间戳、会话 ID、RAG、经验摘要）塞进 system/tools 前端 → 命中率崩盘（常见从 ~80% 掉到个位数）。
- 论文 [Don’t Break the Cache (arXiv:2601.06007)](https://arxiv.org/html/2601.06007v2)：agent 场景策略性缓存可省 **41–80%** 费用；盲目全量缓存有时反而增 TTFT。
- Anthropic/Claude 侧案例：动态内容挪到消息尾部后，命中率可从约 7% 提到 80%+（机制不同，**排序原则相同**）。
- LangChain：`ChatOpenAI` 对 OpenAI 有 `prompt_cache_key`；**DeepSeek 不依赖该参数**，但仍建议固定同一 `base_url`/模型，并自己打日志读 DeepSeek 的 hit/miss 字段。

### 与 Anthropic / OpenAI 的差异（选方案时）

| 厂商 | 控制方式 | 对你（DeepSeek） |
| --- | --- | --- |
| Anthropic | 显式 `cache_control` 断点 | **不适用**（除非换 Claude） |
| OpenAI | 自动前缀 + 可选 `prompt_cache_key` | 部分中转兼容，非 DeepSeek 本体 |
| DeepSeek | 自动硬盘缓存 | **主力路径：只改 prompt 组装，不改 API 形态** |

## 推断（结合当前 WokBee 代码）

以下针对 `src/wokbee/engine/runner.py` 中 `system_prompt` 组装，属**代码对照推断**，非线上实测。

| 风险点 | 为何伤 DeepSeek 命中 | 建议 |
| --- | --- | --- |
| system 内嵌 `项目名称/目标`、`审核策略`、`max_steps` | 改目标/审核即整段 system 变，跨轮/跨 run 前缀断裂 | 静态人格与规则放 system；项目态放 **首条 user / 独立消息** |
| `experience_digest` 直接拼进 system | 每次总结经验后 system 字节级变化，工具定义之后的整段历史更难复用 | 经验改为 messages 尾部或「只读 memory 文件」由模型按需读，**不要进缓存前缀** |
| MCP 工具列表顺序不稳定 | tools 通常排在请求最前，顺序抖动 = 全前缀失效 | 工具名 **稳定排序**；同一次 run 内勿增删工具集 |
| Skills / 子 Agent 动态改 tools | 同理 | 固定工具面；模式用消息传，不换 tool schema |
| 未读取 `prompt_cache_hit_tokens` | 无法判断是「前缀坏了」还是「冷启动」 | 在 usage 回调里打 hit/(hit+miss) 比率 |

**同一轮 Agent 多步（消息只追加、system/tools 不变）**：DeepSeek 官方多轮示例下，后轮应能命中前轮前缀——若 **同一次「运行」内** 命中仍极低，优先查 tools 序列化与中间件是否每步改写 system。

**跨次「运行」/跨项目**：system 含项目态 + 经验时，冷启动与前缀分叉是预期现象；优化目标应是「单次长任务内 hit 率」与「同项目短间隔重跑的公共前缀」。

## 推荐落地顺序（DeepSeek）

1. **观测**：解析 DeepSeek `prompt_cache_hit_tokens` / `miss`，在时间线或日志输出命中率（没有数就无法优化）。
2. **拆 system**：固定指令（人格、禁 archives、工具用法）与可变态（目标、经验、审核）分离；可变态移出前缀。
3. **冻 tools**：MCP/自定义工具按 name 排序；一次 run 内工具集不变。
4. **经验**：`prompt_digest` 改为尾部注入或文件工具读取，避免污染 system。
5. （可选）若走 Claude：再考虑 LangChain Anthropic caching middleware；**DeepSeek 无需此步**。

## 参考链接

- DeepSeek 官方：[上下文硬盘缓存](https://api-docs.deepseek.com/zh-cn/guides/kv_cache)
- DeepSeek 新闻：[硬盘缓存降价说明](https://www.deepseek.com/news/context-caching/)
- 论文：[Don’t Break the Cache](https://arxiv.org/html/2601.06007v2)
- 实践文：[Prompt assembly breaks caching](https://dev.to/parag_d/prompt-caching-works-your-prompt-assembly-code-does-not-5edc)
- LangChain OpenAI cache 参数（对照用）：[ChatOpenAI 文档](https://reference.langchain.com/python/langchain-openai/langchain_openai/chat_models/base/ChatOpenAI)
