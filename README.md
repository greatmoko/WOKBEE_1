# WokBee Scaffold（精简脚手架）

从完整 WokBee 项目剥离后的干净起点，只保留：

| 模块 | 说明 |
|------|------|
| **WokBee 对话** | 多会话聊天，兼容 OpenAI 风格 API |
| **AI配置** | AI 角色、厂商设置（Chatbox 风格：Key / Host / 模型列表） |
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

首次启动后，在「AI配置 → 厂商设置」中填写 API Key、确认 Host，并启用模型；对话页参数（Temperature 等）按会话独立配置。

## 目录结构

```
scaffold/
├── main.py
├── pyproject.toml
├── requirements.txt
├── pyrightconfig.json
└── src/wokbee/
    ├── app.py
    ├── core/           # 配置、AI 客户端、聊天等
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
