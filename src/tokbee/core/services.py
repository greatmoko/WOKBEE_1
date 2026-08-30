"""统一服务注册与生命周期管理。

所有 Manager 实例在 ServiceRegistry 中集中创建，View 通过 registry 获取依赖，
而不是各自 new 出独立实例。
"""

from __future__ import annotations

from tokbee.core.config import Config
from tokbee.core.ai_role import AIRoleManager


class ServiceRegistry:
    """集中管理所有核心 Manager 的创建和访问。

    每个 manager 按需创建（lazy），但整个应用生命周期只有一个实例。
    """

    def __init__(self, config: Config | None = None):
        self._config = config or Config()
        self._role_manager: AIRoleManager | None = None
        self._wokbee_settings = None
        self._wokbee_store = None
        self._autobee_store = None
        self._autobee_executor = None
        self._autobee_scheduler = None
        self._gateway_manager = None

    @property
    def config(self) -> Config:
        return self._config

    @property
    def role_manager(self) -> AIRoleManager:
        if self._role_manager is None:
            self._role_manager = AIRoleManager()
        return self._role_manager

    @property
    def wokbee_settings(self):
        if self._wokbee_settings is None:
            from wokbee.core.settings import WokBeeSettings
            self._wokbee_settings = WokBeeSettings(self._config)
        return self._wokbee_settings

    @property
    def wokbee_store(self):
        if self._wokbee_store is None:
            from wokbee.core.project_store import ProjectStore
            self._wokbee_store = ProjectStore(self.wokbee_settings)
        return self._wokbee_store

    @property
    def autobee_store(self):
        if self._autobee_store is None:
            from autobee.core.store import AutoBeeStore
            self._autobee_store = AutoBeeStore()
        return self._autobee_store

    @property
    def autobee_executor(self):
        if self._autobee_executor is None:
            from tokbee.core.provider_store import ProviderStore
            from autobee.engine.executor import TaskExecutor
            self._autobee_executor = TaskExecutor(
                store=self.autobee_store,
                project_store=self.wokbee_store,
                provider_store=ProviderStore(),
                settings=self.wokbee_settings,
            )
        return self._autobee_executor

    @property
    def autobee_scheduler(self):
        if self._autobee_scheduler is None:
            from autobee.engine.scheduler import SchedulerService
            self._autobee_scheduler = SchedulerService(
                store=self.autobee_store, executor=self.autobee_executor,
            )
        return self._autobee_scheduler

    @property
    def gateway_manager(self):
        if self._gateway_manager is None:
            from tokbee.core.provider_store import ProviderStore
            from wokbee.gateway.manager import GatewayManager
            from wokbee.gateway.store import GatewayStore
            self._gateway_manager = GatewayManager(
                store=GatewayStore(self._config),
                settings=self.wokbee_settings,
                project_store=self.wokbee_store,
                provider_store=ProviderStore(),
                parent=None,
            )
        return self._gateway_manager
