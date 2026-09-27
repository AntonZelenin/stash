"""Scrubs credentials and secret-bearing values out of what leaves a process
as logs or traces. Applied centrally where records and spans are written
(`stash_shared.log`'s formatters, `stash_shared.tracing`'s exporter), so no
call site has to remember it.

It's a backstop, not the policy: application code still never logs user
content (see "Logging" in the architecture doc). What it catches is what
slips in by accident, mostly inside exception messages and third-party
records:

- fields and attributes with a sensitive name (`password`, `authorization`,
  `cookie`, `*_token`, `*secret*`, `api_key`...): the whole value;
- `Bearer`/`Basic` credentials, OpenAI API keys, and the password in a
  connection URL (`postgresql://user:<password>@host`);
- `key=value` / `"key": "value"` pairs with a sensitive key, e.g. in a
  repr'd dict or a query string;
- URL query strings: pre-signed S3 URLs carry their signature and
  credentials there (`X-Amz-Signature`, `X-Amz-Credential`,
  `X-Amz-Security-Token`), and other query strings may carry user input;
- SQLAlchemy's `[parameters: ...]`: bound values are user content
  (engines are also created with `hide_parameters=True`); and a role's
  `PASSWORD '...'` literal in a statement.

It can't recognise user content in free text (a document's words quoted in
a parser's error look like any other words): code handling user content
must keep it out of messages itself, e.g.
`stash_worker_core.errors.content_safe_cause` for parser errors.
"""

import re
from collections.abc import Mapping
from typing import Any

REDACTED = "[REDACTED]"

# Names (of fields, attributes, headers, query parameters) whose value is
# always secret, matched case-insensitively anywhere in the name, with `-`
# and `.` read as `_` (so `Set-Cookie`, `http.request.header.authorization`
# and `turnstile_token` all match). Not a bare "token", "session" or "key":
# `max_output_tokens`, `session_id` and `storage_key` are metadata.
_SENSITIVE_NAME = re.compile(
    r"password|passwd|secret|api_?key|authorization|cookie|credential|signature|private_key"
    r"|(?:^|_)(?:access|refresh|id|auth|security|session|turnstile|csrf|bearer)?_?token(?:$|_)"
    r"|cf_turnstile_response",
    re.IGNORECASE,
)

# The same names in free text, as `name=value`, `name: value` or
# `"name": "value"`; the value runs to the closing quote, or else to the
# next separator.
_SENSITIVE_PAIR = re.compile(
    r"""(?P<name>\b[\w.-]*?(?:password|passwd|secret|api[_-]?key|authorization|cookie|credential|signature"""
    r"""|private[_-]?key|token|cf-turnstile-response)\b["']?\s*[:=]\s*)"""
    r"""(?!\[REDACTED\])(?:"[^"]*"|'[^']*'|[^\s"',;&)}\]]+)""",
    re.IGNORECASE,
)
# A `Cookie:`/`Set-Cookie:` header line: every cookie in it, to the line's end.
_COOKIE_HEADER = re.compile(r"(?P<name>\b(?:set-)?cookie:(?![ \t]*\[REDACTED\])[ \t]*)[^\r\n]+", re.IGNORECASE)
_AUTH_SCHEME = re.compile(r"\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE)
_OPENAI_KEY = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}")
# scheme://user:password@host: keeps the user, drops the password.
_URL_PASSWORD = re.compile(r"(?P<head>\b[a-z][a-z0-9+.-]*://[^\s:/@'\"]+:)[^\s@/'\"]+@", re.IGNORECASE)
_URL_QUERY = re.compile(r"(?P<url>\b[a-z][a-z0-9+.-]*://[^\s?#'\"<>]+)\?[^\s#'\"<>]*", re.IGNORECASE)
_SQL_PARAMETERS = re.compile(r"\[parameters: .*?\](?=\n|$|\s*\(Background)", re.DOTALL)
# `ALTER ROLE ... PASSWORD '<verifier>'` (`app.db_roles`): a literal in the
# statement itself, so in SQL errors and SQL spans' `db.statement`.
_SQL_PASSWORD = re.compile(r"\b(PASSWORD\s+)'(?:[^']|'')*'", re.IGNORECASE)

# Span attributes holding a request's URL or target: their query string is
# left out (`GET /tags?query=...` carries what the user typed).
_URL_ATTRIBUTES = frozenset({"http.url", "url.full", "http.target"})
_QUERY_ATTRIBUTES = frozenset({"url.query"})


def is_sensitive_name(name: str) -> bool:
    return bool(_SENSITIVE_NAME.search(name.replace("-", "_").replace(".", "_")))


def redact_text(text: str) -> str:
    """`text` with credentials, secret-bearing values and URL query strings
    replaced by `REDACTED`. Everything else is kept as it is."""
    if not text:
        return text
    text = _SQL_PARAMETERS.sub(f"[parameters: {REDACTED}]", text)
    text = _SQL_PASSWORD.sub(lambda match: f"{match[1]}'{REDACTED}'", text)
    text = _URL_PASSWORD.sub(lambda match: f"{match['head']}{REDACTED}@", text)
    text = _URL_QUERY.sub(lambda match: f"{match['url']}?{REDACTED}", text)
    text = _AUTH_SCHEME.sub(lambda match: f"{match[1]} {REDACTED}", text)
    text = _OPENAI_KEY.sub(f"sk-{REDACTED}", text)
    text = _COOKIE_HEADER.sub(lambda match: f"{match['name']}{REDACTED}", text)
    return _SENSITIVE_PAIR.sub(lambda match: f"{match['name']}{REDACTED}", text)


def redact_url(url: str) -> str:
    """`url` without its query string, fragment or password: what's safe
    to log of e.g. a pre-signed URL."""
    without_query = url.split("#", 1)[0].split("?", 1)[0]
    return _URL_PASSWORD.sub(lambda match: f"{match['head']}{REDACTED}@", without_query)


def redact_value(value: Any) -> Any:
    """`value` with every string in it scrubbed (`redact_text`) and, in
    mappings, the values of sensitive names replaced whole."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, BaseException):
        # As its message: it would be written as `str(value)` anyway (e.g.
        # Powertools' `stack_trace.value`), after this.
        return redact_text(str(value))
    if isinstance(value, Mapping):
        return redact_fields(value)
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_value(item) for item in value)
    return value


def redact_fields(fields: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: REDACTED if is_sensitive_name(str(key)) and value is not None else redact_value(value)
        for key, value in fields.items()
    }


def redact_span_attributes(attributes: Mapping[str, Any] | None) -> dict[str, Any]:
    """`redact_fields` for span attributes, also leaving the query string
    out of the request URL attributes set by HTTP instrumentation."""
    redacted = redact_fields(attributes or {})
    for key in _URL_ATTRIBUTES & redacted.keys():
        value = redacted[key]
        if isinstance(value, str) and "?" in value:
            redacted[key] = value.split("?", 1)[0] + "?" + REDACTED
    for key in _QUERY_ATTRIBUTES & redacted.keys():
        if redacted[key]:
            redacted[key] = REDACTED
    return redacted
