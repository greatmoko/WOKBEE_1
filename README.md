# WokBee Scaffold（精简脚手架）

从完整 WokBee 项目剥离后的干净起点，只保留：

| 模块 | 说明 |
|------|------|
| **WokBee 对话** | 多会话聊天，兼容 OpenAI 风格 API；支持上下文预算与摘要压缩 |
| **AI配置** | AI 角色、厂商设置（Chatbox 风格：Key / Host / 模型列表 / 每模型上下文窗口）、对话默认设置 |
| **设置** | 通用设置（主题等） |

**已剔除**：AutoBee、知识库、KB 问答、应用页，以及对应配置项与依赖。

> 本目录位于原仓库的 `scaffold/`，**不替代**原项目。复制本文件夹即可另起新项目。

## 快速开始

```bash
# 1. 复制整个 scaffold 目录到新位置并重命名（可选）
# 2. 进入目录
cd your-new-project

# 建议使用虚拟环境
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS / Linux

pip install -r requirements.txt
# 或：pip install -e .

python main.py
# 或：python -m wokbee
```

首次启动后，在「AI配置 → 厂商设置」中填写 API Key、确认 Host，启用模型并核对各模型的**上下文窗口**；对话页参数（Temperature、压缩比例等）按会话独立配置，新建会话默认值在「对话默认设置」。

## 上下文管理

对齐 Chatbox 思路，并在对话输入区旁提供 Cursor 风格的**用量圆环**：

| 能力 | 说明 |
|------|------|
| 每模型上下文窗口 | 厂商设置中可为每个模型编辑 `context_window`（tokens） |
| Token 预算 | 发送前按窗口预留输出空间，从旧消息裁剪；消息条数为 soft limit |
| 自动压缩 | 用量超过「可用窗口 × 压缩触发比例」时，先摘要旧对话再发送 |
| 手动压缩 | 点击用量圆环，立即压缩当前会话上下文 |
| 压缩比例 | 在「对话默认设置」/ 单会话参数中配置（默认 60%） |

界面仍保留完整历史；压缩只影响发给模型的 payload（摘要检查点 + 近期原文）。

## 目录结构

```
scaffold/
├── main.py
├── pyproject.toml
├── requirements.txt
├── pyrightconfig.json
└── src/wokbee/
    ├── app.py
    ├── core/           # 配置、AI 客户端、上下文管理、聊天等
    ├── ui/             # 主窗口、对话 / AI配置 / 设置
    ├── utils/
    └── resources/
```

## 本地数据

默认写入用户目录 `~/.wokbee/`（配置、模型 Key、聊天记录、日志等），不随代码分发。

若多个基于本脚手架的项目共用同一台机器，注意它们会共享该目录；需要隔离时可自行改 `Config` 中的路径。

## 环境要求

- Windows 10 / 11（主目标平台；其它系统未专门验证）
- Python 3.10+
