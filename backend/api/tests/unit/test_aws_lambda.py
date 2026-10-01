"""The Lambda handler serves the app for API Gateway (HTTP API) events."""

import pytest

from app import aws_lambda
from app.storage import tasks
from app.storage.reconciliation import ReconciliationResult


def _http_api_event(method: str, path: str) -> dict:
    return {
        "version": "2.0",
        "routeKey": "$default",
        "rawPath": path,
        "rawQueryString": "",
        "headers": {"host": "api.example.com"},
        "requestContext": {
            "http": {"method": method, "path": path, "protocol": "HTTP/1.1", "sourceIp": "203.0.113.1"},
            "domainName": "api.example.com",
            "stage": "$default",
        },
        "isBase64Encoded": False,
    }


def test_handler_serves_the_app():
    response = aws_lambda.handler(_http_api_event("GET", "/health"), None)

    assert response["statusCode"] == 200
    assert response["body"] == '{"status":"ok"}'


def test_handler_flushes_metrics_traces_and_analytics(monkeypatch):
    flushed = []
    monkeypatch.setattr(aws_lambda.metrics, "flush", lambda: flushed.append("metrics"))
    monkeypatch.setattr(aws_lambda.tracing, "flush", lambda: flushed.append("tracing"))

    class Analytics:
        def flush(self):
            flushed.append("analytics")

    monkeypatch.setattr(aws_lambda, "get_analytics", lambda: Analytics())

    aws_lambda.handler(_http_api_event("GET", "/health"), None)

    # Before the handler returns: the environment may be frozen after.
    assert flushed == ["metrics", "tracing", "analytics"]


@pytest.mark.parametrize("task", [tasks.DRAIN_STORAGE_DELETIONS, tasks.RECONCILE_STORAGE])
def test_scheduled_events_run_their_task(monkeypatch, task):
    ran = []

    async def run_task(name, engine):
        ran.append(name)
        return {"done": True}

    monkeypatch.setattr(aws_lambda, "run_task", run_task)

    assert aws_lambda.handler({"task": task}, None) == {"done": True}
    assert ran == [task]


def test_drain_task_drains_pending_storage_deletions(monkeypatch):
    class FakeDrainer:
        def __init__(self, engine, storage):
            pass

        async def drain(self) -> int:
            return 3

    monkeypatch.setattr(tasks, "StorageDeletionDrainer", FakeDrainer)
    monkeypatch.setattr(tasks, "get_object_storage", lambda: object())

    assert aws_lambda.handler({"task": tasks.DRAIN_STORAGE_DELETIONS}, None) == {"finished": 3}


def test_reconcile_task_reports_what_it_did(monkeypatch):
    class FakeReconciler:
        def __init__(self, engine, storage):
            pass

        async def reconcile(self) -> ReconciliationResult:
            return ReconciliationResult(scanned=10, deleted=2, pass_completed=True)

    monkeypatch.setattr(tasks, "StorageReconciler", FakeReconciler)
    monkeypatch.setattr(tasks, "get_object_storage", lambda: object())

    assert aws_lambda.handler({"task": tasks.RECONCILE_STORAGE}, None) == {
        "scanned": 10,
        "deleted": 2,
        "pass_completed": True,
        "skipped": False,
    }
