"""Database backend selection."""

from __future__ import annotations

import logging

from app.configs.settings import settings
from app.db.postgres import build_postgres_session
from app.db.session import DatabaseSession

LOGGER = logging.getLogger(__name__)


def build_session() -> DatabaseSession:
    if settings.database_backend == "postgres":
        return build_postgres_session()
    raise RuntimeError("Production runtime requires DATABASE_BACKEND=postgres.")
