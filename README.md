# WokBee

Windows 桌面端 AI 助手（当前版本 **v0.1.0**）。

产品名是 **WokBee**；应用内有两块核心能力：

| 品牌 | 做什么 |
|------|--------|
| **TokBee** | 多会话聊天：选模型、设角色、管上下文 |
| **WokBee** | 按「项目」跑 Agent：读写文件、联网、脚本、经验沉淀、Skills / MCP |

左侧一级导航：**TokBee → WokBee → AI配置 → 设置**。

更偏产品视角的说明见：[docs/产品技术方案.md](docs/产品技术方案.md)

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
4. 按需配置 **TokBee 设置**（对话默认参数）、**WokBee 设置**（工作区、审核策略等）

---

## AI配置里有什么

| 页签 | 作用 |
|------|------|
| AI 角色 | 系统提示词角色库，对话可选用 |
| 厂商设置 | OpenAI 兼容 API：Key、Host、模型列表、默认模型 |
| TokBee 设置 | 新建对话时的默认参数（温度、角色等） |
| WokBee 设置 | 项目工作区根目录、默认审核策略、步数上限等 |
| Skills | 全局技能包（文件夹 + `SKILL.md`），运行时只读挂载 |
| MCP | 外挂 MCP 服务器，给 Agent 加工具 |

---

## 目录结构

```
├── main.py                 # 启动入口
├── pyproject.toml
├── requirements.txt
├── docs/
│   └── 产品技术方案.md     # 产品经理可读的技术方案
└── src/
    ├── tokbee/             # 应用壳 + TokBee 对话
    └── wokbee/             # WokBee 项目 Agent 工作流
```

---

## 本地数据

| 路径 | 内容 |
|------|------|
| `~/.wokbee/` | 配置、厂商、角色、对话默认、Skills、日志等 |
| `~/WokBeeWorkspace/` | WokBee 各项目目录（可在「WokBee 设置」修改） |

若本机仍有旧目录 `~/.tokbee/`，启动时会尽量迁移到 `~/.wokbee/`。

Skills 默认目录：`~/.wokbee/skills`（可在 Skills 页改路径）。

---

## 环境要求

- Windows 10 / 11  
- Python 3.11+  
- 可访问的 OpenAI 兼容 API（官方 / 中转 / 本地均可）

主要依赖：PySide6（界面）、Deep Agents / LangGraph（Agent）、httpx、文档解析（PDF/Office）等，详见 `requirements.txt`。

---

## 版本

当前：`0.1.0`（见 `pyproject.toml` / 窗口标题）
