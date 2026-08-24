"""AI 角色管理 — AIRole 数据类 + AIRoleManager CRUD。"""

import json
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path

from wokbee.core.safe_io import safe_write_json


@dataclass
class AIRole:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    name: str = ""
    description: str = ""
    is_default: bool = False
    created_at: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    updated_at: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))


_DEFAULT_ROLE = AIRole(
    id="default",
    name="通用助手",
    description="You are a helpful assistant.",
    is_default=True,
)


class AIRoleManager:
    """AI 角色列表的增删改查 + JSON 持久化。"""

    def __init__(self, config_path: str | None = None):
        self._path = Path(config_path) if config_path else Path.home() / ".wokbee" / "ai_roles.json"
        self._roles: list[AIRole] = []
        self._load()

    def _load(self):
        if self._path.exists():
            try:
                data = json.loads(self._path.read_text(encoding="utf-8"))
                self._roles = [AIRole(**item) for item in data]
            except (json.JSONDecodeError, OSError, TypeError):
                self._roles = []
        else:
            self._roles = []

        if not any(r.is_default for r in self._roles):
            self._roles.insert(0, AIRole(
                id=_DEFAULT_ROLE.id,
                name=_DEFAULT_ROLE.name,
                description=_DEFAULT_ROLE.description,
                is_default=True,
            ))
            self._save()

    def _save(self):
        data = [asdict(r) for r in self._roles]
        safe_write_json(self._path, data)

    def list_all(self) -> list[AIRole]:
        return list(self._roles)

    def get(self, role_id: str) -> AIRole | None:
        return next((r for r in self._roles if r.id == role_id), None)

    def get_default(self) -> AIRole | None:
        return next((r for r in self._roles if r.is_default), None)

    def add(self, role: AIRole):
        self._roles.append(role)
        self._save()

    def update(self, role_id: str, **kwargs):
        r = self.get(role_id)
        if not r:
            return
        for k, v in kwargs.items():
            if k == "is_default":
                continue
            if hasattr(r, k):
                setattr(r, k, v)
        r.updated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._save()

    def delete(self, role_id: str) -> bool:
        role = self.get(role_id)
        if not role or role.is_default:
            return False
        self._roles = [r for r in self._roles if r.id != role_id]
        self._save()
        return True
