"""对话管理 — 支持 CRUD、置顶、搜索。"""

import json
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path

from wokbee.core.chat_params import ChatParams
from wokbee.core.safe_io import safe_write_json


@dataclass
class ChatSession:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    title: str = "新对话"
    pinned: bool = False
    model_provider: str = ""
    model_name: str = ""
    created_at: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    updated_at: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    messages: list[dict] = field(default_factory=list)
    params: dict = field(default_factory=lambda: ChatParams().to_dict())

    def get_params(self) -> ChatParams:
        return ChatParams.from_dict(self.params)

    def set_params(self, p: ChatParams):
        self.params = p.to_dict()


class ChatManager:
    """管理对话列表的持久化与操作。"""

    def __init__(self, config_path: str | None = None):
        if config_path:
            self._path = Path(config_path)
        else:
            self._path = Path.home() / ".wokbee" / "chats.json"
        self._sessions: list[ChatSession] = []
        self._load()

    def _load(self):
        if self._path.exists():
            try:
                data = json.loads(self._path.read_text(encoding="utf-8"))
                self._sessions = [ChatSession(**item) for item in data]
            except (json.JSONDecodeError, OSError, TypeError):
                self._sessions = []

    def save(self):
        data = [asdict(s) for s in self._sessions]
        safe_write_json(self._path, data)

    def list_sorted(self) -> list[ChatSession]:
        pinned = sorted(
            [s for s in self._sessions if s.pinned],
            key=lambda s: s.updated_at, reverse=True,
        )
        normal = sorted(
            [s for s in self._sessions if not s.pinned],
            key=lambda s: s.updated_at, reverse=True,
        )
        return pinned + normal

    def search(self, keyword: str) -> list[ChatSession]:
        if not keyword.strip():
            return self.list_sorted()
        kw = keyword.lower()
        results = [s for s in self._sessions if kw in s.title.lower()]
        pinned = sorted([s for s in results if s.pinned], key=lambda s: s.updated_at, reverse=True)
        normal = sorted([s for s in results if not s.pinned], key=lambda s: s.updated_at, reverse=True)
        return pinned + normal

    def create(self, title: str = "", provider: str = "", model: str = "") -> ChatSession:
        if not title:
            title = f"对话 {datetime.now().strftime('%m-%d %H:%M')}"
        session = ChatSession(title=title, model_provider=provider, model_name=model)
        self._sessions.append(session)
        self.save()
        return session

    def get(self, session_id: str) -> ChatSession | None:
        return next((s for s in self._sessions if s.id == session_id), None)

    def rename(self, session_id: str, new_title: str):
        s = self.get(session_id)
        if s:
            s.title = new_title
            self.save()

    def delete(self, session_id: str):
        self._sessions = [s for s in self._sessions if s.id != session_id]
        self.save()

    def toggle_pin(self, session_id: str):
        s = self.get(session_id)
        if s:
            s.pinned = not s.pinned
            self.save()

    def delete_unpinned(self) -> int:
        """删除所有非置顶对话，返回删除的数量。"""
        before = len(self._sessions)
        self._sessions = [s for s in self._sessions if s.pinned]
        self.save()
        return before - len(self._sessions)

    def touch(self, session_id: str):
        s = self.get(session_id)
        if s:
            s.updated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
