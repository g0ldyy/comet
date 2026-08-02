#!/usr/bin/env python3
"""Bounded, read-only NNTP calibration with anonymous aggregate output."""

from __future__ import annotations

import argparse
import asyncio
import heapq
import random
import socket
import ssl
import statistics
import sys
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from comet.core.models import settings
from comet.usenet.engine_client import EngineClient, EngineUnavailable

MIN_ARTICLE_BYTES = 16 * 1024
MAX_ARTICLE_BYTES = 1024 * 1024
MAX_OVERVIEW_ARTICLES = 5000
MAX_OVERVIEW_BYTES = 24 * 1024 * 1024
MAX_ACTIVE_BYTES = 64 * 1024 * 1024
MAX_GROUP_CANDIDATES = 32
MAX_ARTICLE_WIRE_BYTES = 2 * MAX_ARTICLE_BYTES
MAX_BENCHMARK_CONNECTIONS = 16
MAX_PIPELINE = 16


class BenchmarkUnavailable(RuntimeError):
    """A sanitized benchmark failure that carries no provider material."""


@dataclass(frozen=True, slots=True)
class Candidate:
    message_id: bytes
    declared_bytes: int


@dataclass(frozen=True, slots=True)
class Sample:
    dimension: str
    value: int
    attempted: int
    completed: int
    missing: int
    wire_bytes: int
    elapsed_ns: int

    @property
    def mib_per_second(self) -> float:
        return self.wire_bytes * 1_000_000_000 / self.elapsed_ns / (1024 * 1024)


class NntpConnection:
    def __init__(self, config: dict):
        self._config = config
        self._socket: socket.socket | ssl.SSLSocket | None = None
        self._reader = None
        self.wire_bytes = 0

    def connect(self) -> None:
        try:
            transport = socket.create_connection(
                (self._config["host"], self._config["port"]),
                timeout=8,
            )
            transport.settimeout(15)
            context = ssl.create_default_context()
            if self._config["tls_mode"] == "implicit":
                transport = context.wrap_socket(
                    transport,
                    server_hostname=self._config["host"],
                )
            self._socket = transport
            self._reader = transport.makefile("rb")
            if self._status()[0] not in {200, 201}:
                raise BenchmarkUnavailable("greeting rejected")
            if self._config["tls_mode"] == "starttls":
                self._send(b"STARTTLS")
                if self._status()[0] != 382:
                    raise BenchmarkUnavailable("STARTTLS rejected")
                self._reader.close()
                self._socket = context.wrap_socket(
                    self._socket,
                    server_hostname=self._config["host"],
                )
                self._socket.settimeout(15)
                self._reader = self._socket.makefile("rb")
            self._send(b"MODE READER")
            reader_code = self._status()[0]
            if reader_code not in {200, 201, 480, 502}:
                raise BenchmarkUnavailable("reader mode rejected")
            username = self._config.get("username")
            password = self._config.get("password")
            if username is not None:
                self._send(b"AUTHINFO USER " + username.encode())
                auth_code = self._status()[0]
                if auth_code == 381:
                    self._send(b"AUTHINFO PASS " + password.encode())
                    auth_code = self._status()[0]
                if auth_code != 281:
                    raise BenchmarkUnavailable("authentication rejected")
        except BenchmarkUnavailable:
            raise
        except (OSError, ssl.SSLError, UnicodeError) as exc:
            raise BenchmarkUnavailable("connection failed") from exc

    def close(self) -> None:
        try:
            self._send(b"QUIT")
        except (AttributeError, OSError):
            pass
        if self._reader is not None:
            self._reader.close()
        if self._socket is not None:
            self._socket.close()

    def test_groups(self) -> list[bytes]:
        self._send(b"LIST ACTIVE")
        if self._status()[0] != 215:
            raise BenchmarkUnavailable("active group discovery is unavailable")
        groups: list[tuple[int, bytes]] = []
        for row in self._multiline(maximum_bytes=MAX_ACTIVE_BYTES):
            fields = row.split()
            if len(fields) < 3:
                continue
            group = fields[0]
            try:
                last = int(fields[1])
                first = int(fields[2])
            except ValueError:
                continue
            if (
                not group
                or len(group) + len(b"GROUP \r\n") > 4096
                or any(byte < 33 or byte == 127 for byte in group)
                or first < 1
                or last < first
            ):
                continue
            candidate = (last - first + 1, group)
            if len(groups) < MAX_GROUP_CANDIDATES:
                heapq.heappush(groups, candidate)
            elif candidate > groups[0]:
                heapq.heapreplace(groups, candidate)
        return [group for _count, group in sorted(groups, reverse=True)]

    def select_group(self, group: bytes) -> tuple[int, int]:
        self._send(b"GROUP " + group)
        code, line = self._status()
        fields = line.split()
        if code != 211 or len(fields) < 4:
            raise BenchmarkUnavailable("GROUP rejected")
        try:
            first = int(fields[2])
            last = int(fields[3])
        except ValueError as exc:
            raise BenchmarkUnavailable("invalid GROUP response") from exc
        if first < 1 or last < first:
            raise BenchmarkUnavailable("invalid GROUP response")
        return first, last

    def candidates(self, first: int, last: int) -> list[Candidate]:
        low = max(first, last - MAX_OVERVIEW_ARTICLES + 1)
        for command in (f"OVER {low}-{last}", f"XOVER {low}-{last}"):
            self._send(command.encode())
            code, _line = self._status()
            if code == 224:
                break
        else:
            raise BenchmarkUnavailable("overview is unavailable")
        candidates: list[Candidate] = []
        for row in self._multiline(maximum_bytes=MAX_OVERVIEW_BYTES):
            fields = row.split(b"\t")
            if len(fields) < 7:
                continue
            message_id = fields[4].strip()
            try:
                declared_bytes = int(fields[6])
            except ValueError:
                continue
            if (
                MIN_ARTICLE_BYTES <= declared_bytes <= MAX_ARTICLE_BYTES
                and message_id.startswith(b"<")
                and message_id.endswith(b">")
                and not any(byte < 33 or byte == 127 for byte in message_id)
            ):
                candidates.append(Candidate(message_id, declared_bytes))
        random.SystemRandom().shuffle(candidates)
        return candidates

    def body_batch(self, candidates: list[Candidate], pipeline: int) -> Sample:
        started = time.monotonic_ns()
        initial_wire_bytes = self.wire_bytes
        completed = 0
        missing = 0
        for offset in range(0, len(candidates), pipeline):
            batch = candidates[offset : offset + pipeline]
            self._socket.sendall(
                b"".join(b"BODY " + item.message_id + b"\r\n" for item in batch)
            )
            for _item in batch:
                code, _line = self._status()
                if code == 222:
                    for _row in self._multiline(maximum_bytes=MAX_ARTICLE_WIRE_BYTES):
                        pass
                    completed += 1
                elif code == 430:
                    missing += 1
                else:
                    raise BenchmarkUnavailable("BODY rejected")
        return Sample(
            "pipeline",
            pipeline,
            len(candidates),
            completed,
            missing,
            self.wire_bytes - initial_wire_bytes,
            time.monotonic_ns() - started,
        )

    def _send(self, command: bytes) -> None:
        self._socket.sendall(command + b"\r\n")

    def _line(self) -> bytes:
        line = self._reader.readline(4098)
        if not line or len(line) > 4096 or not line.endswith(b"\n"):
            raise BenchmarkUnavailable("invalid NNTP line")
        self.wire_bytes += len(line)
        return line.rstrip(b"\r\n")

    def _status(self) -> tuple[int, bytes]:
        line = self._line()
        if len(line) < 3 or not line[:3].isdigit():
            raise BenchmarkUnavailable("invalid NNTP status")
        return int(line[:3]), line

    def _multiline(self, *, maximum_bytes: int) -> Iterator[bytes]:
        started_at = self.wire_bytes
        while True:
            line = self._line()
            if self.wire_bytes - started_at > maximum_bytes:
                raise BenchmarkUnavailable("NNTP multiline response is too large")
            if line == b".":
                return
            yield line[1:] if line.startswith(b"..") else line


def recommend_knee(
    samples: list[Sample],
    *,
    expected_values: list[int] | None = None,
    required_samples: int = 1,
) -> int | None:
    """Return a comparable 95%-of-best knee, or None for an incomplete sweep."""
    if required_samples < 1:
        raise ValueError("required_samples must be positive")
    by_value: dict[int, list[float]] = {}
    for sample in samples:
        if sample.completed:
            by_value.setdefault(sample.value, []).append(sample.mib_per_second)
    if expected_values is not None and any(
        len(by_value.get(value, ())) < required_samples for value in expected_values
    ):
        return None
    if not by_value:
        raise BenchmarkUnavailable("no article was delivered")
    medians = {
        value: statistics.median(throughputs) for value, throughputs in by_value.items()
    }
    threshold = max(medians.values()) * 0.95
    return min(
        value for value, throughput in medians.items() if throughput >= threshold
    )


def take_bounded(
    candidates: list[Candidate],
    cursor: int,
    count: int,
    remaining_bytes: int,
) -> tuple[list[Candidate], int, int]:
    selected: list[Candidate] = []
    while cursor < len(candidates) and len(selected) < count:
        candidate = candidates[cursor]
        cursor += 1
        if candidate.declared_bytes > remaining_bytes:
            continue
        selected.append(candidate)
        remaining_bytes -= candidate.declared_bytes
    return selected, cursor, remaining_bytes


def benchmark_connection_count(
    config: dict,
    candidates: list[Candidate],
    connections: int,
    group: bytes,
) -> Sample:
    clients = [NntpConnection(config) for _index in range(connections)]
    try:
        with ThreadPoolExecutor(max_workers=connections) as executor:
            list(executor.map(lambda client: client.connect(), clients))
            list(executor.map(lambda client: client.select_group(group), clients))
            assignments = [
                candidates[index::connections] for index in range(connections)
            ]
            started = time.monotonic_ns()
            futures = [
                executor.submit(
                    client.body_batch,
                    assignment,
                    min(int(config["pipeline"]), MAX_PIPELINE),
                )
                for client, assignment in zip(clients, assignments, strict=True)
            ]
            results = [future.result() for future in futures]
            elapsed_ns = time.monotonic_ns() - started
    finally:
        for client in clients:
            client.close()
    return Sample(
        "connections",
        connections,
        sum(result.attempted for result in results),
        sum(result.completed for result in results),
        sum(result.missing for result in results),
        sum(result.wire_bytes for result in results),
        elapsed_ns,
    )


async def assert_engine_idle() -> None:
    try:
        snapshot = await EngineClient(
            settings.USENET_RUNTIME_DIR + "/engine.json"
        ).stats()
    except EngineUnavailable:
        return
    if (
        snapshot["requests_active"] > 1
        or snapshot["session_prefetches_active"]
        or snapshot["nntp_connections_open"]
        or snapshot["nntp_connections_active"]
        or snapshot["nntp_queue_interactive"]
        or snapshot["nntp_queue_preparation"]
        or snapshot["nntp_queue_background"]
    ):
        raise BenchmarkUnavailable("the Usenet engine is serving active work")


def benchmark_provider(
    config: dict,
    *,
    byte_budget: int,
    windows: int,
) -> tuple[list[Sample], int | None, int | None, int, float]:
    connection = NntpConnection(config)
    started = time.monotonic_ns()
    try:
        connection.connect()
        handshake_ms = (time.monotonic_ns() - started) / 1_000_000
        pipeline_values = sorted({1, 2, 4, 8, int(config["pipeline"])})
        connection_values = sorted(
            {
                1,
                2,
                4,
                8,
                min(int(config["connections"]), MAX_BENCHMARK_CONNECTIONS),
            }
        )
        candidate_target = (
            windows
            * 2
            * max(
                sum(max(4, value) for value in pipeline_values),
                sum(max(4, value * 2) for value in connection_values),
            )
        )
        group = b""
        candidates: list[Candidate] = []
        for discovered_group in connection.test_groups():
            try:
                first, last = connection.select_group(discovered_group)
                discovered_candidates = connection.candidates(first, last)
            except BenchmarkUnavailable:
                continue
            if len(discovered_candidates) > len(candidates):
                group = discovered_group
                candidates = discovered_candidates
            if len(candidates) >= candidate_target:
                break
        if not candidates:
            raise BenchmarkUnavailable("no benchmark article is available")
        pipeline_candidates = candidates[::2]
        connection_candidates = candidates[1::2]
        pipeline_cursor = 0
        connection_cursor = 0
        pipeline_remaining = byte_budget // 2
        connection_remaining = byte_budget - pipeline_remaining
        samples: list[Sample] = []
        for _window in range(windows):
            random.SystemRandom().shuffle(pipeline_values)
            for pipeline in pipeline_values:
                previous_cursor = pipeline_cursor
                previous_remaining = pipeline_remaining
                requested = max(4, pipeline)
                selected, pipeline_cursor, pipeline_remaining = take_bounded(
                    pipeline_candidates,
                    pipeline_cursor,
                    requested,
                    pipeline_remaining,
                )
                if len(selected) != requested:
                    pipeline_cursor = previous_cursor
                    pipeline_remaining = previous_remaining
                    continue
                samples.append(connection.body_batch(selected, pipeline))
        for _window in range(windows):
            random.SystemRandom().shuffle(connection_values)
            for connections in connection_values:
                previous_cursor = connection_cursor
                previous_remaining = connection_remaining
                requested = max(4, connections * 2)
                selected, connection_cursor, connection_remaining = take_bounded(
                    connection_candidates,
                    connection_cursor,
                    requested,
                    connection_remaining,
                )
                if len(selected) != requested:
                    connection_cursor = previous_cursor
                    connection_remaining = previous_remaining
                    continue
                samples.append(
                    benchmark_connection_count(config, selected, connections, group)
                )
        pipeline_samples = [
            sample for sample in samples if sample.dimension == "pipeline"
        ]
        connection_samples = [
            sample for sample in samples if sample.dimension == "connections"
        ]
        return (
            samples,
            recommend_knee(
                pipeline_samples,
                expected_values=pipeline_values,
                required_samples=windows,
            ),
            recommend_knee(
                connection_samples,
                expected_values=connection_values,
                required_samples=windows,
            ),
            pipeline_remaining + connection_remaining,
            handshake_ms,
        )
    finally:
        connection.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Measure configured NNTP providers without printing credentials, hosts, "
            "message-ids, subjects, or payloads."
        )
    )
    parser.add_argument(
        "--max-mib-per-provider",
        type=int,
        default=128,
        choices=range(8, 257),
        metavar="8..256",
    )
    parser.add_argument(
        "--windows",
        type=int,
        default=3,
        choices=range(1, 6),
        metavar="1..5",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not settings.USENET_NATIVE_SERVERS:
        raise SystemExit("No instance NNTP provider is configured.")
    try:
        asyncio.run(assert_engine_idle())
    except BenchmarkUnavailable as exc:
        raise SystemExit(f"Benchmark refused: {exc}") from exc
    byte_budget = args.max_mib_per_provider * 1024 * 1024
    for index, config in enumerate(settings.USENET_NATIVE_SERVERS, 1):
        try:
            (
                samples,
                pipeline_recommendation,
                connection_recommendation,
                remaining,
                handshake_ms,
            ) = benchmark_provider(
                config,
                byte_budget=byte_budget,
                windows=args.windows,
            )
        except BenchmarkUnavailable as exc:
            print(f"provider={index} unavailable={exc}")
            continue
        print(
            f"provider={index} handshake_ms={handshake_ms:.1f} "
            f"consumed_bytes={byte_budget - remaining} "
            "recommended_pipeline="
            f"{pipeline_recommendation if pipeline_recommendation is not None else 'inconclusive'} "
            "recommended_connections="
            f"{connection_recommendation if connection_recommendation is not None else 'inconclusive'}"
        )
        for sample in sorted(
            samples,
            key=lambda item: (item.dimension, item.value, item.elapsed_ns),
        ):
            print(
                f"provider={index} {sample.dimension}={sample.value} "
                f"attempted={sample.attempted} completed={sample.completed} "
                f"missing={sample.missing} wire_bytes={sample.wire_bytes} "
                f"elapsed_ms={sample.elapsed_ns / 1_000_000:.1f} "
                f"mib_s={sample.mib_per_second:.2f}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
