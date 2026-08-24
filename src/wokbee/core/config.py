"""应用配置管理。"""

import json
from pathlib import Path

from wokbee.core.safe_io import safe_write_json

# 全局单例：所有无参 Config() 返回同一实例，内存数据实时共享。
# 避免各页面持有独立副本导致"设置页保存后其他页面读不到最新值"。
_config_instance: "Config | None" = None
_custom_instances: dict[str, "Config"] = {}


class Config:
    """管理应用配置的加载和保存。

    默认单例：`Config()` 返回共享实例，任意页面 set() 后其他页面立即可见。
    传入 config_path 时返回该路径的独立实例。
    """

    DEFAULT_CONFIG = {
        "window": {
            "width": 960,
            "height": 640,
        },
        "theme": "dark",
        "autobee": {
            "workspace_root": "",
            "approval": {
                "skip_read": True,
                "skip_write": False,
                "skip_routine": False,
                "skip_high_risk": False,
            },
            "default_provider": "",
            "default_model_id": "",
            "max_steps": 40,
            "max_parallel_tools": 4,
        },
    }

    def __new__(cls, config_path: str | None = None):
        global _config_instance
        if config_path:
            key = str(Path(config_path))
            if key not in _custom_instances:
                _custom_instances[key] = super().__new__(cls)
            return _custom_instances[key]
        if _config_instance is None:
            _config_instance = super().__new__(cls)
        return _config_instance

    def __init__(self, config_path: str | None = None):
        if config_path:
            self._path = Path(config_path)
        else:
            self._path = Path.home() / ".wokbee" / "config.json"
        # 单例实例只加载一次，避免重复构造覆盖内存中未保存的修改
        if getattr(self, "_loaded", False):
            return
        self._data: dict = {}
        self._load()
        self._loaded = True

    def reload(self):
        """从磁盘重新加载配置（外部修改 config.json 后调用）。"""
        self._data = {}
        self._load()

    def _load(self):
        if self._path.exists():
            try:
                self._data = json.loads(self._path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self._data = {}
        else:
            self._data = {}
        self._merge_defaults(self._data, dict(self.DEFAULT_CONFIG))

    @staticmethod
    def _merge_defaults(data: dict, defaults: dict):
        """递归合并缺省键，不覆盖用户已有嵌套对象中的已有键。"""
        for k, v in defaults.items():
            if k not in data:
                data[k] = json.loads(json.dumps(v)) if isinstance(v, (dict, list)) else v
            elif isinstance(v, dict) and isinstance(data.get(k), dict):
                Config._merge_defaults(data[k], v)

    def save(self):
        safe_write_json(self._path, self._data)

    def get(self, key: str, default=None):
        keys = key.split(".")
        val = self._data
        for k in keys:
            if isinstance(val, dict):
                val = val.get(k)
            else:
                return default
            if val is None:
                return default
        return val

    def set(self, key: str, value):
        keys = key.split(".")
        d = self._data
        for k in keys[:-1]:
            d = d.setdefault(k, {})
        d[keys[-1]] = value
