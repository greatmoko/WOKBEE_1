# WokBee

Windows 桌面端 AI 助手（当前版本 **v0.1.0**）。

产品名是 **WokBee**；应用内有三块核心能力：

| 品牌 | 做什么 |
|------|--------|
| **TokBee** | 多会话聊天：选模型、设角色、管上下文 |
| **WokBee** | 按「项目」跑 Agent：读写文件、联网、脚本、经验沉淀、Skills / MCP |
| **AutoBee** | 定时任务：自然语言生成调度、立即运行、企业微信 Webhook 推送、运行历史 |

左侧一级导航：**TokBee → WokBee → AutoBee → AI配置 → 设置**。

---

## 快速开始

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt
# 或：pip install -e .

python main.py
# 或：python -m tokbee
```

### 首次使用建议

1. 打开 **AI配置 → 厂商设置**，添加厂商、填写 API Key / Host  
2. 启用要用的模型，核对各模型的**上下文窗口**  
3. 在模型旁点 **「默认」**（新建对话 / 新建项目会优先用它）  
4. 按需配置 **TokBee 设置**（对话默认参数）、**WokBee 设置**（默认模型、执行上限、调用节流等）  
5. 需要定时跑任务时，到 **AutoBee** 新建任务并保存；可选填企业微信 Webhook

---

## AI配置里有什么

| 页签 | 作用 |
|------|------|
| AI 角色 | 系统提示词角色库，对话可选用 |
| 厂商设置 | OpenAI 兼容 API：Key、Host、模型列表、默认模型 |
| TokBee 设置 | 新建对话时的默认参数（温度、角色等） |
| WokBee 设置 | 默认模型、最大步数 / 并行工具 / 管线阶段、AI 调用节流、DeepSeek 搜索开关等 |
| Skills | 全局技能包（文件夹 + `SKILL.md`），运行时只读挂载 |
| MCP | 外挂 MCP 服务器，给 Agent 加工具 |

---

## AutoBee 简介

- **任务类型**：纯文本 / 脚本 / WokBee 项目任务  
- **调度**：自然语言描述 → AI 生成 Cron；支持立即运行  
- **推送**：填写企业微信机器人 Webhook 后，运行结果自动推送  
- **历史**：每个任务保留最近 10 条运行记录，点击可看详情  

数据文件：`~/.wokbee/autobee.json`

---

## 目录结构

```
├── main.py                 # 启动入口
├── pyproject.toml
├── requirements.txt
└── src/
    ├── tokbee/             # 应用壳 + TokBee 对话 + 全局 UI 样式
    │   └── ui/styles/      # theme.py（配色）+ system.py（默认控件样式）
    ├── wokbee/             # WokBee 项目 Agent 工作流
    └── autobee/            # AutoBee 定时任务（模型 / 调度 / 执行 / UI）
```

### 界面样式约定（开发）

新增界面默认使用系统样式，从 `tokbee.ui.styles.system` 引入 `apply_*` / `*_qss`（如表单下拉 `apply_form_combo`、主按钮 `apply_primary_btn`、勾选框 `apply_checkbox`）。  
仅当产品明确要求自定义外观时，才写局部 QSS。旧代码可继续 `from tokbee.ui.combo_style import ...`（内部已转发到 `system`）。

---

## 本地数据

| 路径 | 内容 |
|------|------|
| `~/.wokbee/` | 配置、厂商、角色、对话默认、Skills、日志等 |
| `~/.wokbee/autobee.json` | AutoBee 定时任务与运行日志 |
| `~/WokBeeWorkspace/` | WokBee 各项目目录（可在「WokBee 设置」修改） |

若本机仍有旧目录 `~/.tokbee/`，启动时会尽量迁移到 `~/.wokbee/`。

Skills 默认目录：`~/.wokbee/skills`（可在 Skills 页改路径）。

---

## 环境要求

- Windows 10 / 11  
- Python 3.11+  
- 可访问的 OpenAI 兼容 API（官方 / 中转 / 本地均可）

主要依赖：PySide6（界面）、APScheduler（定时）、Deep Agents / LangGraph（Agent）、httpx、文档解析（PDF/Office）等，详见 `requirements.txt`。

---

## 版本

当前：`0.1.0`（见 `pyproject.toml` / 窗口标题）
