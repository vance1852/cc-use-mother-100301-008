"""极地科考站协作基础服务的服务端基础包。"""

from .energy_service import EnergyService
from .service import DomainService

__all__ = ["DomainService", "EnergyService"]
