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
    def ai_interval_ms(self) -> int:
        try:
            return max(0, int(self.get("ai_interval_ms", 0) or 0))
        except (TypeError, ValueError):
            return 0

    @ai_interval_ms.setter
    def ai_interval_ms(self, value: int) -> None:
        self.set("ai_interval_ms", max(0, int(value or 0)))
