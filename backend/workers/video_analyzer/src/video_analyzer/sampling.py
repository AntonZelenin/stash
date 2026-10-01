"""Where in a video its frames are sampled: a fixed number of frames for its
duration, spread evenly over the whole timeline. No scene detection: one
frame from the middle of each of `n` equal segments.

How many, by duration (`frame_count`):

    up to 30 s           5
    30 s - 2 min         8
    2 - 10 min           12
    10 - 30 min          16
    over 30 min          20 (never more, however long)

capped by `max_frames`, and so that frames are at least `min_gap_seconds`
apart (a 3 s clip gets 3 frames, a 0.5 s one 1). Segment midpoints keep
clear of the first and last moments, which are often black (fades) or past
the last decodable frame.

How long processing takes depends on the number of frames, never on the
duration: a 3-hour film costs what a 30-minute one does."""

# (longest duration in seconds, frames): the first row a duration fits.
_SCHEDULE = ((30.0, 5), (120.0, 8), (600.0, 12), (1800.0, 16))
_LONGEST_VIDEO_FRAMES = 20


def frame_count(duration_seconds: float, *, max_frames: int, min_gap_seconds: float) -> int:
    """How many frames to sample from a video of this duration (at least 1)."""
    count = next((frames for longest, frames in _SCHEDULE if duration_seconds <= longest), _LONGEST_VIDEO_FRAMES)
    count = min(count, max_frames)
    if min_gap_seconds > 0:
        count = min(count, int(duration_seconds // min_gap_seconds))
    return max(1, count)


def sample_times(duration_seconds: float | None, *, max_frames: int, min_gap_seconds: float) -> list[float]:
    """When to sample frames, in seconds from the start, in order: the
    midpoint of each of `frame_count` equal segments of the timeline. An
    unknown (None) or zero duration gets one frame, at the start."""
    if not duration_seconds or duration_seconds <= 0:
        return [0.0]
    count = frame_count(duration_seconds, max_frames=max_frames, min_gap_seconds=min_gap_seconds)
    return [round(duration_seconds * (i + 0.5) / count, 3) for i in range(count)]
