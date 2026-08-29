# WokBee

[![GitHub](https://img.shields.io/badge/GitHub-greatmoko%2FWOKBEE__1-blue?logo=github)](https://github.com/greatmoko/WOKBEE_1)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

**WokBee** 是一个 Windows 桌面 AI 助手（当前版本 **v0.2.0**），已在 [GitHub 开源](https://github.com/greatmoko/WOKBEE_1)。  
在同一套界面中集成三类能力：**日常对话（TokBee）**、**项目级 Agent 自动化（WokBee）**、**定时任务调度（AutoBee）**，统一对接 OpenAI 兼容 API。

### 设计思想

与普通「一次性对话式 Agent」不同，WokBee 围绕 **经验沉淀、脚本固化、按项目反复运行** 来设计——越用越省 Token，对模型能力的要求也越低：

| 机制 | 说明 |
|------|------|
| **总结经验（Lesson）** | 任务结束后自动提炼操作路径与踩坑记录；下次同类项目直接复用，不必让 AI 从零摸索 |
| **脚本固化** | 成功的联网请求、命令执行等步骤可写入 `scripts/`，后续**本地重跑、零 Token** |
| **基于项目的可重复运行** | 每个项目有独立工作区与管线；同一目标反复执行时，优先走已有脚本与经验，而非每次重新推理 |
| **降低 Token 用量** | 首轮由 AI 探索并消耗 Token，之后逐步改走固化脚本与经验摘要，API 成本显著下降 |
| **降低模型门槛** | 有经验指引、有脚本兜底，中等能力模型也能稳定完成重复性操作，不必每次都上最强模型 |

> 一句话：**第一次让 AI「探路并记下来」，之后主要靠本机脚本和经验自己跑。**

本项目已在 GitHub 开源，上述机制的实现方式、Prompt、审批策略等均可 Fork 后按你的实际需求自行修改。

---

## ⚠️ 重要声明

> **请在使用前仔细阅读以下内容。**

1. **个人作品，非官方软件**  
   本项目由作者个人 **vibe coding 手搓** 完成，已在 GitHub 开源分享，**不代表任何公司或组织的官方产品**，也**未经 IT / 安全 / 合规部门审核或背书**。如需在办公环境使用，请自行评估风险并遵守相关规定。

2. **不提供作者技术支持**  
   作者**不回答任何技术问题**（安装、配置、Bug、使用咨询等）。请优先阅读本 README；也可在 GitHub 上提 Issue / PR，但不保证回复。

3. **数据与安全责任自负**  
   - API Key、对话内容、项目文件均保存在**本机**；发送至 AI 厂商的内容取决于你所选 API。  
   - WokBee Agent 可在本机执行命令（`execute`），使用前请理解审批策略。  
   - 使用第三方 AI 接口时，**请勿将涉密资料**粘贴进对话或 Agent 目标。

4. **按现状提供（AS IS）**  
   软件可能存在 Bug、不稳定或未覆盖的边界情况，使用后果由使用者自行承担。

---

## 获取与安装

### 克隆仓库

```powershell
git clone https://github.com/greatmoko/WOKBEE_1.git
cd WOKBEE_1
```

也可在 GitHub 页面点击 **Code → Download ZIP** 下载源码包后解压。

解压/克隆后的目录结构示例（**路径尽量不含中文或空格**）：

```
WOKBEE_1/
├── main.py
├── README.md
├── requirements.txt
└── src/
```

> 本项目**仅提供源码**，不提供预编译 exe。使用前需自行安装 Python 3.11+。

---

## 环境要求

| 项目 | 要求 |
|------|------|
| 操作系统 | Windows 10 / 11（64 位） |
| Python | **3.11 或更高**（推荐 3.11 / 3.12） |
| 网络 | 可访问所配置的 AI API |
| 磁盘 | 建议预留 500 MB 以上 |

主要依赖：PySide6、APScheduler、Deep Agents / LangGraph、httpx、PDF/Office 文档解析等，完整列表见 `requirements.txt`。

---

## 安装与启动

### 1. 进入项目目录

若已 `git clone`，直接进入 `WOKBEE_1`；若下载 ZIP，解压后进入对应目录。

### 2. 创建虚拟环境（推荐）

```powershell
cd WOKBEE_1
python -m venv .venv
.venv\Scripts\activate
```

激活成功后，命令行前会出现 `(.venv)` 前缀。

> 若 `python` 命令不可用，请安装 [Python 3.11+](https://www.python.org/downloads/)，安装时勾选 **Add Python to PATH**。

### 3. 安装依赖

```powershell
pip install -r requirements.txt
```

国内网络较慢时，可临时使用镜像：

```powershell
pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```

### 4. 启动应用

```powershell
python main.py
```

启动成功后，窗口标题显示 **WokBee v0.2.0**。

### 5. 日常使用快捷方式（可选）

可将以下内容保存为 `启动WokBee.bat`，放在桌面双击启动：

```bat
@echo off
cd /d C:\path\to\WOKBEE_1
call .venv\Scripts\activate
python main.py
```

---

## 首次配置（必做）

安装完成后，**必须先配置 AI 厂商**，否则对话与 Agent 无法调用模型。

| 步骤 | 操作 | 说明 |
|------|------|------|
| 1 | 左侧导航 → **AIConfig** → **厂商设置** | 添加厂商、填写 **API Key** 与 **Host** |
| 2 | 拉取或手动添加模型 | 启用要用的模型，核对**上下文窗口**大小 |
| 3 | 设置默认模型 | 在模型旁点击 **「默认」**，新建对话/项目会优先使用 |
| 4 | **TokBee 设置** | 配置新建对话的默认参数（温度、角色等） |
| 5 | **WokBee 设置** | 配置默认模型、执行步数上限、审批策略等 |
| 6 | （可选）**Skills / MCP** | 挂载全局技能包或外挂 MCP 工具服务器 |
| 7 | （可选）**AutoBee** | 需要定时任务时，新建任务并保存；可填企业微信 Webhook |

内置支持的厂商类型包括 OpenAI、Google Gemini、DeepSeek、智谱 GLM、通义千问、Kimi 等 OpenAI 兼容接口；也支持自定义 Host 的中转服务。

---

## 核心功能

应用左侧一级导航：**TokBee → WokBee → AutoBee → AIConfig → Settings**。

### TokBee — 多会话 AI 对话

面向日常问答、文档解读、头脑风暴等轻量场景。

- **多会话管理**：左侧对话列表，支持新建、重命名、删除
- **模型与角色**：每条对话可选不同模型；可绑定 AI 角色（系统提示词）
- **附件支持**：图片、PDF、Word、Excel、PPT 等文件拖入或选择上传
- **上下文可视化**：环形指示器展示当前上下文用量
- **流式输出**：实时显示 AI 回复，支持 Markdown 渲染

---

### WokBee — 项目级 AI Agent

面向需要**持续执行、读写文件、跑脚本、产出交付物**的复杂任务。

每个「项目」有独立工作区，Agent 在沙箱目录内运行，支持人机协作审批。

**Agent 能力**

| 能力 | 说明 |
|------|------|
| 文件读写 | 在项目沙箱内读、写、搜索文件；产出物写入 `deliverables/` |
| 联网检索 | `web_search`、`http_get`、`http_request`；DeepSeek 官方搜索（可开关） |
| 本机命令 | `execute` 在 Windows 上经 pwsh 执行 |
| 脚本固化 | 成功的步骤可固化为 `scripts/` 本地脚本，下次免 Token 重跑 |
| 经验沉淀 | 任务结束后自动生成 Lesson，供后续项目参考 |
| Skills / MCP | 挂载技能包或外挂 MCP 工具 |
| 人机协作 | 写文件、执行命令等可按风险等级要求人工审批 |

**默认工作区路径**：`~/WokBeeWorkspace/`（可在 WokBee 设置中修改）

---

### AutoBee — 定时任务调度

| 功能 | 说明 |
|------|------|
| 任务类型 | **文本**、**脚本**（Python / JS）、**WokBee 项目** |
| 自然语言调度 | 用中文描述执行频率，AI 自动生成 Cron 表达式 |
| 立即运行 | 手动触发一次，不必等到计划时间 |
| 企业微信推送 | 填写机器人 Webhook，运行结果自动推送 |
| 运行历史 | 每个任务保留最近 10 条记录 |

---

### AIConfig — 全局 AI 与扩展配置

| 页签 | 作用 |
|------|------|
| **AI 角色** | 系统提示词角色库 |
| **厂商设置** | API Key、Host、模型列表、默认模型 |
| **TokBee 设置** | 新建对话默认参数 |
| **WokBee 设置** | 默认模型、步数上限、审批策略、节流等 |
| **Skills** | 全局技能包目录 |
| **MCP** | 外挂 MCP 服务器 |

---

## 本地数据

| 路径 | 内容 |
|------|------|
| `~/.wokbee/` | 全局配置、厂商 Key、角色、对话记录、Skills、日志等 |
| `~/.wokbee/autobee.json` | AutoBee 定时任务与运行日志 |
| `~/WokBeeWorkspace/` | WokBee 各项目目录 |

---

## 常见问题

**Q：启动报错 `No module named 'PySide6'`**  
A：未安装依赖或未激活虚拟环境。执行 `.venv\Scripts\activate` 后重新 `pip install -r requirements.txt`。

**Q：对话一直提示无可用模型**  
A：到 AIConfig → 厂商设置，确认已添加厂商、填写 Key、启用至少一个模型并设为默认。

**Q：Agent 卡在「待审批」**  
A：时间线中有待确认操作，在操作栏或弹窗中批准/拒绝。

**Q：定时任务没有执行**  
A：确认应用处于运行状态（AutoBee 调度器随主程序启动）。

**Q：如何升级版本**  
A：`git pull` 拉取最新代码（或重新下载 ZIP），在虚拟环境中执行 `pip install -r requirements.txt`；`~/.wokbee/` 配置一般可保留。

**Q：想改功能或修 Bug 怎么办**  
A：欢迎 Fork 仓库自行修改，或通过 GitHub Issue / Pull Request 参与；作者不保证处理时效。

---

## 目录结构

```
├── main.py                 # 启动入口
├── pyproject.toml
├── requirements.txt
├── scripts/
│   └── make_release.ps1    # 打 src zip 发布包（可选）
└── src/
    ├── tokbee/             # 应用壳 + TokBee 对话 + UI 样式
    ├── wokbee/             # WokBee 项目 Agent 工作流
    └── autobee/            # AutoBee 定时任务
```

---

## 版本

当前版本：**0.2.0**

---

## 开源与许可

- **仓库**：https://github.com/greatmoko/WOKBEE_1  
- **许可**：[MIT License](LICENSE) — 可自由使用、修改与分发，需保留版权声明。  
- **贡献**：可通过 GitHub Issue 反馈问题、Pull Request 提交改动；作者不提供一对一技术支持。

本项目为作者个人作品，按现状（AS IS）分享。**不提供任何形式的担保。**
