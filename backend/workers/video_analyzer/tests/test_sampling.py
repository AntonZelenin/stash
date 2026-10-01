import pytest

from video_analyzer.sampling import frame_count, sample_times


def _count(duration: float) -> int:
    return frame_count(duration, max_frames=20, min_gap_seconds=1.0)


@pytest.mark.parametrize(
    ("duration", "frames"),
    [
        (12, 5),
        (30, 5),
        (30.5, 8),
        (90, 8),
        (120, 8),
        (121, 12),
        (600, 12),
        (601, 16),
        (1800, 16),
        (1801, 20),
        (4 * 3600, 20),  # however long: never more
    ],
)
def test_frames_by_duration(duration, frames):
    assert _count(duration) == frames


@pytest.mark.parametrize(("duration", "frames"), [(4.5, 4), (3, 3), (1.2, 1), (0.4, 1), (0.04, 1)])
def test_short_videos_get_frames_at_least_the_gap_apart_and_always_one(duration, frames):
    assert _count(duration) == frames


def test_max_frames_caps_the_schedule():
    assert frame_count(3600, max_frames=10, min_gap_seconds=1.0) == 10
    assert frame_count(10, max_frames=3, min_gap_seconds=1.0) == 3


def test_frames_are_the_midpoints_of_equal_segments_of_the_whole_timeline():
    assert sample_times(30, max_frames=20, min_gap_seconds=1.0) == [3.0, 9.0, 15.0, 21.0, 27.0]


def test_long_video_samples_span_it_evenly_without_its_first_and_last_moments():
    duration = 2 * 3600
    times = sample_times(duration, max_frames=20, min_gap_seconds=1.0)

    assert len(times) == 20
    assert times == sorted(times)
    assert times[0] == pytest.approx(duration / 40)
    assert times[-1] == pytest.approx(duration - duration / 40)
    gaps = {round(b - a, 3) for a, b in zip(times, times[1:])}
    assert gaps == {duration / 20}


@pytest.mark.parametrize("duration", [5, 30, 119, 599, 3000])
def test_samples_are_never_closer_than_the_gap(duration):
    times = sample_times(duration, max_frames=20, min_gap_seconds=1.0)

    assert all(b - a >= 1.0 for a, b in zip(times, times[1:]))
    assert 0 < times[0] and times[-1] < duration


def test_a_very_short_video_gets_its_middle_frame():
    assert sample_times(0.5, max_frames=20, min_gap_seconds=1.0) == [0.25]


@pytest.mark.parametrize("duration", [None, 0, -1])
def test_unknown_duration_gets_one_frame_at_the_start(duration):
    assert sample_times(duration, max_frames=20, min_gap_seconds=1.0) == [0.0]
