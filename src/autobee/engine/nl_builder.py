"""自然语言 → 定时任务配置。"""

from __future__ import annotations

import json
import re

from apscheduler.triggers.cron import CronTrigger

from tokbee.core.ai_client import AIClient
from tokbee.core.provider_store import ResolvedModel

_SYSTEM = (
    "你是一个定时任务配置生成器。根据用户的自然语言描述，输出一个 JSON 对象，"
    "用于创建自动化定时任务。\n"
    "硬性要求：\n"
    "1. 只输出一个 JSON 对象，不要 Markdown 代码块，不要任何解释文字。\n"
    "2. 字段结构：\n"
    '{"name":"任务名称","type":"text|script|wokbee",'
    '"schedule":"5段cron表达式(空格分隔)","cron_text":"人类可读的中文说明","config":{}}\n'
    "3. type 只能是 text | script | wokbee 之一：\n"
    "   - text：文本类。config 可含 {\"content\": \"正文内容\"}\n"
    "   - script：脚本代码。config 可含 {\"code\":\"脚本代码\",\"script_lang\":\"python|javascript\"}；"
    "超时默认 120 秒，无需输出 timeout_s\n"
    "   - wokbee：跑 WokBee 项目任务。config 可含 {\"project_id\":\"prj_xxx(可空)\"}；"
    "无需 user_message / max_steps，执行时自动用项目目标\n"
    "   - 若用户提到企业微信推送，它是推送渠道而非类型：type 仍按上述选，"
    "config 追加 {\"push_wecom\":true,\"webhook_url\":\"群机器人webhook(若有)\","
    "\"msgtype\":\"text|markdown\",\"mention\":\"@all或账号\"}。\n"
    "4. schedule 必须是合法的标准 5 段 cron（分 时 日 月 周），如 \"0 9 * * *\"、\"*/30 * * * *\"、"
    "\"0 9 * * 1-5\"。若描述不完整，请给出最合理的默认值并放进 cron_text 说明。\n"
    "5. cron_text 用简短中文说明触发时机，如 \"每天 09:00\"、\"每个工作日 09:00\"、\"每 30 分钟\"。\n"
    "6. config 只填生成人能确定的内容；不确定的字段留空字符串。\n"
)


class NLBuilder:
    """调用 AI 模型，把一句话变成定时代码配置。"""

    def __init__(self, provider_store=None):
        self._provider_store = provider_store

    def generate(self, text: str, model: ResolvedModel) -> dict | None:
        """返回解析后的 dict；失败返回 None，同时把原始文本放入 self.last_raw。"""
        self.last_raw = ""
        if not (text or "").strip():
            raise ValueError("请输入要生成的自然语言描述")
        if model is None:
            raise ValueError("请先选择用于生成的 AI 模型")

        client = AIClient(
            model.api_host, model.api_key, model.model_id,
            family=model.family, protocol=model.api_protocol,
        )
        resp = client.chat(
            [
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": f"用户需求：{text.strip()}"},
            ],
            temperature=0.2,
            max_tokens=1200,
        )
        raw = (resp.content or "").strip() or (resp.reasoning_content or "").strip()
        self.last_raw = raw
        parsed = self._parse(raw)
        if parsed is None:
            raise ValueError("AI 未能生成可解析的配置，请重试或手动编辑。")
        return parsed

    @staticmethod
    def _parse(raw: str) -> dict | None:
        """去围栏 → json.loads → 正则兜底；校验 cron 与 type。"""
        text = (raw or "").strip()
        if not text:
            return None
        fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
        if fence:
            text = fence.group(1).strip()
        data = None
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            m = re.search(r"\{[\s\S]*\}", text)
            if m:
                try:
                    data = json.loads(m.group(0))
                except json.JSONDecodeError:
                    data = None
        if not isinstance(data, dict):
            return None

        name = str(data.get("name") or "").replace("\x00", "").strip()
        ttype = str(data.get("type") or "text").strip().lower()
        valid_types = {"text", "script", "wokbee"}
        if ttype not in valid_types:
            ttype = "text"  # 含旧语义 wecom → 落入 text，由推送渠道承担
        schedule = str(data.get("schedule") or "").strip()
        if schedule:
            try:
                CronTrigger.from_crontab(schedule)
            except ValueError:
                schedule = ""
        if not schedule:
            return None
        config = data.get("config") if isinstance(data.get("config"), dict) else {}
        config = {str(k): v for k, v in config.items()}
        # 旧模型可能仍输出 wecom：映射为文本 + 开启推送渠道
        if str(data.get("type") or "").strip().lower() == "wecom":
            ttype = "text"
            config.setdefault("push_wecom", True)
        return {
            "name": name or "新的定时任务",
            "type": ttype,
            "schedule": schedule,
            "cron_text": str(data.get("cron_text") or "").replace("\x00", "").strip(),
            "config": config,
        }
