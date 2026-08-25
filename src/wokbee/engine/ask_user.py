"""向用户澄清意图：ask_user 工具 + LangGraph interrupt。

对齐开源 Deep Agents / LangGraph HITL 模式（AskUserMiddleware）：
工具内调用 interrupt() 暂停，宿主弹窗收集答案后 Command(resume=...)。
"""

from __future__ import annotations

from typing import Any, Literal

from langchain_core.tools import tool
from langgraph.types import interrupt
from pydantic import BaseModel, Field


class ClarifyQuestion(BaseModel):
    """一道澄清题。"""

    id: str = Field(description="问题稳定 id，如 q1")
    prompt: str = Field(description="问题正文，简短明确")
    mode: Literal["single", "multi"] = Field(
        default="single",
        description="single=单选；multi=多选",
    )
    options: list[str] = Field(
        description="候选项文案列表（不要把「自定义/其他」写进这里，由系统自动追加）",
        min_length=1,
    )
    allow_custom: bool = Field(
        default=True,
        description="是否在选项末尾追加「其他（请填写）」自定义项",
    )


class AskUserPayload(BaseModel):
    type: Literal["ask_user"] = "ask_user"
    questions: list[ClarifyQuestion]


ASK_USER_TOOL_DESCRIPTION = """当你不确定用户意图、目标范围、安装位置、风格偏好等时，必须调用本工具向用户提问，而不是猜测。

规则：
- 一次可问 1～5 个问题；优先选择题，少用开放题。
- mode=single 单选；mode=multi 多选。
- options 只写具体候选项；不要写「其他/自定义」（系统会自动加最后一项）。
- allow_custom=true（默认）时用户可填自定义内容。
- 收到用户答案后再继续执行。
"""


def is_ask_user_interrupt(value: Any) -> bool:
    if isinstance(value, dict):
        return value.get("type") == "ask_user" and bool(value.get("questions"))
    return False


def normalize_ask_user_value(value: Any) -> dict[str, Any]:
    """把 interrupt 值规整为 AskUserPayload 字典。"""
    if isinstance(value, AskUserPayload):
        return value.model_dump()
    if not isinstance(value, dict):
        return {
            "type": "ask_user",
            "questions": [
                {
                    "id": "q1",
                    "prompt": str(value)[:500],
                    "mode": "single",
                    "options": ["是", "否"],
                    "allow_custom": True,
                }
            ],
        }
    questions = value.get("questions") or []
    out_q: list[dict[str, Any]] = []
    for i, q in enumerate(questions):
        if isinstance(q, ClarifyQuestion):
            out_q.append(q.model_dump())
            continue
        if not isinstance(q, dict):
            continue
        opts = q.get("options") or q.get("choices") or []
        if isinstance(opts, str):
            opts = [opts]
        opts = [str(x).strip() for x in opts if str(x).strip()]
        if not opts:
            opts = ["是", "否"]
        mode = str(q.get("mode") or q.get("selection") or "single").lower()
        if mode in ("multiple", "multi_select", "checkbox"):
            mode = "multi"
        if mode not in ("single", "multi"):
            mode = "single"
        out_q.append(
            {
                "id": str(q.get("id") or f"q{i+1}"),
                "prompt": str(q.get("prompt") or q.get("question") or q.get("text") or "请选择").strip(),
                "mode": mode,
                "options": opts[:20],
                "allow_custom": bool(q.get("allow_custom", True)),
            }
        )
    if not out_q:
        out_q = [
            {
                "id": "q1",
                "prompt": str(value.get("prompt") or value.get("message") or "请确认"),
                "mode": "single",
                "options": ["继续", "取消"],
                "allow_custom": True,
            }
        ]
    return {"type": "ask_user", "questions": out_q}


def format_answers_for_model(payload: dict[str, Any], answers: dict[str, Any]) -> str:
    """把用户答案格式化为工具返回文本。"""
    if answers.get("cancelled"):
        return "用户取消了澄清提问，未作选择。请停止高风险操作，并简要说明需要用户补充什么。"
    qs = {q["id"]: q for q in (payload.get("questions") or []) if isinstance(q, dict)}
    raw_list = answers.get("answers")
    if not isinstance(raw_list, list):
        raw_list = []
    lines = ["用户澄清回答："]
    for item in raw_list:
        if not isinstance(item, dict):
            continue
        qid = str(item.get("id") or "")
        q = qs.get(qid) or {}
        prompt = q.get("prompt") or qid
        selected = item.get("selected") or []
        if isinstance(selected, str):
            selected = [selected]
        custom = str(item.get("custom") or "").strip()
        parts = [str(x) for x in selected if str(x).strip()]
        if custom:
            parts.append(f"自定义：{custom}")
        lines.append(f"- {prompt} → {('；'.join(parts) if parts else '（未选）')}")
    if len(lines) == 1:
        lines.append("- （无有效选项）")
    return "\n".join(lines)


def build_ask_user_tool() -> Any:
    """构造可挂到 create_deep_agent(tools=[...]) 的 ask_user 工具。"""

    @tool(description=ASK_USER_TOOL_DESCRIPTION)
    def ask_user(questions: list[ClarifyQuestion]) -> str:
        """向用户弹窗提问以澄清意图（单选/多选 + 可选自定义）。"""
        raw = [
            q.model_dump() if isinstance(q, ClarifyQuestion) else q for q in (questions or [])
        ]
        payload = normalize_ask_user_value({"type": "ask_user", "questions": raw})
        response = interrupt(payload)
        if not isinstance(response, dict):
            response = {"answers": [], "raw": str(response)}
        return format_answers_for_model(payload, response)

    ask_user.name = "ask_user"
    return ask_user
