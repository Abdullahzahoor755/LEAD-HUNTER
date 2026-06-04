from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class _FakeResponse:
    payload: dict[str, Any]
    is_success: bool = True
    status_code: int = 200
    headers: dict[str, str] | None = None

    @property
    def text(self) -> str:
        return "{}"

    def json(self) -> dict[str, Any]:
        return self.payload


def test_dashboard_run_once_sends_job_type(monkeypatch) -> None:
    import dashboard

    calls: list[tuple[str, str, dict[str, Any]]] = []

    def fake_api_request(method: str, path: str, **kwargs: Any) -> _FakeResponse:
        calls.append((method, path, kwargs))
        return _FakeResponse(payload={"status": "completed"})

    monkeypatch.setattr(dashboard, "api_request", fake_api_request)

    dashboard.enqueue_job("lead_generation", {"limit": 10}, run_now=True)
    dashboard.enqueue_job("outreach", run_now=True)

    run_calls = [call for call in calls if call[1] == "/jobs/run-once"]
    assert run_calls[0][2]["json"] == {"job_type": "lead_generation"}
    assert run_calls[1][2]["json"] == {"job_type": "outreach"}
