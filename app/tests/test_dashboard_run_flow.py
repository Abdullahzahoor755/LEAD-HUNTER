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


class _NullContext:
    def __enter__(self) -> "_NullContext":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False


class _TabsContext:
    def __init__(self) -> None:
        self.items = (_NullContext(), _NullContext())

    def __iter__(self):
        return iter(self.items)


class _FakeStreamlit:
    def __init__(self) -> None:
        self.session_state: dict[str, Any] = {}
        self.text_input_calls: list[tuple[str, dict[str, Any]]] = []

    def markdown(self, *args: Any, **kwargs: Any) -> None:
        return None

    def columns(self, *args: Any, **kwargs: Any):
        return _NullContext(), _NullContext()

    def tabs(self, *args: Any, **kwargs: Any):
        return _TabsContext()

    def form(self, *args: Any, **kwargs: Any):
        return _NullContext()

    def text_input(self, label: str, **kwargs: Any) -> str:
        self.text_input_calls.append((label, kwargs))
        return ""

    def form_submit_button(self, *args: Any, **kwargs: Any) -> bool:
        return False

    def error(self, *args: Any, **kwargs: Any) -> None:
        return None

    def rerun(self) -> None:
        return None

    def button(self, *args: Any, **kwargs: Any) -> bool:
        return False


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


def test_auth_forms_do_not_prefill_admin_credentials(monkeypatch) -> None:
    import dashboard

    fake_st = _FakeStreamlit()
    monkeypatch.setattr(dashboard, "st", fake_st)
    monkeypatch.setattr(dashboard, "api_request", lambda *args, **kwargs: _FakeResponse(payload={}))

    dashboard.require_login()

    fields = {label: kwargs for label, kwargs in fake_st.text_input_calls}

    assert fields["Tenant ID"].get("value", "") == ""
    assert fields["Email"].get("value", "") == ""
    assert fields["Password"].get("value", "") == ""
    assert fields["Workspace / Tenant ID"].get("value", "") == ""
    assert fields["Business Name"].get("value", "") == ""
    assert fields["Work Email"].get("value", "") == ""
    assert fields["Your Name"].get("value", "") == ""
    assert fields["Password"].get("type") == "password"
