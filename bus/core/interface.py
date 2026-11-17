"""后端抽象接口

定义后端会话管理的统一接口，串口/网络/Modbus 后端继承此基类。
"""

from abc import ABC, abstractmethod
from typing import Any


class BackendInterface(ABC):
    """后端会话抽象基类"""

    @abstractmethod
    async def connect(self, **kwargs: Any) -> bool:
        """建立连接"""
        ...

    @abstractmethod
    async def disconnect(self) -> bool:
        """断开连接"""
        ...

    @abstractmethod
    async def send(self, data: bytes) -> int:
        """发送数据，返回发送字节数"""
        ...

    @abstractmethod
    def get_status(self) -> dict[str, Any]:
        """获取当前会话状态"""
        ...

    @property
    @abstractmethod
    def is_connected(self) -> bool:
        """是否已连接"""
        ...
