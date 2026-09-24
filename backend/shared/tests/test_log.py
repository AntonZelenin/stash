import io
import json
import logging
from uuid import UUID

import pytest

from stash_shared import log

ITEM_ID = UUID("00000000-0000-0000-0000-000000000001")


@pytest.fixture
def output():
    """Configures logging into a buffer, restoring the previous setup after."""
    root = logging.getLogger()
    saved_handlers, saved_level, saved_backend = list(root.handlers), root.level, log._backend
    buffer = io.StringIO()

    def configure(platform: str, environment: str = "prod", level: str = "DEBUG") -> io.StringIO:
        log.configure_logging(service="test-service", platform=platform, environment=environment, level=level)
        [handler] = root.handlers
        handler.setStream(buffer)
        return buffer

    yield configure

    root.handlers[:] = saved_handlers
    root.setLevel(saved_level)
    log._backend = saved_backend


def _json_lines(buffer: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in buffer.getvalue().splitlines() if line.startswith("{")]


def test_json_output_has_common_and_given_fields(output):
    buffer = output("digitalocean")

    log.get_logger("stash.test").info("Item created", item_id=ITEM_ID, item_type="image", skipped=None)

    [entry] = _json_lines(buffer)
    assert entry["message"] == "Item created"
    assert entry["level"] == "INFO"
    assert entry["service"] == "test-service"
    assert entry["platform"] == "digitalocean"
    assert entry["environment"] == "prod"
    assert entry["logger"] == "stash.test"
    assert entry["timestamp"].endswith("Z")
    assert entry["item_id"] == str(ITEM_ID)
    assert entry["item_type"] == "image"
    assert "skipped" not in entry


def test_local_output_is_one_readable_line(output):
    buffer = output("local")

    log.get_logger("stash.test").warning("Job attempt failed; retrying", attempt=2, reason="has spaces")

    line = buffer.getvalue().strip()
    assert "WARNING" in line
    assert "stash.test: Job attempt failed; retrying" in line
    assert 'attempt=2 reason="has spaces"' in line


def test_exception_keeps_stack_trace_and_error_type(output):
    buffer = output("digitalocean")

    try:
        raise TimeoutError("OpenAI timed out")
    except TimeoutError:
        log.get_logger("stash.test").exception("Job failed")

    [entry] = _json_lines(buffer)
    assert entry["level"] == "ERROR"
    assert entry["error_type"] == "TimeoutError"
    assert "Traceback" in entry["exception"]
    assert "OpenAI timed out" in entry["exception"]


def test_exc_info_accepts_an_exception_instance(output):
    buffer = output("digitalocean")

    log.get_logger("stash.test").warning("Retrying", exc_info=ValueError("bad"))

    [entry] = _json_lines(buffer)
    assert entry["error_type"] == "ValueError"
    assert "ValueError: bad" in entry["exception"]


def test_context_is_added_to_records_and_restored_after_the_block(output):
    buffer = output("digitalocean")
    logger = log.get_logger("stash.test")

    with log.log_context(job_id="1-0", item_id=ITEM_ID):
        with log.log_context(attempt=3):
            logger.info("inner")
        logger.info("outer", job_id="explicit wins")
    logger.info("after")

    inner, outer, after = _json_lines(buffer)
    assert (inner["job_id"], inner["attempt"], inner["item_id"]) == ("1-0", 3, str(ITEM_ID))
    assert outer["job_id"] == "explicit wins"
    assert "attempt" not in outer
    assert "job_id" not in after and "item_id" not in after


def test_bind_context_is_visible_to_the_enclosing_block(output):
    buffer = output("digitalocean")

    def authenticate():
        log.bind_context(user_id="u1")

    with log.log_context(request_id="r1"):
        authenticate()
        log.get_logger("stash.test").info("Request completed")
    log.get_logger("stash.test").info("after")

    completed, after = _json_lines(buffer)
    assert (completed["request_id"], completed["user_id"]) == ("r1", "u1")
    assert "user_id" not in after


def test_third_party_records_get_the_context(output):
    buffer = output("digitalocean")

    with log.log_context(item_id=ITEM_ID):
        logging.getLogger("some.library").warning("library says %s", "hi")

    [entry] = _json_lines(buffer)
    assert entry["message"] == "library says hi"
    assert entry["item_id"] == str(ITEM_ID)


def test_level_filters_records(output):
    buffer = output("digitalocean", level="WARNING")

    log.get_logger("stash.test").info("hidden")
    log.get_logger("stash.test").warning("shown")

    assert [entry["message"] for entry in _json_lines(buffer)] == ["shown"]


def test_logged_call_logs_success_with_duration_and_result(output):
    buffer = output("digitalocean")
    logger = log.get_logger("stash.test")

    with log.logged_call(logger, "openai.responses", model="m") as call:
        call.update(output_chars=12)

    [started, succeeded] = _json_lines(buffer)
    assert started["level"] == "DEBUG"
    assert succeeded["message"] == "External call succeeded"
    assert succeeded["level"] == "INFO"
    assert (succeeded["operation"], succeeded["model"], succeeded["output_chars"]) == ("openai.responses", "m", 12)
    assert succeeded["duration_ms"] >= 0


def test_logged_call_logs_failure_and_reraises(output):
    buffer = output("digitalocean")

    class RateLimited(Exception):
        status_code = 429

    with pytest.raises(RateLimited):
        with log.logged_call(log.get_logger("stash.test"), "openai.responses"):
            raise RateLimited()

    failed = _json_lines(buffer)[-1]
    assert failed["message"] == "External call failed"
    assert failed["level"] == "WARNING"
    assert (failed["error_type"], failed["status_code"]) == ("RateLimited", 429)
    assert "exception" not in failed


def test_aws_platform_without_powertools_falls_back_to_standard_logging(output, monkeypatch, capsys):
    import builtins

    real_import = builtins.__import__

    def no_powertools(name, *args, **kwargs):
        if name.startswith("aws_lambda_powertools"):
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_powertools)
    buffer = output("aws", environment="stage")
    log.get_logger("stash.test").info("still logged")

    # Logged while configuring, before the fixture redirects the handler.
    assert "powertools is not installed" in capsys.readouterr().out
    [entry] = _json_lines(buffer)
    assert entry["message"] == "still logged"
    assert (entry["platform"], entry["environment"]) == ("aws", "stage")


@pytest.mark.parametrize("environment", ["local", "dev", "stage", "prod"])
def test_environment_only_labels_records_and_platform_picks_the_format(output, environment):
    local = output("local", environment=environment)
    log.get_logger("stash.test").info("readable")
    assert local.getvalue().split(" ", 2)[1] == "INFO"  # console line, not JSON

    json_buffer = output("digitalocean", environment=environment)
    log.get_logger("stash.test").info("structured")
    [entry] = _json_lines(json_buffer)
    assert entry["environment"] == environment
