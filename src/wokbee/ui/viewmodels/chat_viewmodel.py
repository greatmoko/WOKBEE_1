"""ChatView 的 ViewModel — 将业务逻辑从 View 中分离。"""

from __future__ import annotations

import json as _json
import logging
import re
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from wokbee.core.chat_manager import ChatManager, ChatSession
from wokbee.core.provider_store import ProviderStore, ResolvedModel
from wokbee.core.session_settings import SessionSettings
from wokbee.core.ai_role import AIRole, AIRoleManager
from wokbee.core.file_reader import (
    is_image, is_document, read_image_as_base64, read_file_as_text,
)

logger = logging.getLogger("wokbee")


class ChatViewModel(QObject):
    session_changed = Signal(object)
    session_created = Signal(str)
    title_updated = Signal(str)
    model_changed = Signal(object)  # ResolvedModel | None
    sending_changed = Signal(bool)
    files_changed = Signal(list)
    error = Signal(str)

    def __init__(
        self,
        chat_manager: ChatManager,
        role_manager: AIRoleManager,
        provider_store: ProviderStore | None = None,
    ):
        super().__init__()
        self.manager = chat_manager
        self.role_manager = role_manager
        self._provider_store = provider_store or ProviderStore()
        self._session: ChatSession | None = None
        self._current_model: ResolvedModel | None = None
        self._pending_files: list[str] = []
        self._drafts: dict[str, str] = {}

    @property
    def provider_store(self) -> ProviderStore:
        return self._provider_store

    @property
    def session(self) -> ChatSession | None:
        return self._session

    @property
    def current_model(self) -> ResolvedModel | None:
        return self._current_model

    @property
    def pending_files(self) -> list[str]:
        return list(self._pending_files)

    def save_draft(self, text: str):
        if self._session:
            self._drafts[self._session.id] = text

    def pop_draft(self, session_id: str) -> str:
        return self._drafts.pop(session_id, "")

    def init_model_for_session(self):
        self._provider_store = ProviderStore()
        self._current_model = None
        settings = self._session.get_params() if self._session else None

        if settings and settings.provider and settings.model_id:
            self._current_model = self._provider_store.resolve(
                settings.provider, settings.model_id,
            )

        if not self._current_model and self._session and self._session.model_name:
            self._current_model = self._provider_store.resolve(
                self._session.model_provider, self._session.model_name,
            )

        if not self._current_model:
            self._current_model = self._provider_store.first_resolved()

        if self._current_model and self._session:
            p = self._session.get_params()
            p.provider = self._current_model.provider_id
            p.model_id = self._current_model.model_id
            self._session.set_params(p)
            self.manager.save()

        self.model_changed.emit(self._current_model)

    def select_model(self, provider_id: str, model_id: str):
        selected = self._provider_store.resolve(provider_id, model_id)
        if not selected:
            return
        self._current_model = selected
        self.model_changed.emit(self._current_model)
        if self._session:
            p = self._session.get_params()
            p.provider = selected.provider_id
            p.model_id = selected.model_id
            self._session.set_params(p)
            self.manager.save()

    def load_session(self, session_id: str):
        session = self.manager.get(session_id)
        if not session:
            return
        self._session = session
        self._pending_files.clear()
        self.files_changed.emit(self._pending_files)
        self.session_changed.emit(session)
        self.init_model_for_session()

    def ensure_session(self) -> ChatSession:
        if self._session:
            return self._session
        if not self._current_model:
            raise ValueError("未配置模型")
        session = self.manager.create(
            provider=self._current_model.provider_id,
            model=self._current_model.model_id,
        )
        self._session = session
        self.session_created.emit(session.id)
        return session

    def add_files(self, paths: list[str]):
        for p in paths:
            if p not in self._pending_files:
                self._pending_files.append(p)
        self.files_changed.emit(self._pending_files)

    def remove_file(self, fp: str):
        if fp in self._pending_files:
            self._pending_files.remove(fp)
        self.files_changed.emit(self._pending_files)

    def build_send_payload(self, text: str) -> tuple[str, list[dict], str]:
        session = self.ensure_session()
        api_parts: list[dict] = []
        display_text = text
        doc_prefix = ""

        for fp in self._pending_files:
            try:
                if is_image(fp):
                    b64, mime = read_image_as_base64(fp)
                    api_parts.append({
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime};base64,{b64}"},
                    })
                    display_text += f"\n[📎 {Path(fp).name}]"
                elif is_document(fp):
                    extracted = read_file_as_text(fp)
                    doc_prefix += f"--- {Path(fp).name} ---\n{extracted}\n---\n\n"
                    display_text += f"\n[📎 {Path(fp).name}]"
            except Exception as exc:
                self.error.emit(f"读取文件失败: {Path(fp).name}\n{exc}")
                logger.error("读取附件 %s 失败: %s", fp, exc)

        self._pending_files.clear()
        self.files_changed.emit(self._pending_files)

        full_text = doc_prefix + text if doc_prefix else text
        has_images = any(p.get("type") == "image_url" for p in api_parts)
        if has_images:
            if full_text:
                api_parts.insert(0, {"type": "text", "text": full_text})
            api_user_content = api_parts
        else:
            api_user_content = full_text

        if not display_text.strip():
            display_text = "[附件]"

        session.messages.append({"role": "user", "content": display_text})
        self.manager.touch(session.id)
        self.manager.save()

        params = session.get_params()
        all_msgs = session.messages
        max_msgs = params.max_context_message_count
        if max_msgs > 0 and max_msgs < 10_000_000:
            api_messages = [
                {"role": m["role"], "content": m["content"]}
                for m in all_msgs[-(max_msgs + 1):-1]
            ]
        else:
            api_messages = [
                {"role": m["role"], "content": m["content"]}
                for m in all_msgs[:-1]
            ]
        api_messages.append({"role": "user", "content": api_user_content})
        if params.system_prompt.strip():
            api_messages.insert(0, {"role": "system", "content": params.system_prompt.strip()})

        return display_text, api_messages, session.id

    def get_chat_params(self) -> SessionSettings | None:
        if not self._session:
            return None
        return self._session.get_params()

    def save_chat_params(self, params: SessionSettings):
        if self._session:
            self._session.set_params(params)
            self.manager.save()
            if params.provider and params.model_id:
                self.select_model(params.provider, params.model_id)

    def save_stream_result(self, session_id: str, content: str, reasoning: str):
        session = self.manager.get(session_id)
        if not session:
            return
        content, reasoning = self.parse_think_tags(content, reasoning)
        if not content.strip() and reasoning.strip():
            content = reasoning
            reasoning = ""
        msg: dict = {"role": "assistant", "content": content}
        if reasoning:
            msg["reasoning_content"] = reasoning
        session.messages.append(msg)
        self.manager.save()

    def save_sync_result(self, session_id: str, content: str, reasoning: str):
        content, reasoning = self.parse_think_tags(content, reasoning)
        if not content.strip() and reasoning.strip():
            content = reasoning
            reasoning = ""
        session = self.manager.get(session_id)
        if session:
            msg: dict = {"role": "assistant", "content": content}
            if reasoning:
                msg["reasoning_content"] = reasoning
            session.messages.append(msg)
            self.manager.save()

    def auto_title(self, session: ChatSession):
        if not session.title.startswith("对话 "):
            return False
        user_msgs = [m for m in session.messages if m["role"] == "user"]
        if len(user_msgs) != 1:
            return False
        raw = user_msgs[0].get("content", "")
        if isinstance(raw, list):
            text_parts = [
                p.get("text", "") for p in raw
                if isinstance(p, dict) and p.get("type") == "text"
            ]
            raw = " ".join(text_parts)
        fallback = raw.strip().replace("\n", " ")[:30]
        if not fallback:
            return False
        session.title = fallback
        self.title_updated.emit(session.id)
        return True

    def apply_ai_name(self, session_id: str, reply: str):
        name = ""
        text = reply.strip()
        text = re.sub(r'<think>[\s\S]*?</think>', '', text, flags=re.IGNORECASE).strip()
        for m in reversed(list(re.finditer(r'\{[^{}]*\}', text))):
            try:
                obj = _json.loads(m.group())
                name = str(obj.get("name") or "").strip()
                if name:
                    name = name[:30]
                    break
            except (_json.JSONDecodeError, TypeError):
                continue
        if not name:
            return
        s = self.manager.get(session_id)
        if not s:
            return
        s.title = name
        self.manager.save()
        self.title_updated.emit(session_id)

    def create_role(self, name: str, description: str) -> AIRole:
        new_role = AIRole(name=name, description=description)
        self.role_manager.add(new_role)
        return new_role

    @staticmethod
    def parse_think_tags(content: str, reasoning: str) -> tuple[str, str]:
        if "<think>" in content:
            match = re.search(r"<think>(.*?)</think>", content, re.DOTALL)
            if match:
                think_text = match.group(1).strip()
                cleaned = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
                if think_text and not reasoning:
                    reasoning = think_text
                return cleaned, reasoning
        return content, reasoning

    def show_welcome_state(self):
        self._provider_store = ProviderStore()
        self._current_model = None
        self._session = None
        self.model_changed.emit(self._current_model)

    def clear_session(self):
        self._session = None
