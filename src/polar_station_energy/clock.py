"""为能源服务提供可推进的手工时钟，满足可控制的时间基准。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


class ManualClock:
    """测试与离线验收使用的可推进 UTC 时钟。"""

    def __init__(self, value: datetime) -> None:
        if value.tzinfo is None:
            raise ValueError("手工时钟必须包含时区")
        self._value = value.astimezone(timezone.utc)

    def now(self) -> datetime:
        """返回当前设定的 UTC 时间。"""

        return self._value

    def set(self, value: datetime) -> None:
        """直接设定当前时间。"""

        if value.tzinfo is None:
            raise ValueError("手工时钟必须包含时区")
        self._value = value.astimezone(timezone.utc)

    def advance(self, **kwargs) -> None:
        """按 timedelta 参数推进当前时间。"""

        self._value = self._value + timedelta(**kwargs)
