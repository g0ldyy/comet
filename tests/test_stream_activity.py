from comet.api.v1.stream_activity import activity_window


def test_auto_activity_window_follows_fresh_usage_instead_of_retention():
    window = activity_window(10_000, "auto", 9_970)

    assert window.bucket_seconds == 15
    assert window.bucket_count <= 5
    assert window.started_at <= 9_970


def test_auto_activity_window_scales_granularity_with_age():
    window = activity_window(700_000, "auto", 700_000 - 3 * 24 * 60 * 60)

    assert window.bucket_seconds == 2 * 60 * 60
    assert window.bucket_count <= 85


def test_fixed_activity_window_keeps_a_bounded_number_of_points():
    window = activity_window(700_000, "7d", 699_990)

    assert window.bucket_seconds == 2 * 60 * 60
    assert window.bucket_count <= 85


def test_activity_window_does_not_create_an_empty_boundary_bucket():
    window = activity_window(10_800, "15m", None)

    assert window.bucket_count == 60
    assert window.started_at + window.bucket_count * window.bucket_seconds == 10_800


def test_auto_activity_window_tolerates_replica_clock_skew():
    window = activity_window(10_000, "auto", 10_030)

    assert window.bucket_count > 0
    assert window.started_at <= window.ended_at
