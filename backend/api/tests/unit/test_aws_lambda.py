"""The Lambda handler serves the app for API Gateway (HTTP API) events."""

from app import aws_lambda


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


def test_handler_flushes_metrics_and_traces(monkeypatch):
    flushed = []
    monkeypatch.setattr(aws_lambda.metrics, "flush", lambda: flushed.append("metrics"))
    monkeypatch.setattr(aws_lambda.tracing, "flush", lambda: flushed.append("tracing"))

    aws_lambda.handler(_http_api_event("GET", "/health"), None)

    assert flushed == ["metrics", "tracing"]
