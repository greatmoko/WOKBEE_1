"""跨项目 Agent 记忆：记忆概述 + 单一记忆库（SQLite）。

- 记忆概述：全局单个 MD（`~/.wokbee/agent_memory/overview.md`，≤40000 字），
  含「用户记忆」与「Agent 记忆（能力/坑/环境/工具/需管理内容）」。运行前注入，一般不改，
  仅当 AI 判断有必要时更新。
- 记忆库：单个 SQLite 表 `memory`，按关键字跨项目查询后按需注入。
  - kind='agent'：每项目一条 ≤2000 字（5W1H）项目 Agent 记忆，key=project_id。
  - kind='user'：用户要求记忆的内容，key=f"{project_id}-user"。
  均含 keywords 与 refs（原始文件绝对路径数组）。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from tokbee.core.config import default_data_dir
from tokbee.core.safe_io import safe_write_text

logger = logging.getLogger("wokbee")

_MEMORY_DIR_NAME = "agent_memory"
_OVERVIEW_FILE = "overview.md"
_DB_FILE = "memory.db"

_MAX_OVERVIEW_CHARS = 40000
_MAX_AGENT_MEMORY_CHARS = 2000
_MAX_USER_MEMORY_CHARS = 5000
_MAX_READ_ROWS = 50

_AGENT_KEY_PREFIX = ""
_USER_KEY_SUFFIX = "-user"


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def agent_memory_root() -> Path:
    root = default_data_dir() / _MEMORY_DIR_NAME
    root.mkdir(parents=True, exist_ok=True)
    return root


def overview_path() -> Path:
    return agent_memory_root() / _OVERVIEW_FILE


def memory_db_path() -> Path:
    return agent_memory_root() / _DB_FILE


def agent_memory_key(project_id: str) -> str:
    return str(project_id or "").strip()


def user_memory_key(project_id: str) -> str:
    return f"{str(project_id or '').strip()}{_USER_KEY_SUFFIX}"


def _clip(text: str, max_chars: int) -> str:
    s = (text or "").strip()
    if len(s) <= max_chars:
        return s
    return s[: max(0, max_chars - 1)].rstrip() + "…"


# --------------------------------------------------------------------------- #
# 记忆概述
# --------------------------------------------------------------------------- #

def read_overview() -> str:
    path = overview_path()
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def write_overview(text: str) -> Path:
    path = overview_path()
    safe_write_text(path, _clip(text, _MAX_OVERVIEW_CHARS))
    return path


def build_default_overview(*, settings=None) -> str:
    """从系统提示词与运行环境剥离融汇出第一版记忆概述（≤40000 字）。

    能力描述 / 需管理内容来自 `static_system_prompt` 与 AGENTS.md 的行为约定；
    系统环境来自 runtime_env；工具清单为当前注册的全部工具。
    """
    from wokbee.engine import prompt as prompt_mod
    from wokbee.engine.runtime_env import collect_runtime_env, format_runtime_env_block

    settings = settings or _default_settings()
    run_prompt = prompt_mod.static_system_prompt(mode="run")
    chat_prompt = prompt_mod.static_system_prompt(mode="chat")

    env = collect_runtime_env(settings=settings)
    env_block = format_runtime_env_block(env)

    tool_lines = "\n".join(f"- {t}" for t in _TOOL_INDEX)

    sections = [
        "# WokBee Agent 记忆概述",
        "",
        "> 本文件是 Agent 的跨项目记忆：**运行前注入**。由系统提示词、运行环境与工具清单提炼的"
        "第一版；一般情况下不更新，仅当 AI 判断有必要时补充或处理。总字数 ≤40000。",
        "",
        "## 用户记忆",
        "",
        "（暂无用户画像；后续由 Agent 在交互中总结用户的喜好与要求后补充。用户单独要求记住的内容"
        "写入记忆库 kind=user，并按关键字跨项目检索。）",
        "",
        "## Agent 记忆",
        "",
        "### 能力描述",
        "",
        _condense(run_prompt),
        "",
        "交互模式补充：",
        _condense(chat_prompt),
        "",
        "### 踩过的坑",
        "",
        "（暂无沉淀；运行/对话中发现的失败教训会写入对应项目 agent 记忆，必要时由 AI 更新本节。）",
        "",
        "### 系统环境",
        "",
        env_block,
        "",
        "### 可调用工具",
        "",
        tool_lines,
        "",
        "### 需管理内容",
        "",
        "- **查找优先记忆**：当用户让你「找 / 查 / 搜索 / 寻找」某个东西时，优先用 "
        "`search_memory`（跨项目记忆库）与 `load_conversation_memory`（对话记忆）检索记忆相关"
        "内容，再考虑联网或文件检索；命中后把出处（记忆原文/原始文件地址）告知用户。",
        "- 目录约定：workspace/ 沙箱、deliverables/ 交付物、uploads/ 用户上传（归档保留）、"
        "memory/experiences/ 经验、memory/chat_memory.md 对话记忆、scripts/ 管线脚本、"
        "references/ 参考材料（归档保留）、archives/ 归档（**禁止访问**）。",
        "- 使用外部软件/服务/登录/环境参数时，把可复用代码/配置/密钥存到 references/ 并在 "
        "MANIFEST.md 登记，确保稳定复跑；敏感信息仅供本机。",
        "- 凭据：list_credentials / get_credential 只给环境变量名；execute 时密码已注入进程环境，"
        "严禁在回复、命令或文件中写出账号密码。",
        "- 文件工具一律用虚拟路径（workspace/、deliverables/、uploads/、/ext/…）；"
        "仅 execute 接受真实主机路径。",
        "",
    ]
    return "\n".join(sections)


def _condense(prompt: str, max_chars: int = 700) -> str:
    text = (prompt or "").strip()
    text = text.replace("\n", " ").replace("  ", " ").strip()
    return _clip(text, max_chars)


def _default_settings():
    from wokbee.core.settings import WokBeeSettings

    return WokBeeSettings()


# 供记忆概述使用的工具清单（与 runner.build_agent 注册保持一致）
_TOOL_INDEX = (
    "联网：web_search / deepseek_web_search / http_get / http_request",
    "文件：read_file / write_file / edit_file / ls / glob / grep / delete（虚拟路径）",
    "命令：execute（本机，pwsh 执行）",
    "项目元信息：get_project_info / update_project_title / update_project_goal",
    "凭据：list_credentials / get_credential",
    "意图澄清：ask_user",
    "外部目录授权：request_access",
    "对话记忆：load_conversation_memory（按需注入最近对话记忆）",
    "Agent 记忆库：search_memory（跨项目关键字调取）",
    "用户记忆：save_user_memory（记住用户要求的内容）",
    "MCP：按已加载服务器暴露的工具（若有）",
)


def ensure_overview() -> str:
    """读取记忆概述；不存在则用第一版生成并落盘。返回文本。"""
    text = read_overview()
    if text.strip():
        return text
    text = build_default_overview()
    write_overview(text)
    return text


def reset_memory() -> str:
    """清空记忆库，并将全局记忆概述恢复为系统初始版本。"""
    overview = build_default_overview()
    with _conn_lock:
        _init_schema()
        conn = _get_conn()
        with conn:
            conn.execute("DELETE FROM memory")
    write_overview(overview)
    return overview


# --------------------------------------------------------------------------- #
# 记忆库（单一 SQLite 表）
# --------------------------------------------------------------------------- #

_conn_lock = threading.RLock()
_conns: dict[str, sqlite3.Connection] = {}


def _get_conn() -> sqlite3.Connection:
    path = memory_db_path()
    with _conn_lock:
        conn = _conns.get(str(path))
        if conn is None:
            conn = sqlite3.connect(str(path), check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            _conns[str(path)] = conn
        return conn


def _init_schema() -> None:
    conn = _get_conn()
    with conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS memory (
                memory_key TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                kind TEXT NOT NULL DEFAULT 'agent',
                content TEXT NOT NULL,
                keywords TEXT NOT NULL,
                refs TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_keywords ON memory(keywords);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_kind ON memory(kind);")


def _encode_refs(refs) -> str:
    if not refs:
        return "[]"
    out: list[str] = []
    for r in refs:
        s = str(r or "").strip()
        if s and s not in out:
            out.append(s)
    return json.dumps(out, ensure_ascii=False)


def _decode_refs(raw: str) -> list[str]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    return [str(x) for x in data if str(x).strip()]


def upsert_memory(
    *,
    project_id: str,
    kind: str = "agent",
    content: str,
    keywords: list[str] | str,
    refs: list[str] | None = None,
) -> str:
    """写入/更新一条记忆。kind='agent' 用 project_id 作 key；kind='user' 用 {pid}-user。"""
    _init_schema()
    kind = "user" if kind == "user" else "agent"
    key = user_memory_key(project_id) if kind == "user" else agent_memory_key(project_id)
    max_chars = _MAX_AGENT_MEMORY_CHARS if kind == "agent" else _MAX_USER_MEMORY_CHARS
    content_f = _clip(content, max_chars)
    if not content_f:
        content_f = "（无内容）"
    kw = keywords if isinstance(keywords, list) else [str(keywords or "")]
    kw = [str(k).strip() for k in kw if str(k).strip()]
    kw_f = ",".join(dict.fromkeys(kw))
    refs_f = _encode_refs(refs)
    now = _now()
    conn = _get_conn()
    with conn:
        conn.execute(
            """
            INSERT INTO memory (memory_key, project_id, kind, content, keywords, refs, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(memory_key) DO UPDATE SET
                kind=excluded.kind,
                content=excluded.content,
                keywords=excluded.keywords,
                refs=excluded.refs,
                updated_at=excluded.updated_at
            """,
            (key, str(project_id or "").strip(), kind, content_f, kw_f, refs_f, now),
        )
    return key


def get_memory(project_id: str, kind: str = "agent") -> dict | None:
    _init_schema()
    key = user_memory_key(project_id) if kind == "user" else agent_memory_key(project_id)
    conn = _get_conn()
    row = conn.execute(
        "SELECT memory_key, project_id, kind, content, keywords, refs, updated_at "
        "FROM memory WHERE memory_key=?",
        (key,),
    ).fetchone()
    if not row:
        return None
    return _row_to_dict(row)


def search_memory(
    query: str,
    kind: str = "all",
    k: int = 3,
) -> list[dict]:
    """按关键字跨项目检索记忆（LIKE 匹配关键字与内容），返回关联度靠前的行。"""
    _init_schema()
    q = (query or "").strip()
    if not q:
        return []
    terms = [t for t in _split_terms(q)[:8]]
    if not terms:
        return []
    conn = _get_conn()
    clauses: list[str] = []
    params: list[str] = []
    for t in terms:
        pat = f"%{t}%"
        clauses.append("(keywords LIKE ? OR content LIKE ?)")
        params.extend([pat, pat])
    sql = (
        "SELECT memory_key, project_id, kind, content, keywords, refs, updated_at "
        "FROM memory WHERE " + " AND ".join(clauses)
    )
    if kind in ("agent", "user"):
        sql += " AND kind=?"
        params.append(kind)
    sql += " ORDER BY updated_at DESC LIMIT ?"
    params.append(str(max(1, min(int(k or 3), _MAX_READ_ROWS))))
    rows = conn.execute(sql, params).fetchall()
    return [_row_to_dict(r) for r in rows]


def _split_terms(q: str) -> list[str]:
    for sep in (",", "，", "、", ";", "；", " ", "　"):
        q = q.replace(sep, " ")
    return [t for t in q.split() if t.strip()]


def _row_to_dict(row) -> dict:
    (
        memory_key,
        project_id,
        kind,
        content,
        keywords,
        refs,
        updated_at,
    ) = row
    return {
        "memory_key": memory_key,
        "project_id": project_id,
        "kind": kind,
        "content": content,
        "keywords": keywords,
        "refs": _decode_refs(refs),
        "updated_at": updated_at,
    }


def format_memory_for_injection(items: list[dict], *, max_chars: int = 6000) -> str:
    """把检索到的记忆行渲染成供注入的文本。"""
    if not items:
        return ""
    lines: list[str] = []
    for i, m in enumerate(items, 1):
        kw = str(m.get("keywords") or "").replace(",", "、")
        lines.append(
            f"{i}. [{m.get('kind')}] 项目 {m.get('project_id')} · 关键字：{kw or '（无）'}"
        )
        content = str(m.get("content") or "").replace("\n", " ")
        lines.append(f"   {content[:600]}")
        refs = m.get("refs") or []
        if refs:
            lines.append("   原始文件：" + "；".join(str(r) for r in refs[:6]))
        lines.append("")
    joined = "\n".join(lines).strip()
    if len(joined) > max_chars:
        joined = "…(前部省略)\n" + joined[-max_chars:]
    return joined


# --------------------------------------------------------------------------- #
# AI 生成 / 更新
# --------------------------------------------------------------------------- #

_AI_PROJECT_MEMORY_SYSTEM = """你是 WokBee 的「项目 Agent 记忆」助手。根据一个项目已有的
经验文档（memory/experiences/）、对话记忆（memory/chat_memory.md）与相关原始文件，生成该项目的
一条**跨项目可检索**的 Agent 记忆。

硬性要求：
1. 只输出一个 JSON 对象（不要 Markdown 围栏），字段：
   {
     "summary": "项目 Agent 记忆正文，用 5W1H（Why/What/Where/When/Who/How）概括：
                 这个项目要做什么、怎么做、环境、关键步骤与坑、用到的东西；面向「日后跨项目复用」",
     "keywords": ["关键字1", "关键字2", ...],   // 便于索引，不限数量
     "refs": ["原始文件绝对路径1", ...]           // 仅保存地址；可含经验文件、对话记忆、脚本、参考材料
   }
2. summary 不超过 2000 字；简洁、方法向，只记录「怎么做」，少写结果正文。
3. 关键踩坑、可复用脚本/命令、环境依赖、references/ 材料要尽量提炼进 summary 与 keywords。
"""


def summarize_project_agent_memory(
    *,
    model: Any,
    project_id: str,
    goal: str,
    previous_agent_memory: str,
    lesson_text: str,
    chat_memory_text: str,
    refs: list[str],
) -> dict | None:
    """调用模型生成/更新一条项目 Agent 记忆；失败返回 None。"""
    user = (
        f"项目 ID：{project_id or '（无）'}\n"
        f"项目目标：{goal or '（未设置）'}\n\n"
        f"## 现有项目 Agent 记忆（可能为空）\n{previous_agent_memory or '（无）'}\n\n"
        f"## 最新经验（方法向）\n{lesson_text or '（无）'}\n\n"
        f"## 最近对话记忆\n{chat_memory_text or '（无）'}\n\n"
        f"## 候选原始文件地址\n" + ("\n".join(refs) if refs else "（无）") + "\n\n"
        "请输出符合要求的 JSON。summary ≤2000 字，采用 5W1H，突出可复用方法与踩过的坑。"
    )
    messages = [
        {"role": "system", "content": _AI_PROJECT_MEMORY_SYSTEM},
        {"role": "user", "content": user},
    ]
    text = _invoke_text(model, messages)
    if not text:
        return None
    from wokbee.engine.lessons import _parse_ai_summary_json

    data = _parse_ai_summary_json(text)
    if not isinstance(data, dict):
        return None
    summary = str(data.get("summary") or "").strip()
    if not summary:
        return None
    kw_raw = data.get("keywords")
    if isinstance(kw_raw, list):
        keywords = [str(x).strip() for x in kw_raw if str(x).strip()]
    elif isinstance(kw_raw, str):
        keywords = [x.strip() for x in str(kw_raw).replace("，", ",").split(",") if x.strip()]
    else:
        keywords = []
    ref_raw = data.get("refs")
    if isinstance(ref_raw, list):
        new_refs = [str(x).strip() for x in ref_raw if str(x).strip()]
    elif isinstance(ref_raw, str):
        new_refs = [x.strip() for x in str(ref_raw).splitlines() if x.strip()]
    else:
        new_refs = []
    merged_refs: list[str] = []
    for r in list(refs) + new_refs:
        if r and r not in merged_refs:
            merged_refs.append(r)
    return {"summary": summary, "keywords": keywords, "refs": merged_refs[:200]}


def _invoke_text(model, messages: list[dict]) -> str:
    text = ""
    try:
        parts: list[str] = []
        for chunk in model.stream(messages):
            piece = getattr(chunk, "content", None)
            if piece is None:
                continue
            if isinstance(piece, list):
                piece = "".join(
                    b.get("text", "") if isinstance(b, dict) else str(b) for b in piece
                )
            piece = str(piece)
            if piece:
                parts.append(piece)
        text = "".join(parts).strip()
    except Exception:
        text = ""
    if not text:
        try:
            resp = model.invoke(messages)
            raw = getattr(resp, "content", None) or str(resp)
            if isinstance(raw, list):
                raw = "\n".join(
                    b.get("text", "") if isinstance(b, dict) else str(b) for b in raw
                )
            text = str(raw).strip()
        except Exception:
            text = ""
    return text


_AI_OVERVIEW_JUDGE_SYSTEM = """你是 WokBee 的「记忆概述是否需要更新」决策助手。

背景：记忆概述（≤40000 字）是跨项目、运行前注入的高层总结（用户记忆 + Agent 能力/坑/环境/工具/需管理内容）。
它**一般情况下不更新**。

值得更新的情形（需确有意义）：
1. 发现了新的、影响未来所有项目的**跨项目**经验/坑/能力/工具/环境变化。
2. 用户画像（喜好/要求）有显著的、稳定的变化。
3. 现有概述缺失关键章节、明显过时或错误。

不必更新的情形：
1. 本次只是某个具体项目的局部进展（这类应写入项目 agent 记忆，而非全局概述）。
2. 无跨项目层面新信息。

硬性要求：只返回一个 JSON 对象，格式 {"should_update": true/false, "reason": "一句话理由"}。
默认应偏向 false。"""


def judge_update_overview(*, model: Any, overview: str, project_id: str, new_info: str) -> bool:
    messages = [
        {"role": "system", "content": _AI_OVERVIEW_JUDGE_SYSTEM},
        {
            "role": "user",
            "content": (
                f"## 当前记忆概述\n{overview or '（无）'}\n\n"
                f"## 来自项目 {project_id} 的新信息\n{new_info[:3000] or '（无）'}\n\n"
                "请判断是否需要更新记忆概述，仅返回 JSON。"
            ),
        },
    ]
    text = _invoke_text(model, messages)
    if not text:
        return False
    from wokbee.engine.lessons import _parse_ai_summary_json

    data = _parse_ai_summary_json(text)
    return bool(data and data.get("should_update"))


_AI_OVERVIEW_REWRITE_SYSTEM = """你是 WokBee 的记忆概述维护助手。根据现有记忆概述与新增的跨项目信息，
重写一份精炼的「记忆概述」Markdown。

结构必须包含：
## 用户记忆
## Agent 记忆
（Agent 记忆下建议小节：能力描述 / 踩过的坑 / 系统环境 / 可调用工具 / 需管理内容）

硬性要求：
1. 总字数 ≤40000。
2. 只记录跨项目、运行前需要预知的内容；具体项目的细节交给项目 agent 记忆，不要堆进概述。
3. 保持方法向、简洁；只输出 Markdown 正文（不要 Markdown 围栏）。"""


def rewrite_overview(*, model: Any, overview: str, project_id: str, new_info: str) -> str:
    """让 AI 重写记忆概述（≤40000 字）；失败时返回原概述。"""
    messages = [
        {"role": "system", "content": _AI_OVERVIEW_REWRITE_SYSTEM},
        {
            "role": "user",
            "content": (
                f"## 现有记忆概述\n{overview or '（无）'}\n\n"
                f"## 来自项目 {project_id} 的新信息\n{new_info[:4000] or '（无）'}\n\n"
                "请重写记忆概述（≤40000 字），仅输出 Markdown 正文。"
            ),
        },
    ]
    text = _invoke_text(model, messages)
    if not text:
        return overview
    return _clip(text, _MAX_OVERVIEW_CHARS)


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #

def build_memory_tools(*, emit=None):
    """构造跨项目记忆工具：search_memory / save_user_memory。"""
    from langchain_core.tools import tool

    def _notify(kind: str, content: str, meta: dict | None = None) -> None:
        if emit:
            try:
                emit(kind, content, meta or {})
            except Exception:
                pass

    @tool
    def search_memory(query: str, kind: str = "all", k: int = 3) -> str:
        """跨项目检索 Agent 记忆库（每项目一条 agent 记忆 + 用户要求记忆的内容）。

        当你需要跨项目或过往项目的经验/方法/踩过的坑、或回忆用户之前要求记住的内容时，
        传入关键字查询，返回关联度较高的记忆正文与原始文件地址。kind 可选 'agent'/'user'/'all'。
        """
        items = search_memory_store(query, kind=kind, k=k)
        text = format_memory_for_injection(items)
        return text or "（未检索到匹配的记忆）"

    @tool
    def save_user_memory(content: str, keywords: str = "", refs: list[str] | None = None) -> str:
        """保存用户**单独要求 Agent 记住**的内容（跨项目可检索）。

        当用户说「记住…」「以后都要…」「我的偏好是…」等明确要长期记住时调用。
        content 为要记住的内容（可精简说明）；keywords 用逗号分隔便于检索；
        refs 为与该记忆相关的原始文件绝对路径（可选）。key 为「项目ID-user」。
        """
        if not (content or "").strip():
            return "错误：内容不能为空"
        key = upsert_user_memory(content=content, keywords=keywords, refs=refs)
        _notify("info", f"已保存用户记忆（key={key}），可跨项目检索。")
        return f"已保存用户记忆，key={key}；之后可随时用 search_memory 按关键字调取。"

    return [search_memory, save_user_memory]


def search_memory_store(query: str, *, kind: str = "all", k: int = 3) -> list[dict]:
    return search_memory(query, kind=kind, k=k)


def upsert_user_memory(
    *,
    content: str,
    keywords: list[str] | str,
    refs: list[str] | None = None,
    project_id: str = "",
) -> str:
    """保存用户要求记忆的内容。project_id 缺省则用 'user' 占位（跨项目仍可检索）。"""
    return upsert_memory(
        project_id=project_id or "user",
        kind="user",
        content=content,
        keywords=keywords,
        refs=refs,
    )
