"""会话级不可变 system 与【会话上下文】块构建。"""

from __future__ import annotations


def static_system_prompt(*, mode: str) -> str:
    """会话级不可变 system：仅保留身份/模式/硬规则，细节行为见【记忆概述】与【会话上下文】。"""
    if mode == "chat":
        return (
            "你是 WokBee——运行在用户本机上的工作助手，具备**完整**网络与本机执行能力。\n"
            "当前是**交互模式**（用户点「发送」）：不自动跑经验/脚本有序管线，"
            "但你仍可自由使用全部能力完成用户请求；提问可与项目目标无关。\n"
            "**严禁**访问 archives/。\n"
            "能力范围、系统环境、可调用工具、目录与凭据约定、跨项目记忆概述见用户消息中的"
            "【记忆概述】；本轮项目态见【会话上下文】。两者均已注入，务必遵循，勿假设 system 会随轮次改写。\n"
            "意图不清或有多种做法时，请用 ask_user 向用户提问；需要跨项目记忆时用 search_memory 检索。\n"
            "当用户让你「找 / 查 / 搜索 / 寻找」某个东西时，**优先**用 search_memory（跨项目记忆）"
            "与 load_conversation_memory（对话记忆）检索记忆相关内容，再考虑联网或文件检索。"
        )
    return (
        "你是 WokBee——运行在用户本机上的工作助手，具备完整网络与本机执行能力。\n"
        "按项目的【记忆概述】与经验执行：能力/环境/工具/目录与凭据约定见【记忆概述】，"
        "本轮项目态见【会话上下文】；两者均已注入，务必遵循。\n"
        "**严禁**读取、列举、搜索或通过 shell 访问 `archives/`。\n"
        "文件工具只用虚拟路径（workspace/、deliverables/、uploads/、/ext/…）；"
        "仅 execute 接受真实主机路径。\n"
        "主机按 pipeline.json 的 steps 顺序推进，仅在 type=ai 的步骤唤你。\n"
        "凭据：list_credentials / get_credential 只给环境变量名，严禁在回复、命令或文件中写出账号密码。\n"
        "需要跨项目记忆时用 search_memory 检索；用户明确要求记住的内容用 save_user_memory 保存。\n"
        "当用户让你「找 / 查 / 搜索 / 寻找」某个东西时，**优先**用 search_memory（跨项目记忆）"
        "与 load_conversation_memory（对话记忆）检索记忆相关内容，再考虑联网或文件检索。\n"
        "system 在本会话内保持字节级稳定以利于 DeepSeek 前缀缓存。"
    )


def build_session_context_block(
    *,
    title: str,
    goal: str,
    approval_summary: str,
    max_steps: int | None = None,
    experience_digest: str = "",
    mode: str = "run",
    runtime_env_block: str = "",
    memory_overview_digest: str = "",
    extra_lines: list[str] | None = None,
) -> str:
    """易变内容：拼进首条/当轮 user，不进 system。"""
    lines = [
        "【会话上下文】（本块可能随项目变更；勿写入对 system 稳定性的假设）",
        f"- 模式：{'交互' if mode == 'chat' else '运行'}",
        f"- 项目名称：{title or '未命名项目'}",
        f"- 目标：{goal or '（未设置）'}",
        f"- 审核策略：{approval_summary or '（未设置）'}",
    ]
    if max_steps is not None and int(max_steps) > 0:
        lines.append(f"- 步数上限约：{int(max_steps)}（请聚焦目标）")
    if runtime_env_block.strip():
        lines.append("")
        lines.append(runtime_env_block.strip())
    if experience_digest.strip():
        lines.append("")
        lines.append(experience_digest.strip())
    if memory_overview_digest.strip():
        lines.append("")
        lines.append("【记忆概述】（跨项目 Agent 记忆，运行前注入；一般无需改写）")
        lines.append(memory_overview_digest.strip())
    if extra_lines:
        for line in extra_lines:
            s = (line or "").strip()
            if s:
                lines.append(s)
    return "\n".join(lines)


def compose_user_with_context(user_message: str, context_block: str) -> str:
    user_message = (user_message or "").strip()
    context_block = (context_block or "").strip()
    if not context_block:
        return user_message
    if not user_message:
        return context_block
    return f"{context_block}\n\n——\n{user_message}"
