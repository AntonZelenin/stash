"""Every request is measured by route template and method — rate, latency,
4xx and 5xx — without the concrete path or any id in the dimensions."""

from collections import Counter
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from stash_shared import metrics

from app.request_metrics import RequestMetricsMiddleware
from helpers import register_and_login


class _Recorder(metrics._Backend):
    def __init__(self):
        self.points: list[tuple[str, float, dict[str, str]]] = []

    def record(self, name, unit, value, dimensions, *, summed):
        self.points.append((name, value, dict(dimensions)))

    def requests(self) -> Counter:
        """(metric, route, method) -> count, request metrics only."""
        return Counter(
            (name, dims["route"], dims["method"]) for name, _value, dims in self.points if name.startswith("Request")
        )


@pytest.fixture
def recorded(monkeypatch) -> _Recorder:
    recorder = _Recorder()
    monkeypatch.setattr(metrics, "_backend", recorder)
    return recorder


async def test_route_template_not_the_path(client, recorded):
    _, token = await register_and_login(client)
    recorded.points.clear()

    response = await client.delete(f"/items/{uuid4()}", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 404
    # The route template, never the concrete path with the item id.
    assert recorded.requests() == {
        ("Requests", "/items/{item_id}", "DELETE"): 1,
        ("Requests4xx", "/items/{item_id}", "DELETE"): 1,
        ("RequestDuration", "/items/{item_id}", "DELETE"): 1,
    }


async def test_ok_and_client_errors(client, recorded):
    _, token = await register_and_login(client)
    recorded.points.clear()

    await client.get("/items", headers={"Authorization": f"Bearer {token}"})
    await client.get("/items")  # 401

    assert recorded.requests() == {
        ("Requests", "/items", "GET"): 2,
        ("Requests4xx", "/items", "GET"): 1,
        ("RequestDuration", "/items", "GET"): 2,
    }
    [duration, _] = [value for name, value, _dims in recorded.points if name == "RequestDuration"]
    assert duration >= 0


async def test_server_error(client, embedder, recorded):
    embedder.fail = True
    _, token = await register_and_login(client)
    recorded.points.clear()

    response = await client.post("/search", json={"query": "cat"}, headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 503
    assert recorded.requests()[("Requests5xx", "/search", "POST")] == 1
    assert ("Requests4xx", "/search", "POST") not in recorded.requests()


async def test_unmatched_paths_and_unknown_methods_share_one_bucket(client, recorded):
    await client.get("/wp-admin/setup.php")
    await client.request("PROPFIND", "/.env")

    assert recorded.requests() == {
        ("Requests", "unmatched", "GET"): 1,
        ("Requests4xx", "unmatched", "GET"): 1,
        ("RequestDuration", "unmatched", "GET"): 1,
        ("Requests", "unmatched", "OTHER"): 1,
        ("Requests4xx", "unmatched", "OTHER"): 1,
        ("RequestDuration", "unmatched", "OTHER"): 1,
    }


async def test_healthcheck_is_not_counted(client, recorded):
    await client.get("/health")

    assert recorded.points == []


async def test_unhandled_exception_counts_as_server_error(recorded):
    async def failing_app(scope, receive, send):
        raise RuntimeError("boom")

    transport = ASGITransport(app=RequestMetricsMiddleware(failing_app), raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.get("/items")

    assert recorded.requests() == {
        ("Requests", "unmatched", "GET"): 1,
        ("Requests5xx", "unmatched", "GET"): 1,
        ("RequestDuration", "unmatched", "GET"): 1,
    }
