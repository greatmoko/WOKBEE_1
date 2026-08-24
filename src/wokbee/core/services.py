"""统一服务注册与生命周期管理。

所有 Manager 实例在 ServiceRegistry 中集中创建，View 通过 registry 获取依赖，
而不是各自 new 出独立实例。
"""

from __future__ import annotations

from wokbee.core.config import Config
from wokbee.core.ai_role import AIRoleManager


class ServiceRegistry:
    """集中管理所有核心 Manager 的创建和访问。

    每个 manager 按需创建（lazy），但整个应用生命周期只有一个实例。
    """

    def __init__(self, config: Config | None = None):
        self._config = config or Config()
        self._role_manager: AIRoleManager | None = None

    @property
    def config(self) -> Config:
        return self._config

    @property
    def role_manager(self) -> AIRoleManager:
        if self._role_manager is None:
            self._role_manager = AIRoleManager()
        return self._role_manager
