"""WokBee 统一错误类型体系。"""


class WokBeeError(Exception):
    """所有 WokBee 业务异常的基类。"""


class AIError(WokBeeError):
    """AI 调用相关错误：网络失败、API 异常、模型不可用等。"""


class StorageError(WokBeeError):
    """数据持久化错误：JSON 损坏、写入失败等。"""
