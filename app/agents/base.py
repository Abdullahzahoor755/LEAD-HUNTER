"""Base classes for pluggable tenant-aware agents."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict

from app.core.models import TenantContext
from app.db.session import AsyncDatabaseSession, DatabaseSession


@dataclass(slots=True)
class AgentRequest:
    tenant: TenantContext
    payload: Dict[str, Any] = field(default_factory=dict)


class BaseAgent(ABC):
    name = "base"

    @abstractmethod
    async def run(self, request: AgentRequest, db: DatabaseSession | AsyncDatabaseSession) -> Dict[str, Any]:
        raise NotImplementedError
