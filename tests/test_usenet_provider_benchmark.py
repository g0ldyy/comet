import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from scripts.run_usenet_provider_benchmark import (
    BenchmarkUnavailable,
    Candidate,
    NntpConnection,
    Sample,
    assert_engine_idle,
    recommend_knee,
    take_bounded,
)


def sample(value: int, mib_per_second: int) -> Sample:
    return Sample(
        "pipeline",
        value,
        1,
        1,
        0,
        mib_per_second * 1024 * 1024,
        1_000_000_000,
    )


def test_recommendation_uses_the_smallest_setting_within_five_percent():
    assert (
        recommend_knee(
            [
                sample(1, 5),
                sample(2, 9),
                sample(4, 10),
                sample(8, 10),
            ]
        )
        == 4
    )


def test_recommendation_is_inconclusive_until_every_setting_is_equally_sampled():
    assert (
        recommend_knee(
            [
                sample(1, 5),
                sample(1, 6),
                sample(2, 9),
                sample(2, 10),
                sample(4, 11),
            ],
            expected_values=[1, 2, 4],
            required_samples=2,
        )
        is None
    )


def test_candidate_admission_is_byte_bounded_and_never_reuses_a_candidate():
    candidates = [
        Candidate(f"<{index}@example.test>".encode(), size)
        for index, size in enumerate((4, 8, 16, 2))
    ]

    selected, cursor, remaining = take_bounded(candidates, 0, 2, 10)
    second, second_cursor, second_remaining = take_bounded(
        candidates,
        cursor,
        2,
        remaining,
    )

    assert [candidate.declared_bytes for candidate in selected] == [4, 2]
    assert second == []
    assert second_cursor == len(candidates)
    assert second_remaining == 4


def test_group_discovery_ranks_the_server_catalog_without_name_assumptions():
    connection = NntpConnection.__new__(NntpConnection)
    commands = []
    statuses = iter(
        [
            (215, b"215 active groups follow"),
            (211, b"211 42 100 141 future+hierarchy"),
        ]
    )
    connection._send = commands.append
    connection._status = lambda: next(statuses)
    connection._multiline = lambda **_kwargs: iter(
        [
            b"small.group 5 1 y",
            b"future+hierarchy 141 100 y",
        ]
    )

    groups = connection.test_groups()
    assert groups == [b"future+hierarchy", b"small.group"]
    assert connection.select_group(groups[0]) == (100, 141)
    assert commands == [b"LIST ACTIVE", b"GROUP future+hierarchy"]


def test_idle_probe_does_not_count_its_own_stats_request_as_active_work():
    snapshot = {
        "requests_active": 1,
        "session_prefetches_active": 0,
        "nntp_connections_open": 0,
        "nntp_connections_active": 0,
        "nntp_queue_interactive": 0,
        "nntp_queue_preparation": 0,
        "nntp_queue_background": 0,
    }
    with patch(
        "scripts.run_usenet_provider_benchmark.EngineClient.stats",
        new=AsyncMock(return_value=snapshot),
    ):
        asyncio.run(assert_engine_idle())

    snapshot["requests_active"] = 2
    with (
        patch(
            "scripts.run_usenet_provider_benchmark.EngineClient.stats",
            new=AsyncMock(return_value=snapshot),
        ),
        pytest.raises(BenchmarkUnavailable),
    ):
        asyncio.run(assert_engine_idle())
