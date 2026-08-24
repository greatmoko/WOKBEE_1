"""对话管理 — 支持 CRUD、置顶、搜索。"""

import json
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path

from wokbee.core.session_settings import SessionSettings, GlobalSessionDefaults
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
    params: dict = field(default_factory=dict)

    def get_params(self) -> SessionSettings:
        raw = dict(self.params or {})
        if not raw.get("provider") and self.model_provider:
            raw["provider"] = self.model_provider
        if not raw.get("model_id") and self.model_name:
            raw["model_id"] = self.model_name
        return SessionSettings.from_dict(raw)

    def set_params(self, p: SessionSettings):
        self.params = p.to_dict()
        if p.provider:
            self.model_provider = p.provider
        if p.model_id:
            self.model_name = p.model_id


class ChatManager:
    """管理对话列表的持久化与操作。"""

    def __init__(self, config_path: str | None = None):
        if config_path:
            self._path = Path(config_path)
        else:
            self._path = Path.home() / ".wokbee" / "chats.json"
        self._sessions: list[ChatSession] = []
        self._defaults = GlobalSessionDefaults()
        self._load()

    def _load(self):
        if self._path.exists():
            try:
                data = json.loads(self._path.read_text(encoding="utf-8"))
                self._sessions = []
                for item in data:
                    if not isinstance(item, dict):
                        continue
                    allowed = {f.name for f in ChatSession.__dataclass_fields__.values()}
                    filtered = {k: v for k, v in item.items() if k in allowed}
                    self._sessions.append(ChatSession(**filtered))
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
        defaults = self._defaults.get()
        if provider:
            defaults.provider = provider
        if model:
            defaults.model_id = model
        session = ChatSession(
            title=title,
            model_provider=defaults.provider,
            model_name=defaults.model_id,
            params=defaults.to_dict(),
        )
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
        before = len(self._sessions)
        self._sessions = [s for s in self._sessions if s.pinned]
        self.save()
        return before - len(self._sessions)

    def touch(self, session_id: str):
        s = self.get(session_id)
        if s:
            s.updated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    @property
    def session_defaults(self) -> GlobalSessionDefaults:
        return self._defaults
