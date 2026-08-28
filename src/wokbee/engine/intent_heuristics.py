"""AI 回复「是否在承诺后续动作」的中英文续跑启发式。"""

from __future__ import annotations

import re


# —— 语义标记（时间指向）表 ——
# 中英文按「时间指向」分组：intent(承诺/下一步) / done(已收工) / unfinished(转折可续跑)。
# 全部编译为大小写不敏感的正则。英文用 (?<!\w)...(?!\w) 词边界匹配 —— 替代原先
# 「i'll 」「i will 」等尾随空格的脆子串（句尾 I'll. 会漏判），同时避免
# "use curl" 误伤 "refuse curl"、裸工具名进 done 分支等子串误判。
# 中文不用词边界（汉字相邻即语义单元，加 \w 边界反而会把「然后我将」漏掉）。
_AI_ACTION_INTENT_MARKERS = (
    "我将", "我会", "我先", "我去", "让我", "准备", "将要", "接下来",
    "确认路径", "确认真实", "申请权限", "将使用", "将用", "会用", "我用",
    "去运行", "去执行", "运行该", "运行此", "运行脚本", "执行该", "执行脚本",
    "调用 execute", "使用 execute", "用 execute", "用 pwsh", "用 curl",
    "通过 execute", "先看一下", "看一下链接",
)
# 英文模型常只输出计划口吻不带 tool_calls；必须识别，否则自动续跑被跳过
_AI_ACTION_INTENT_MARKERS_EN = (
    "let me", "i'll", "i will", "i need to", "i'm going to", "i am going to",
    "going to", "next i'll", "next i will", "i'll use", "i will use",
    "i'll try", "i will try", "try to find", "use curl", "use execute",
    "run curl", "fetch the", "check the site", "get the raw",
)
_AI_DONE_MARKERS = (
    "已完成", "已经完成", "已成功", "已生成", "任务完成",
    # 注意：不要用裸「已写入/已保存」——工具落盘常说「完整结果已写入文件」，
    # 与「让我继续」同句会出现，会误判为收工并跳过自动续跑。
    "已写入 deliverables", "已写入交付", "保存到 deliverables",
)
_AI_DONE_MARKERS_EN = (
    "completed", "already done", "successfully written to deliverables",
    "written to deliverables", "saved to deliverables", "task is done",
    "finished the task",
)
# 完成措辞出现时，只有带「仍未完成/还需继续」的转折才续跑，否则视为收工。
# 注意：勿把裸工具名（用 curl / 用 execute）放进这里——报告里常说「我用 curl 验证了…」，
# 那是过去式陈述，不是未完成动作；否则会让 done 分支形同虚设。
_AI_UNFINISHED_MARKERS = (
    "但还需要", "还需", "还要", "还差", "还未", "尚未", "待办", "仍未",
    "仍需要", "我会继续", "我再", "还要继续", "剩下", "遗留",
)
_AI_UNFINISHED_MARKERS_EN = (
    "but i still", "but we still", "still need", "still have", "not yet",
    "yet to", "remain", "left to do", "i'll continue", "still to",
)


def _marker_re(phrases: tuple[str, ...], *, word_boundary: bool) -> re.Pattern:
    if not phrases:
        return re.compile(r"$^")  # 永不匹配
    parts = [re.escape(p) for p in phrases]
    if word_boundary:
        parts = [rf"(?<!\w){p}(?!\w)" for p in parts]
    return re.compile("|".join(parts), re.IGNORECASE)


# 中文无词边界；英文用词边界
_AI_INTENT_RE = _marker_re(_AI_ACTION_INTENT_MARKERS, word_boundary=False)
_AI_INTENT_RE_EN = _marker_re(_AI_ACTION_INTENT_MARKERS_EN, word_boundary=True)
_AI_DONE_RE = _marker_re(_AI_DONE_MARKERS, word_boundary=False)
_AI_DONE_RE_EN = _marker_re(_AI_DONE_MARKERS_EN, word_boundary=True)
_AI_UNFINISHED_RE = _marker_re(_AI_UNFINISHED_MARKERS, word_boundary=False)
_AI_UNFINISHED_RE_EN = _marker_re(_AI_UNFINISHED_MARKERS_EN, word_boundary=True)


def ai_reply_suggests_pending_action(text: str) -> bool:
    """AI 回复是否在承诺后续动作、但任务可能尚未真正收尾（中英均可）。

    优先看「完成措辞」：一旦明确收工就默认结束（False），除非同时出现
    「但仍需/还没完成」等未完成转折；此时才判定为待续跑。
    无完成措辞时，退回「动作意图」启发式：描述下一步动作 → True。
    全部为词边界 + 大小写不敏感正则匹配，规避子串误判与句尾缺空格漏判。
    """
    msg = (text or "").strip()
    if not msg:
        return False
    has_done = bool(_AI_DONE_RE.search(msg) or _AI_DONE_RE_EN.search(msg))
    if has_done:
        return bool(_AI_UNFINISHED_RE.search(msg) or _AI_UNFINISHED_RE_EN.search(msg))
    has_intent = bool(_AI_INTENT_RE.search(msg) or _AI_INTENT_RE_EN.search(msg))
    return has_intent
