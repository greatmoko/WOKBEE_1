"""WokBee 全局设置（存于 ~/.wokbee/config.json 的 wokbee 段）。"""

from __future__ import annotations

from pathlib import Path

from tokbee.core.config import Config

from wokbee.core.models import ApprovalFlags

DEFAULT_WORKSPACE = Path.home() / "WokBeeWorkspace"

DEFAULT_APPROVAL = {
    "skip_read": True,
    "skip_write": False,
    "skip_routine": False,
    "skip_high_risk": False,
}

DEFAULTS = {
    "workspace_root": str(DEFAULT_WORKSPACE),
    "approval": dict(DEFAULT_APPROVAL),
    "default_provider": "",
    "default_model_id": "",
    "max_steps": 40,
    "max_parallel_tools": 4,
    # 有序管线：script/ai 阶段切换次数上限（非强制交错，按 pipeline steps 顺序）
    "max_pipeline_phases": 64,
    # AI 调用间隔（毫秒）：两次 AI 调用「发起时间」的最小间隔；0 = 不限制
    "ai_interval_ms": 0,
    # 是否把 DeepSeek 官方服务端搜索注册成 deepseek_web_search 工具
    "enable_deepseek_search": True,
    # 已授权的附加目录（项目外）：Agent 可经 /ext/<slug>/ 虚拟路径用文件工具访问。
    # 元素为 {"name": <slug>, "path": <绝对路径>}，全局共享、跨项目生效。
    "additional_directories": [],
}


def _default_workspace_str() -> str:
    return str(DEFAULT_WORKSPACE)


class WokBeeSettings:
    """读写 WokBee 配置。"""

    def __init__(self, config: Config | None = None):
        self._config = config or Config()
        self._ensure_defaults()

    def _ensure_defaults(self) -> None:
        dirty = False
        for key, value in DEFAULTS.items():
            if self._config.get(f"wokbee.{key}") is None:
                self._config.set(
                    f"wokbee.{key}",
                    dict(value) if isinstance(value, dict) else value,
                )
                dirty = True
        # 迁移旧策略字段 → approval 勾选
        if self._config.get("wokbee.approval") is None or not isinstance(
            self._config.get("wokbee.approval"), dict
        ):
            legacy = self._migrate_legacy_approval()
            self._config.set("wokbee.approval", legacy.to_dict())
            dirty = True
        else:
            # 补齐缺失的勾选键
            cur = dict(self._config.get("wokbee.approval") or {})
            for k, v in DEFAULT_APPROVAL.items():
                if k not in cur:
                    cur[k] = v
                    dirty = True
            self._config.set("wokbee.approval", cur)
        if dirty:
            self._config.save()

    def _migrate_legacy_approval(self) -> ApprovalFlags:
        raw = {
            "policy": self._config.get("wokbee.default_policy"),
            "trust_yolo": self._config.get("wokbee.allow_project_yolo"),
        }
        # allow_project_yolo 不是 trust，只表示允许；旧默认仍用 graded
        if raw.get("policy"):
            return ApprovalFlags.from_legacy(
                {"policy": raw["policy"], "trust_yolo": False}
            )
        return ApprovalFlags.from_dict(DEFAULT_APPROVAL)

    def get(self, key: str, default=None):
        return self._config.get(
            f"wokbee.{key}",
            default if default is not None else DEFAULTS.get(key),
        )

    def set(self, key: str, value) -> None:
        self._config.set(f"wokbee.{key}", value)

    def save(self) -> None:
        self._config.save()

    @property
    def workspace_root(self) -> Path:
        raw = self.get("workspace_root") or ""
        if not str(raw).strip():
            raw = _default_workspace_str()
        return Path(str(raw)).expanduser()

    @workspace_root.setter
    def workspace_root(self, value: str | Path) -> None:
        self.set("workspace_root", str(value))

    @property
    def approval(self) -> ApprovalFlags:
        raw = self.get("approval") or {}
        if not isinstance(raw, dict):
            return ApprovalFlags.from_dict(DEFAULT_APPROVAL)
        return ApprovalFlags.from_dict(raw)

    @approval.setter
    def approval(self, value: ApprovalFlags | dict) -> None:
        if isinstance(value, ApprovalFlags):
            self.set("approval", value.to_dict())
        else:
            self.set("approval", ApprovalFlags.from_dict(value).to_dict())

    @property
    def default_provider(self) -> str:
        return str(self.get("default_provider") or "")

    @default_provider.setter
    def default_provider(self, value: str) -> None:
        self.set("default_provider", value or "")

    @property
    def default_model_id(self) -> str:
        return str(self.get("default_model_id") or "")

    @default_model_id.setter
    def default_model_id(self, value: str) -> None:
        self.set("default_model_id", value or "")

    @property
    def max_steps(self) -> int:
        try:
            return max(1, int(self.get("max_steps", 40)))
        except (TypeError, ValueError):
            return 40

    @max_steps.setter
    def max_steps(self, value: int) -> None:
        self.set("max_steps", max(1, int(value)))

    @property
    def max_parallel_tools(self) -> int:
        try:
            return max(1, int(self.get("max_parallel_tools", 4)))
        except (TypeError, ValueError):
            return 4

    @max_parallel_tools.setter
    def max_parallel_tools(self, value: int) -> None:
        self.set("max_parallel_tools", max(1, int(value)))

    @property
    def max_pipeline_phases(self) -> int:
        try:
            return max(1, int(self.get("max_pipeline_phases", 64)))
        except (TypeError, ValueError):
            return 64

    @max_pipeline_phases.setter
    def max_pipeline_phases(self, value: int) -> None:
        self.set("max_pipeline_phases", max(1, min(500, int(value))))

    @property
    def enable_deepseek_search(self) -> bool:
        return bool(self.get("enable_deepseek_search", True))

    @enable_deepseek_search.setter
    def enable_deepseek_search(self, value: bool) -> None:
        self.set("enable_deepseek_search", bool(value))

    @property
    def additional_directories(self) -> list[dict[str, str]]:
        """已授权附加目录（项目外）。仅返回真实存在且为目录的项。"""
        raw = self.get("additional_directories") or []
        if not isinstance(raw, list):
            return []
        out: list[dict[str, str]] = []
        for item in raw:
            if isinstance(item, dict):
                name = str(item.get("name") or "").strip()
                path = str(item.get("path") or "").strip()
            elif isinstance(item, str):
                path, name = item.strip(), ""
            else:
                continue
            if not path:
                continue
            try:
                rp = Path(path).expanduser().resolve()
            except OSError:
                continue
            if not rp.is_dir():
                continue
            out.append({
                "name": name or rp.name,
                "path": str(rp),
                "slug": str(item.get("slug") or "") if isinstance(item, dict) else "",
            })
        return out

    @additional_directories.setter
    def additional_directories(self, value: list[dict[str, str]]) -> None:
        self.set("additional_directories", list(value or []))

    def add_additional_directory(self, name: str, path: str | Path, slug: str = "") -> dict[str, str] | None:
        """把目录加入全局白名单（去重、保存）。目录不存在则返回 None。

        slug 是 /ext/<slug>/ 虚拟路由段；持久化它，下次会话重建 composite 时能复命同一个
        虚拟路径，避免旧 /ext/<slug>/ 引用（经验/脚本里的路径）跨会话失效或串路由。

        此方法可能从 worker 线程（request_access 工具）调用，与主线程设置页并发读-改-写同一
        单例 Config → 用其锁串行化整段操作，避免更新丢失。
        """
        with self._config.lock:
            try:
                rp = Path(str(path)).expanduser().resolve()
            except OSError:
                return None
            if not rp.is_dir():
                return None
            cur = self._canonicalize(self.get("additional_directories") or []) or []
            key = str(rp)
            for item in cur:
                if item["path"] == key:
                    item["name"] = name or item.get("name") or rp.name
                    if slug:
                        item["slug"] = slug  # 更新为最新选中的 slug
                    self._config.set("wokbee.additional_directories", cur)
                    self._config.save()
                    return {"name": item["name"], "path": item["path"], "slug": item.get("slug", "")}
            entry = {"name": name or rp.name, "path": key}
            if slug:
                entry["slug"] = slug
            cur.append(entry)
            self._config.set("wokbee.additional_directories", cur)
            self._config.save()
            return entry

    def remove_additional_directory(self, path: str | Path) -> bool:
        """按路径移除白名单项。"""
        with self._config.lock:
            try:
                key = str(Path(str(path)).expanduser().resolve())
            except OSError:
                return False
            cur = self._canonicalize(self.get("additional_directories") or []) or []
            new = [x for x in cur if x["path"] != key]
            if len(new) != len(cur):
                self._config.set("wokbee.additional_directories", new)
                self._config.save()
                return True
            return False

    @staticmethod
    def _canonicalize(items: list) -> list[dict[str, str]]:
        """把 [str] 或 [dict] 混合列表统一成 [{"name","path","slug"}]。"""
        out: list[dict[str, str]] = []
        for item in items:
            if isinstance(item, dict) and item.get("path"):
                path = str(item["path"]).strip()
                name = str(item.get("name") or "").strip()
            elif isinstance(item, str) and item.strip():
                path = item.strip()
                name = ""
            else:
                continue
            try:
                rp = Path(path).expanduser().resolve()
            except OSError:
                continue
            if not rp.is_dir():
                continue
            out.append({
                "name": name or rp.name,
                "path": str(rp),
                "slug": str(item.get("slug") or "") if isinstance(item, dict) else "",
            })
        return out

    @property
    def ai_interval_ms(self) -> int:
        try:
            return max(0, int(self.get("ai_interval_ms", 0) or 0))
        except (TypeError, ValueError):
            return 0

    @ai_interval_ms.setter
    def ai_interval_ms(self, value: int) -> None:
        self.set("ai_interval_ms", max(0, int(value or 0)))
