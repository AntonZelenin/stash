"""The limit specification format used by the settings: a comma-separated
list of windows, each "<count>/<length>", e.g. "5/15m,20/1d" (at most 5
per 15 minutes and 20 per day). Lengths are in seconds (s), minutes (m),
hours (h) or days (d). "" means no limit.

Kept free of database imports: the settings validate their limits with it
while they're being built, before the database module has finished
importing."""

import re
from dataclasses import dataclass

_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_WINDOW = re.compile(r"(\d+)/(\d+)([smhd])")


@dataclass(frozen=True)
class Window:
    """At most `limit` (requests, or units such as bytes) per `seconds`."""

    limit: int
    seconds: int


def parse_windows(spec: str) -> tuple[Window, ...]:
    """The windows of `spec`, shortest first. Raises `ValueError` if it's
    malformed, or a window has length 0 or lists the same length twice."""
    windows: dict[int, Window] = {}
    for part in (part.strip() for part in spec.split(",")):
        if not part:
            continue
        match = _WINDOW.fullmatch(part)
        if match is None:
            raise ValueError(f"invalid rate limit window {part!r}; expected e.g. '5/15m'")
        seconds = int(match.group(2)) * _UNIT_SECONDS[match.group(3)]
        if seconds <= 0:
            raise ValueError(f"rate limit window {part!r} has no length")
        if seconds in windows:
            raise ValueError(f"rate limit {spec!r} has two windows of {seconds}s")
        windows[seconds] = Window(limit=int(match.group(1)), seconds=seconds)
    return tuple(sorted(windows.values(), key=lambda window: window.seconds))
