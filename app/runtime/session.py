"""Singleton runtime/session management for API and worker processes."""

from __future__ import annotations

from dataclasses import dataclass
from threading import Lock

from app.api.app import ApiApplication, create_fastapi_app
from app.db.factory import build_session
from app.db.session import DatabaseSession
from app.workers.runner import build_job_queue


@dataclass(slots=True)
class SharedRuntime:
    db: DatabaseSession
    api: ApiApplication
    queue: object
    web_app: object


_RUNTIME: SharedRuntime | None = None
_LOCK = Lock()


def get_runtime() -> SharedRuntime:
    global _RUNTIME
    if _RUNTIME is None:
        with _LOCK:
            if _RUNTIME is None:
                db = build_session()
                _RUNTIME = SharedRuntime(
                    db=db,
                    api=ApiApplication(db=db),
                    queue=build_job_queue(db),
                    web_app=create_fastapi_app(db=db),
                )
    return _RUNTIME


def reset_runtime() -> None:
    global _RUNTIME
    with _LOCK:
        _RUNTIME = None
