import asyncio
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from comet.usenet.engine_client import (
    EngineArchiveError,
    EngineClient,
    EngineNntpError,
    EngineParseError,
    _archive_member_identity,
    _prepare_par2_volume_request,
)
from comet.usenet.engine_stats import (
    ENGINE_STAT_BOOLEAN_FIELDS,
    ENGINE_STAT_INTEGER_FIELDS,
)
from comet.usenet.engine_transport import (
    MAX_ENGINE_CONTROL_BYTES,
    EngineDescriptor,
    EngineUnavailable,
    _maximum_response_bytes,
    _response_timeout,
)
from comet.usenet.limits import (
    MAX_NZB_METADATA_BYTES,
    MAX_USENET_LOGICAL_BYTES,
)


def provider_set_registration(
    generation: str = "b" * 64, identity: str = "P" * 22
) -> tuple[int, dict[str, str], bytes]:
    return (
        200,
        {},
        json.dumps(
            {
                "version": 1,
                "provider_set_id": identity,
                "generation": generation,
            }
        ).encode(),
    )


def engine_stats_payload() -> dict[str, int | bool]:
    return {
        "version": 1,
        **{field: 0 for field in ENGINE_STAT_INTEGER_FIELDS},
        **{field: True for field in ENGINE_STAT_BOOLEAN_FIELDS},
    }


class EngineDescriptorTests(unittest.TestCase):
    def _write_descriptor(self, path: Path, payload: object) -> None:
        path.write_bytes(json.dumps(payload).encode())
        path.chmod(0o600)

    def test_descriptor_loads_one_private_exact_document(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            descriptor_path = root / "engine.json"
            socket_path = root / "engine.sock"
            payload = {
                "version": 1,
                "socket_path": str(socket_path),
                "runtime_id": "A" * 22,
                "api_version": 1,
            }
            self._write_descriptor(descriptor_path, payload)

            self.assertEqual(
                EngineDescriptor.load(descriptor_path),
                EngineDescriptor(1, str(socket_path), "A" * 22, 1),
            )

    def test_descriptor_rejects_oversized_permissive_and_linked_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            descriptor_path = root / "engine.json"
            descriptor_path.write_bytes(b"x" * 4_097)
            descriptor_path.chmod(0o600)
            with self.assertRaises(EngineUnavailable):
                EngineDescriptor.load(descriptor_path)

            descriptor_path.write_bytes(b"{}")
            descriptor_path.chmod(0o640)
            with self.assertRaises(EngineUnavailable):
                EngineDescriptor.load(descriptor_path)

            descriptor_path.unlink()
            target = root / "target"
            target.write_bytes(b"{}")
            target.chmod(0o600)
            descriptor_path.symlink_to(target)
            with self.assertRaises(EngineUnavailable):
                EngineDescriptor.load(descriptor_path)

    def test_descriptor_requires_consumed_fields_and_ignores_extensions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            descriptor_path = root / "engine.json"
            valid = {
                "version": 1,
                "socket_path": str(root / "engine.sock"),
                "runtime_id": "A" * 22,
                "api_version": 1,
            }
            extended = {
                **valid,
                "runtime_id": "runtime/id?opaque-value",
                "extra": True,
            }
            self._write_descriptor(descriptor_path, extended)
            loaded = EngineDescriptor.load(descriptor_path)
            self.assertEqual(loaded.api_version, 1)
            self.assertEqual(loaded.runtime_id, "runtime/id?opaque-value")
            invalid_payloads = (
                {**valid, "version": True},
                {**valid, "api_version": True},
                {**valid, "socket_path": "relative.sock"},
                {**valid, "socket_path": "/" + "x" * 108},
                {**valid, "runtime_id": "short"},
                {**valid, "runtime_id": "A" * 65},
            )
            for payload in invalid_payloads:
                with self.subTest(payload=payload):
                    self._write_descriptor(descriptor_path, payload)
                    with self.assertRaises(EngineUnavailable):
                        EngineDescriptor.load(descriptor_path)


class EngineClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_health_requires_only_the_consumed_contract(self):
        client = EngineClient("/missing/engine.json")
        valid_payloads = (
            {"version": 1, "mode": "parser"},
            {
                "version": 1,
                "mode": "future-native-mode",
                "par2": "starting",
                "archive": {"state": "ready"},
            },
        )
        for payload in valid_payloads:
            with self.subTest(payload=payload):
                client.request = AsyncMock(
                    return_value=(200, {}, json.dumps(payload).encode())
                )
                self.assertEqual(await client.health(), payload)

        invalid_payloads = (
            [],
            {"version": 1},
            {"version": 1, "mode": ""},
            {"version": True, "mode": "parser"},
            {"mode": "native"},
            {"version": 2, "mode": "native"},
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                client.request = AsyncMock(
                    return_value=(200, {}, json.dumps(payload).encode())
                )
                with self.assertRaisesRegex(EngineUnavailable, "invalid health"):
                    await client.health()

    async def test_missing_descriptor_invalidates_registered_provider_sets(self):
        with tempfile.TemporaryDirectory() as directory:
            descriptor_path = Path(directory) / "engine.json"
            descriptor_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "socket_path": str(Path(directory) / "engine.sock"),
                        "runtime_id": "A" * 22,
                        "api_version": 1,
                    }
                )
            )
            descriptor_path.chmod(0o600)
            client = EngineClient(descriptor_path)
            client._load_descriptor()
            client._provider_set_ids["b" * 64] = "P" * 22

            descriptor_path.unlink()

            with self.assertRaises(EngineUnavailable):
                client._load_descriptor()
            self.assertEqual(client._provider_set_ids, {})

    async def test_request_logs_typed_engine_failure_details(self):
        responses = iter(
            (
                (
                    b"422",
                    b'{"version":1,"code":"nntp_article_missing","retryable":false}',
                ),
                (
                    b"503",
                    b'{"version":1,"code":"nntp_cancelled","retryable":true}',
                ),
            )
        )

        async def failed_response(reader, writer):
            await reader.read(4_096)
            status_code, body = next(responses)
            writer.write(
                b"HTTP/1.1 "
                + status_code
                + b" \r\nContent-Length: "
                + str(len(body)).encode()
                + b"\r\n\r\n"
                + body
            )
            await writer.drain()
            writer.close()

        with tempfile.TemporaryDirectory() as directory:
            socket_path = str(Path(directory) / "engine.sock")
            server = await asyncio.start_unix_server(failed_response, socket_path)
            descriptor = EngineDescriptor(1, socket_path, "runtime", 1)
            try:
                with (
                    patch.object(
                        EngineClient,
                        "_load_descriptor",
                        return_value=descriptor,
                    ),
                    patch("comet.usenet.engine_transport.log.warning") as warning,
                    patch("comet.usenet.engine_transport.log.info") as info,
                ):
                    client = EngineClient("/missing/engine.json")
                    status, _headers, returned = await client.request(
                        "POST", "/v1/materializations"
                    )
                    cancelled_status, _headers, cancelled = await client.request(
                        "POST", "/v1/materializations"
                    )
            finally:
                server.close()
                await server.wait_closed()

        self.assertEqual(
            (status, returned),
            (
                422,
                b'{"version":1,"code":"nntp_article_missing","retryable":false}',
            ),
        )
        self.assertEqual(
            (cancelled_status, cancelled),
            (
                503,
                b'{"version":1,"code":"nntp_cancelled","retryable":true}',
            ),
        )
        warning.assert_called_once()
        info.assert_not_called()
        self.assertEqual(
            warning.call_args.kwargs["failure_reason"],
            "nntp_article_missing",
        )
        self.assertFalse(warning.call_args.kwargs["retryable"])

    async def test_request_waits_for_transient_engine_admission(self):
        attempts = 0

        async def admission_response(reader, writer):
            nonlocal attempts
            await reader.read(4_096)
            attempts += 1
            if attempts < 3:
                status = b"503 Service Unavailable"
                body = b'{"version":1,"code":"native_busy","retryable":true}'
            else:
                status = b"200 OK"
                body = b"{}"
            writer.write(
                b"HTTP/1.1 "
                + status
                + b"\r\nContent-Length: "
                + str(len(body)).encode()
                + b"\r\n\r\n"
                + body
            )
            await writer.drain()
            writer.close()

        with tempfile.TemporaryDirectory() as directory:
            socket_path = str(Path(directory) / "engine.sock")
            server = await asyncio.start_unix_server(admission_response, socket_path)
            descriptor = EngineDescriptor(1, socket_path, "runtime", 1)
            try:
                with (
                    patch.object(
                        EngineClient,
                        "_load_descriptor",
                        return_value=descriptor,
                    ),
                    patch(
                        "comet.usenet.engine_transport.asyncio.sleep",
                        new=AsyncMock(),
                    ) as sleep,
                    patch("comet.usenet.engine_transport.log.warning") as warning,
                ):
                    response = await EngineClient("/missing/engine.json").request(
                        "POST", "/v1/materializations"
                    )
            finally:
                server.close()
                await server.wait_closed()

        self.assertEqual(response, (200, {"content-length": "2"}, b"{}"))
        self.assertEqual(attempts, 3)
        self.assertEqual(
            [call.args for call in sleep.await_args_list],
            [(0.05,), (0.1,)],
        )
        warning.assert_not_called()

    async def test_admission_wait_is_cancellable(self):
        client = EngineClient("/missing/engine.json")
        client._request_once = AsyncMock(
            return_value=(
                503,
                {},
                b'{"version":1,"code":"native_busy","retryable":true}',
            )
        )
        request = asyncio.create_task(client.request("POST", "/v1/materializations"))
        await asyncio.sleep(0)
        request.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await request
        client._request_once.assert_awaited_once()

    async def test_request_rejects_noncanonical_engine_response_length(self):
        async def noncanonical_response(reader, writer):
            await reader.read(1)
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: +0\r\n\r\n")
            await writer.drain()
            writer.close()

        with tempfile.TemporaryDirectory() as directory:
            socket_path = str(Path(directory) / "engine.sock")
            server = await asyncio.start_unix_server(noncanonical_response, socket_path)
            descriptor = EngineDescriptor(1, socket_path, "runtime", 1)
            try:
                with patch.object(
                    EngineClient, "_load_descriptor", return_value=descriptor
                ):
                    with self.assertRaises(EngineUnavailable):
                        await EngineClient("/missing/engine.json").request(
                            "GET", "/v1/health"
                        )
            finally:
                server.close()
                await server.wait_closed()

    async def test_request_rejects_ambiguous_engine_response_lengths(self):
        async def ambiguous_response(reader, writer):
            await reader.read(1)
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nContent-Length: 0\r\n\r\n"
            )
            await writer.drain()
            writer.close()

        with tempfile.TemporaryDirectory() as directory:
            socket_path = str(Path(directory) / "engine.sock")
            server = await asyncio.start_unix_server(ambiguous_response, socket_path)
            descriptor = EngineDescriptor(1, socket_path, "runtime", 1)
            try:
                with patch.object(
                    EngineClient, "_load_descriptor", return_value=descriptor
                ):
                    with self.assertRaises(EngineUnavailable):
                        await EngineClient("/missing/engine.json").request(
                            "GET", "/v1/health"
                        )
            finally:
                server.close()
                await server.wait_closed()

    async def test_request_rejects_a_malformed_status_line(self):
        async def malformed_response(reader, writer):
            await reader.read(1)
            writer.write(b"NOT-HTTP 200 OK\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            writer.close()

        with tempfile.TemporaryDirectory() as directory:
            socket_path = str(Path(directory) / "engine.sock")
            server = await asyncio.start_unix_server(
                malformed_response,
                socket_path,
            )
            descriptor = EngineDescriptor(1, socket_path, "runtime", 1)
            try:
                with patch.object(
                    EngineClient, "_load_descriptor", return_value=descriptor
                ):
                    with self.assertRaises(EngineUnavailable):
                        await EngineClient("/missing/engine.json").request(
                            "GET", "/v1/health"
                        )
            finally:
                server.close()
                await server.wait_closed()

    async def test_request_times_out_when_a_framed_body_stalls(self):
        async def stalled_response(reader, writer):
            await reader.read(1)
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 1\r\n\r\n")
            await writer.drain()
            await reader.read()
            writer.close()

        with tempfile.TemporaryDirectory() as directory:
            socket_path = str(Path(directory) / "engine.sock")
            server = await asyncio.start_unix_server(stalled_response, socket_path)
            descriptor = EngineDescriptor(1, socket_path, "runtime", 1)
            try:
                with (
                    patch.object(
                        EngineClient, "_load_descriptor", return_value=descriptor
                    ),
                    patch(
                        "comet.usenet.engine_transport.MAX_ENGINE_CONTROL_RESPONSE_SECONDS",
                        0.01,
                    ),
                ):
                    with self.assertRaises(EngineUnavailable):
                        await EngineClient("/missing/engine.json").request(
                            "GET", "/v1/health"
                        )
            finally:
                server.close()
                await server.wait_closed()

    async def test_materialization_is_not_cut_off_by_the_control_timeout(self):
        async def delayed_response(reader, writer):
            await reader.read(4_096)
            await asyncio.sleep(0.02)
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}")
            await writer.drain()
            writer.close()

        with tempfile.TemporaryDirectory() as directory:
            socket_path = str(Path(directory) / "engine.sock")
            server = await asyncio.start_unix_server(delayed_response, socket_path)
            descriptor = EngineDescriptor(1, socket_path, "runtime", 1)
            try:
                with (
                    patch.object(
                        EngineClient,
                        "_load_descriptor",
                        return_value=descriptor,
                    ),
                    patch(
                        "comet.usenet.engine_transport."
                        "MAX_ENGINE_CONTROL_RESPONSE_SECONDS",
                        0.001,
                    ),
                ):
                    status, _headers, body = await EngineClient(
                        "/missing/engine.json"
                    ).request("POST", "/v1/materializations")
            finally:
                server.close()
                await server.wait_closed()

        self.assertEqual((status, body), (200, b"{}"))

    async def test_request_times_out_when_the_engine_stops_reading(self):
        release = asyncio.Event()
        handler_closed = asyncio.Event()

        async def stalled_request(_reader, writer):
            try:
                await release.wait()
            finally:
                writer.close()
                handler_closed.set()

        with tempfile.TemporaryDirectory() as directory:
            socket_path = str(Path(directory) / "engine.sock")
            server = await asyncio.start_unix_server(stalled_request, socket_path)
            descriptor = EngineDescriptor(1, socket_path, "runtime", 1)
            try:
                with (
                    patch.object(
                        EngineClient, "_load_descriptor", return_value=descriptor
                    ),
                    patch(
                        "comet.usenet.engine_transport.MAX_ENGINE_REQUEST_WRITE_SECONDS",
                        0.01,
                    ),
                    patch(
                        "comet.usenet.engine_transport.MAX_ENGINE_CLOSE_SECONDS",
                        0.01,
                    ),
                ):
                    with self.assertRaises(EngineUnavailable):
                        await EngineClient("/missing/engine.json").request(
                            "POST",
                            "/v1/health",
                            b"x" * (16 * 1024 * 1024),
                        )
            finally:
                release.set()
                await handler_closed.wait()
                server.close()
                await server.wait_closed()

    async def test_request_rejects_unbounded_or_malformed_input_before_connect(self):
        client = EngineClient("/missing/engine.json")

        for method, path, body in (
            ("PATCH", "/v1/health", b""),
            ("GET", "/v1/health HTTP/1.1", b""),
            ("GET", "/v1/health", "not-bytes"),
        ):
            with self.subTest(method=method, path=path, body_type=type(body)):
                with self.assertRaisesRegex(ValueError, "invalid engine request"):
                    await client.request(method, path, body)
        with patch(
            "comet.usenet.engine_transport.MAX_ENGINE_NZB_METADATA_BYTES",
            8,
        ):
            with self.assertRaisesRegex(ValueError, "invalid engine request"):
                await client.request("POST", "/v1/health", b"x" * 9)

    def test_native_range_response_limits_match_the_range_contract(self):
        self.assertEqual(
            _maximum_response_bytes(f"/v1/raw-composites/{'a' * 64}/read"),
            8 * 1024 * 1024,
        )
        self.assertEqual(
            _maximum_response_bytes(f"/v1/sessions/{'a' * 22}/read"),
            8 * 1024 * 1024,
        )

    def test_native_response_timeouts_follow_the_work_contract(self):
        for path in (
            "/v1/sessions",
            f"/v1/sessions/{'A' * 22}/read",
            f"/v1/raw-composites/{'a' * 64}/read",
        ):
            with self.subTest(path=path):
                self.assertEqual(_response_timeout(path), 35)
        for path in (
            "/v1/materializations",
            f"/v1/artifacts/{'a' * 64}/native-inspect",
            f"/v1/materializations/{'a' * 64}/native-inspect",
            f"/v1/raw-composites/{'a' * 64}/native-inspect",
            "/v1/archive-plan",
            "/v1/archive-direct/catalog",
            "/v1/archive-direct/open",
            "/v1/session-archives/catalog",
            "/v1/session-archives/open",
            "/v1/par2/discover",
            "/v1/par2/map-sources",
            "/v1/par2/repair",
            "/v1/archive-nested/catalog",
            "/v1/archive-nested/extract",
        ):
            with self.subTest(path=path):
                self.assertIsNone(_response_timeout(path))
        self.assertEqual(_response_timeout("/v1/health"), 5)

    def test_native_catalog_response_limit_matches_its_bounded_contract(self):
        self.assertEqual(
            _maximum_response_bytes(f"/v1/artifacts/{'a' * 64}/parse"),
            MAX_NZB_METADATA_BYTES,
        )
        self.assertEqual(
            _maximum_response_bytes(f"/v1/artifacts/{'A' * 64}/parse"),
            1024 * 1024,
        )
        self.assertEqual(
            _maximum_response_bytes(f"/v1/artifacts/{'a' * 64}/native-catalog"),
            2 * 1024 * 1024,
        )
        self.assertEqual(
            _maximum_response_bytes("/v1/session-archives/catalog"),
            2 * 1024 * 1024,
        )
        self.assertEqual(
            _maximum_response_bytes("/v1/par2/map-sources"),
            2 * 1024 * 1024,
        )
        self.assertEqual(
            _maximum_response_bytes("/v1/par2/repair"),
            2 * 1024 * 1024,
        )

    async def test_engine_stats_require_consumed_fields_and_ignore_extensions(self):
        client = EngineClient("/missing/engine.json")
        payload = engine_stats_payload()
        payload["sessions"] = 3
        client.request = AsyncMock(return_value=(200, {}, json.dumps(payload).encode()))

        returned = await client.stats()

        self.assertNotIn("version", returned)
        self.assertEqual(returned["sessions"], 3)
        self.assertIs(returned["disk_cache_stats_available"], True)
        self.assertIs(returned["spool_stats_available"], True)
        client.request.assert_awaited_once_with("GET", "/v1/stats")
        client.request = AsyncMock(
            return_value=(
                200,
                {},
                json.dumps({**payload, "configuration_partition": "secret"}).encode(),
            )
        )
        self.assertNotIn("configuration_partition", await client.stats())

        invalid_payloads = [
            {key: value for key, value in payload.items() if key != "sessions"},
            {**payload, "sessions": True},
            {**payload, "sessions": -1},
            {**payload, "sessions": 2**64},
            {**payload, "disk_cache_stats_available": 1},
            {**payload, "spool_stats_available": 1},
        ]
        for invalid in invalid_payloads:
            with self.subTest(invalid=set(invalid) ^ set(payload)):
                client.request = AsyncMock(
                    return_value=(200, {}, json.dumps(invalid).encode())
                )
                with self.assertRaisesRegex(EngineUnavailable, "invalid stats"):
                    await client.stats()

    async def test_engine_drain_requires_the_acknowledgement_fields(self):
        client = EngineClient("/missing/engine.json")
        client.request = AsyncMock(
            return_value=(202, {}, b'{"version":1,"draining":true}')
        )

        await client.drain()
        client.request = AsyncMock(
            return_value=(202, {}, b'{"version":1,"draining":true,"extra":1}')
        )
        await client.drain()

        for status, payload in [
            (200, b'{"version":1,"draining":true}'),
            (202, b'{"version":1,"draining":false}'),
            (202, b"not-json"),
        ]:
            with self.subTest(status=status, payload=payload):
                client.request = AsyncMock(return_value=(status, {}, payload))
                with self.assertRaises(EngineUnavailable):
                    await client.drain()

    async def test_engine_resume_requires_the_acknowledgement_fields(self):
        client = EngineClient("/missing/engine.json")
        client.request = AsyncMock(
            return_value=(200, {}, b'{"version":1,"draining":false}')
        )

        await client.resume()
        client.request.assert_awaited_once_with("POST", "/v1/resume")

        for status, payload in [
            (202, b'{"version":1,"draining":false}'),
            (200, b'{"version":1,"draining":true}'),
            (200, b"not-json"),
        ]:
            with self.subTest(status=status, payload=payload):
                client.request = AsyncMock(return_value=(status, {}, payload))
                with self.assertRaises(EngineUnavailable):
                    await client.resume()

    async def test_request_rejects_an_oversized_engine_response_body_before_reading_it(
        self,
    ):
        async def oversized_response(reader, writer):
            await reader.read(1)
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 1048577\r\n\r\n")
            await writer.drain()
            writer.close()

        with tempfile.TemporaryDirectory() as directory:
            socket_path = str(Path(directory) / "engine.sock")
            server = await asyncio.start_unix_server(oversized_response, socket_path)
            descriptor = EngineDescriptor(1, socket_path, "runtime", 1)
            try:
                with patch.object(
                    EngineClient, "_load_descriptor", return_value=descriptor
                ):
                    with self.assertRaises(EngineUnavailable):
                        await EngineClient("/missing/engine.json").request(
                            "GET", "/v1/health"
                        )
            finally:
                server.close()
                await server.wait_closed()

    async def test_request_rejects_an_oversized_engine_response_header(self):
        async def oversized_response(reader, writer):
            await reader.read(1)
            writer.write(
                b"HTTP/1.1 200 OK\r\nX-Padded: " + b"x" * (16 * 1024) + b"\r\n\r\n"
            )
            await writer.drain()
            writer.close()

        with tempfile.TemporaryDirectory() as directory:
            socket_path = str(Path(directory) / "engine.sock")
            server = await asyncio.start_unix_server(oversized_response, socket_path)
            descriptor = EngineDescriptor(1, socket_path, "runtime", 1)
            try:
                with patch.object(
                    EngineClient, "_load_descriptor", return_value=descriptor
                ):
                    with self.assertRaises(EngineUnavailable):
                        await EngineClient("/missing/engine.json").request(
                            "GET", "/v1/health"
                        )
            finally:
                server.close()
                await server.wait_closed()

    async def test_parse_rejects_invalid_artifact_identity_before_socket_io(self):
        client = EngineClient("/missing/engine.json")

        for identity, error in (
            ("A" * 64, "lowercase SHA-256"),
            ("a" * 64, "does not match"),
        ):
            with self.subTest(identity=identity):
                with self.assertRaisesRegex(ValueError, error):
                    await client.parse_nzb(identity, b"<nzb/>")

    async def test_parse_sends_only_a_verified_artifact_identity_to_the_engine(self):
        document = b"<nzb/>"
        digest = hashlib.sha256(document).hexdigest()
        manifest = [{"postings": [{"number": 1, "bytes": 1, "message_id": "x"}]}]
        client = EngineClient("/missing/engine.json")
        client.request = AsyncMock(
            return_value=(
                200,
                {},
                json.dumps(
                    {
                        "version": 2,
                        "files": 1,
                        "segments": 1,
                        "nh1": "nh1:" + "a" * 40,
                        "nm1": "nm1:" + "b" * 64,
                        "metadata": {"password": "archive-secret"},
                        "manifest": manifest,
                    }
                ).encode(),
            )
        )

        self.assertEqual(
            (await client.parse_nzb(digest, document))["manifest"],
            manifest,
        )

        client.request.assert_awaited_once_with(
            "POST", f"/v1/artifacts/{digest}/parse", document
        )

    async def test_parse_requires_consumed_success_and_failure_fields(self):
        document = b"<nzb/>"
        digest = hashlib.sha256(document).hexdigest()
        valid = {
            "version": 2,
            "files": 1,
            "segments": 1,
            "nh1": "nh1:" + "a" * 40,
            "nm1": "nm1:" + "b" * 64,
            "metadata": {},
            "manifest": [{"postings": [{}]}],
        }
        invalid_successes = (
            [],
            {**valid, "version": True},
            {**valid, "files": True},
            {**valid, "files": 2},
            {**valid, "segments": 2},
            {**valid, "nh1": "nh1:x"},
            {**valid, "nm1": "nm1:y"},
            {**valid, "metadata": []},
            {**valid, "manifest": [{}]},
        )
        client = EngineClient("/missing/engine.json")
        for payload in invalid_successes:
            with self.subTest(payload=payload):
                client.request = AsyncMock(
                    return_value=(200, {}, json.dumps(payload).encode())
                )
                with self.assertRaisesRegex(EngineUnavailable, "invalid parse data"):
                    await client.parse_nzb(digest, document)

        client.request = AsyncMock(
            return_value=(
                200,
                {},
                json.dumps({**valid, "unexpected": None}).encode(),
            )
        )
        self.assertEqual((await client.parse_nzb(digest, document))["files"], 1)

        client.request = AsyncMock(
            return_value=(
                422,
                {},
                json.dumps(
                    {
                        "version": 1,
                        "code": "invalid nzb code",
                        "retryable": False,
                    }
                ).encode(),
            )
        )
        with self.assertRaises(EngineParseError) as caught:
            await client.parse_nzb(digest, document)
        self.assertEqual(str(caught.exception), "invalid nzb code")

    async def test_materialization_rejects_a_payload_above_its_metadata_budget(self):
        client = EngineClient("/missing/engine.json")
        postings = [(index, 1, f"{index}@{'a' * 990}") for index in range(1, 1_100)]

        with patch(
            "comet.usenet.engine_client.MAX_ENGINE_NZB_METADATA_BYTES",
            1024 * 1024,
        ):
            with self.assertRaisesRegex(ValueError, "exceeds"):
                await client.materialize_nntp_postings(
                    postings,
                    servers=[
                        {
                            "provider_configuration_id": "primary",
                            "host": "news.example.test",
                            "port": 119,
                            "tls_mode": "plaintext",
                            "allow_private": False,
                            "username": None,
                            "password": None,
                            "connections": 4,
                            "pipeline": 2,
                            "priority": 0,
                            "backup": False,
                        }
                    ],
                    account_partition=b"a" * 32,
                    provider_set_generation="b" * 64,
                )

    async def test_archive_volume_plan_is_closed_and_reconciled_to_the_request(self):
        client = EngineClient("/missing/engine.json")
        first = "a" * 64
        second = "b" * 64
        set_identity = "c" * 64
        client.request = AsyncMock(
            return_value=(
                200,
                {},
                json.dumps(
                    {
                        "version": 1,
                        "plan": {
                            "set_identity": set_identity,
                            "kind": {"layout": "raw_split"},
                            "exact_size": 30,
                            "volumes": [
                                {
                                    "content_identity": first,
                                    "relative_path": "movie.mkv.001",
                                    "number": 0,
                                    "exact_size": 10,
                                },
                                {
                                    "content_identity": second,
                                    "relative_path": "movie.mkv.002",
                                    "number": 1,
                                    "exact_size": 20,
                                },
                            ],
                        },
                    }
                ).encode(),
            )
        )

        plan = await client.plan_archive_volumes(
            [
                (first, "movie.mkv.001", 10),
                (second, "movie.mkv.002", 20),
            ]
        )

        self.assertEqual(plan["set_identity"], set_identity)
        method, path, body = client.request.await_args.args
        self.assertEqual((method, path), ("POST", "/v1/archive-plan"))
        self.assertEqual(
            json.loads(body),
            {
                "volumes": [
                    {
                        "content_identity": first,
                        "relative_path": "movie.mkv.001",
                        "expected_size": 10,
                    },
                    {
                        "content_identity": second,
                        "relative_path": "movie.mkv.002",
                        "expected_size": 20,
                    },
                ]
            },
        )
        client.request = AsyncMock(
            return_value=(
                200,
                {},
                json.dumps(
                    {
                        "version": 1,
                        "plan": {
                            "set_identity": set_identity,
                            "kind": {"layout": "raw_split"},
                            "exact_size": 30,
                            "volumes": [
                                {
                                    "content_identity": first,
                                    "relative_path": "wrong.mkv.001",
                                    "number": 0,
                                    "exact_size": 10,
                                },
                                {
                                    "content_identity": second,
                                    "relative_path": "movie.mkv.002",
                                    "number": 1,
                                    "exact_size": 20,
                                },
                            ],
                        },
                    }
                ).encode(),
            )
        )
        with self.assertRaisesRegex(EngineUnavailable, "invalid archive plan"):
            await client.plan_archive_volumes(
                [
                    (first, "movie.mkv.001", 10),
                    (second, "movie.mkv.002", 20),
                ]
            )

    async def test_archive_volume_plan_preserves_typed_busy_failure(self):
        client = EngineClient("/missing/engine.json")
        client.request = AsyncMock(
            return_value=(
                503,
                {},
                b'{"version":1,"code":"archive_busy","retryable":true}',
            )
        )

        with self.assertRaises(EngineArchiveError) as raised:
            await client.plan_archive_volumes([("a" * 64, "release.rar", 100)])

        self.assertEqual(raised.exception.code, "archive_busy")
        self.assertTrue(raised.exception.retryable)

    async def test_archive_failures_preserve_opaque_bounded_error_codes(self):
        client = EngineClient("/missing/engine.json")
        for code in ("archive-busy", "échec"):
            with self.subTest(code=code):
                client.request = AsyncMock(
                    return_value=(
                        503,
                        {},
                        json.dumps(
                            {
                                "version": 1,
                                "code": code,
                                "retryable": True,
                            }
                        ).encode(),
                    )
                )
                with self.assertRaises(EngineArchiveError) as raised:
                    await client.plan_archive_volumes([("a" * 64, "release.rar", 100)])
                self.assertEqual(raised.exception.code, code)

        client.request = AsyncMock(
            return_value=(
                503,
                {},
                json.dumps(
                    {
                        "version": 1,
                        "code": "x" * 129,
                        "retryable": True,
                    }
                ).encode(),
            )
        )
        with self.assertRaisesRegex(
            EngineUnavailable,
            "invalid archive failure data",
        ):
            await client.plan_archive_volumes([("a" * 64, "release.rar", 100)])

    async def test_nested_archive_routes_bind_the_layer_chain_and_output_size(self):
        client = EngineClient("/missing/engine.json")
        content_identity = "a" * 64
        set_identity = "b" * 64
        selected_paths = ("payload.tar.gz", "Movie.2026.mkv")
        relative_path = "!/".join(selected_paths)
        plan = {
            "set_identity": set_identity,
            "kind": {"layout": "single_archive", "format": "zip"},
            "exact_size": 100,
            "volumes": [
                {
                    "content_identity": content_identity,
                    "relative_path": "release.zip",
                    "number": 0,
                    "exact_size": 100,
                }
            ],
        }
        member = {
            "member_id": _archive_member_identity(set_identity, relative_path, 42),
            "relative_path": relative_path,
            "exact_size": 42,
            "kind": "video",
            "selected_paths": list(selected_paths),
        }
        client.request = AsyncMock(
            return_value=(
                200,
                {},
                json.dumps({"version": 1, "plan": plan, "members": [member]}).encode(),
            )
        )

        returned_plan, members = await client.catalog_nested_archive_volumes(
            [(content_identity, "release.zip", 100)],
            passphrase="archive-secret",
        )

        self.assertEqual((returned_plan, members), (plan, [member]))
        self.assertEqual(
            client.request.await_args.args[:2],
            ("POST", "/v1/archive-nested/catalog"),
        )
        self.assertEqual(
            json.loads(client.request.await_args.args[2])["passphrase"],
            "archive-secret",
        )
        client.request = AsyncMock(
            return_value=(
                200,
                {},
                json.dumps(
                    {
                        "version": 1,
                        "identity": "c" * 64,
                        "byte_size": 42,
                        "asset_revision": "d" * 64,
                    }
                ).encode(),
            )
        )

        result = await client.extract_nested_archive_volume_set(
            [(content_identity, "release.zip", 100)],
            42,
            selected_paths,
            passphrase="archive-secret",
        )

        self.assertEqual(result, ("c" * 64, 42, "d" * 64))
        method, path, body = client.request.await_args.args
        self.assertEqual((method, path), ("POST", "/v1/archive-nested/extract"))
        self.assertEqual(json.loads(body)["selected_paths"], list(selected_paths))
        self.assertEqual(json.loads(body)["passphrase"], "archive-secret")
        with self.assertRaisesRegex(ValueError, "passphrase"):
            await client.extract_nested_archive_volume_set(
                [(content_identity, "release.zip", 100)],
                42,
                selected_paths,
                passphrase="bad\nsecret",
            )
        invalid = {**member, "selected_paths": ["other.zip", "Movie.2026.mkv"]}
        client.request = AsyncMock(
            return_value=(
                200,
                {},
                json.dumps({"version": 1, "plan": plan, "members": [invalid]}).encode(),
            )
        )
        with self.assertRaisesRegex(EngineUnavailable, "invalid archive catalog"):
            await client.catalog_nested_archive_volumes(
                [(content_identity, "release.zip", 100)]
            )

    async def test_stored_archive_routes_are_bound_to_the_member_and_plan(self):
        client = EngineClient("/missing/engine.json")
        content_identity = "a" * 64
        set_identity = "b" * 64
        selected_path = "Movie.2026.mkv"
        exact_size = 42
        member_identity = _archive_member_identity(
            set_identity, selected_path, exact_size
        )
        plan = {
            "set_identity": set_identity,
            "kind": {"layout": "single_archive", "format": "rar5"},
            "exact_size": 100,
            "volumes": [
                {
                    "content_identity": content_identity,
                    "relative_path": "release.rar",
                    "number": 0,
                    "exact_size": 100,
                }
            ],
        }
        member = {
            "member_id": member_identity,
            "relative_path": selected_path,
            "exact_size": exact_size,
            "kind": "video",
        }
        client.request = AsyncMock(
            return_value=(
                200,
                {},
                json.dumps({"version": 1, "plan": plan, "members": [member]}).encode(),
            )
        )

        returned_plan, members = await client.catalog_stored_archive_volumes(
            [(content_identity, "release.rar", 100)]
        )

        self.assertEqual((returned_plan, members), (plan, [member]))
        self.assertEqual(
            client.request.await_args.args[:2],
            ("POST", "/v1/archive-direct/catalog"),
        )
        client.request = AsyncMock(
            return_value=(
                200,
                {},
                json.dumps(
                    {
                        "version": 1,
                        "identity": member_identity,
                        "exact_size": exact_size,
                        "etag": member_identity,
                        "relative_path": selected_path,
                        "plan": plan,
                    }
                ).encode(),
            )
        )

        opened = await client.open_stored_archive_member(
            [(content_identity, "release.rar", 100)],
            exact_size,
            selected_path,
        )

        self.assertEqual(opened, (plan, member_identity, exact_size, member_identity))
        self.assertEqual(
            client.request.await_args.args[:2],
            ("POST", "/v1/archive-direct/open"),
        )
        session_id = "S" * 22
        session_plan = dict(
            plan,
            kind={"layout": "single_archive", "format": "seven_zip"},
        )
        client.request = AsyncMock(
            side_effect=[
                (
                    200,
                    {},
                    json.dumps(
                        {"version": 1, "plan": session_plan, "members": [member]}
                    ).encode(),
                ),
                (
                    200,
                    {},
                    json.dumps(
                        {
                            "version": 1,
                            "identity": member_identity,
                            "exact_size": exact_size,
                            "etag": member_identity,
                            "relative_path": selected_path,
                            "plan": session_plan,
                        }
                    ).encode(),
                ),
            ]
        )

        returned_plan, members = await client.catalog_session_archive_volumes(
            [(session_id, content_identity, "release.rar", 100)],
            passphrase="archive-secret",
        )
        opened = await client.open_session_archive_member(
            [(session_id, content_identity, "release.rar", 100)],
            exact_size,
            selected_path,
            passphrase="archive-secret",
        )

        self.assertEqual((returned_plan, members), (session_plan, [member]))
        self.assertEqual(
            opened,
            (session_plan, member_identity, exact_size, member_identity),
        )
        first_request, second_request = client.request.await_args_list
        self.assertEqual(
            first_request.args[:2],
            ("POST", "/v1/session-archives/catalog"),
        )
        self.assertEqual(
            json.loads(first_request.args[2])["volumes"][0],
            {
                "session_id": session_id,
                "revision": content_identity,
                "relative_path": "release.rar",
                "expected_size": 100,
            },
        )
        self.assertEqual(
            json.loads(first_request.args[2])["passphrase"],
            "archive-secret",
        )
        self.assertEqual(
            second_request.args[:2],
            ("POST", "/v1/session-archives/open"),
        )
        self.assertEqual(
            json.loads(second_request.args[2])["passphrase"],
            "archive-secret",
        )
        rar4_plan = dict(plan, kind={"layout": "single_archive", "format": "rar4"})
        client.request = AsyncMock(
            return_value=(
                200,
                {},
                json.dumps(
                    {
                        "version": 1,
                        "identity": member_identity,
                        "exact_size": exact_size,
                        "etag": member_identity,
                        "relative_path": selected_path,
                        "plan": rar4_plan,
                    }
                ).encode(),
            )
        )

        opened = await client.open_stored_archive_member(
            [(content_identity, "release.rar", 100)],
            exact_size,
            selected_path,
        )

        self.assertEqual(
            opened, (rar4_plan, member_identity, exact_size, member_identity)
        )

    async def test_archive_volume_plan_rejects_aggregate_budget_before_request(self):
        client = EngineClient("/missing/engine.json")
        client.request = AsyncMock()

        with self.assertRaisesRegex(ValueError, "archive volume plan"):
            await client.plan_archive_volumes(
                [
                    ("a" * 64, "release.001", 600 * 1024 * 1024 * 1024),
                    ("b" * 64, "release.002", 600 * 1024 * 1024 * 1024),
                ]
            )

        client.request.assert_not_awaited()

    def test_par2_requests_are_not_limited_to_sixty_four_volumes(self):
        files = [
            (
                hashlib.sha256(str(index).encode()).hexdigest(),
                f"release.vol{index:03}+01.par2",
                1,
            )
            for index in range(65)
        ]

        _payload, request, identities, _expected = _prepare_par2_volume_request(
            files,
            error="PAR2 input invalid",
        )

        self.assertEqual(len(request["volumes"]), 65)
        self.assertEqual(len(identities), 65)

    async def test_par2_discovery_is_closed_and_binds_each_set_to_request_volumes(self):
        client = EngineClient("/missing/engine.json")
        first_identity = "a" * 64
        second_identity = "b" * 64
        first = {
            "set_id": "1" * 32,
            "slice_size": 4096,
            "files": [
                {
                    "file_id": "2" * 32,
                    "relative_path": "First.mkv",
                    "exact_size": 4096,
                    "full_md5": "3" * 32,
                    "first_16k_md5": "4" * 32,
                    "slice_count": 1,
                }
            ],
            "recovery_exponents": [0],
            "volume_content_identities": [first_identity, second_identity],
        }
        second = {
            "set_id": "5" * 32,
            "slice_size": 4,
            "files": [
                {
                    "file_id": "6" * 32,
                    "relative_path": "Second.rar",
                    "exact_size": 8,
                    "full_md5": "7" * 32,
                    "first_16k_md5": "8" * 32,
                    "slice_count": 2,
                }
            ],
            "recovery_exponents": [],
            "volume_content_identities": [second_identity],
        }
        first["files"].append(
            {
                "file_id": "0" * 32,
                "relative_path": "First.part02.rar",
                "exact_size": 4096,
                "full_md5": "9" * 32,
                "first_16k_md5": "a" * 32,
                "slice_count": 1,
            }
        )
        client.request = AsyncMock(
            return_value=(
                200,
                {},
                json.dumps({"version": 1, "sets": [first, second]}).encode(),
            )
        )

        self.assertEqual(
            await client.discover_par2_sets(
                [
                    (second_identity, "opaque-sidecar-2", 200),
                    (first_identity, "opaque-sidecar-1", 100),
                ]
            ),
            [first, second],
        )
        method, path, body = client.request.await_args.args
        self.assertEqual((method, path), ("POST", "/v1/par2/discover"))
        self.assertEqual(
            json.loads(body),
            {
                "files": [
                    {
                        "content_identity": second_identity,
                        "relative_path": "opaque-sidecar-2",
                        "expected_size": 200,
                    },
                    {
                        "content_identity": first_identity,
                        "relative_path": "opaque-sidecar-1",
                        "expected_size": 100,
                    },
                ]
            },
        )

        reordered_first = dict(
            first,
            volume_content_identities=[second_identity, first_identity],
        )
        client.request = AsyncMock(
            return_value=(
                200,
                {},
                json.dumps({"version": 1, "sets": [second, reordered_first]}).encode(),
            )
        )
        reordered = await client.discover_par2_sets(
            [
                (first_identity, "first.par2", 100),
                (second_identity, "second.par2", 200),
            ]
        )
        self.assertEqual([item["set_id"] for item in reordered], ["5" * 32, "1" * 32])
        self.assertEqual(
            reordered[1]["volume_content_identities"],
            [second_identity, first_identity],
        )

        for invalid in (
            {"version": 1, "sets": [first, first]},
            {
                "version": 1,
                "sets": [
                    dict(first, volume_content_identities=["c" * 64]),
                ],
            },
            {
                "version": 1,
                "sets": [
                    dict(
                        first,
                        volume_content_identities=[first_identity, first_identity],
                    ),
                ],
            },
        ):
            client.request = AsyncMock(
                return_value=(200, {}, json.dumps(invalid).encode())
            )
            with self.assertRaisesRegex(EngineUnavailable, "invalid PAR2 discovery"):
                await client.discover_par2_sets(
                    [
                        (first_identity, "first.par2", 100),
                        (second_identity, "second.par2", 200),
                    ]
                )

    async def test_par2_source_mapping_is_closed_and_identity_bound(self):
        client = EngineClient("/missing/engine.json")
        recovery_identity = "a" * 64
        first_source = "b" * 64
        second_source = "c" * 64
        mapping = {
            "version": 1,
            "set_id": "1" * 32,
            "slice_size": 4096,
            "mappings": [
                {
                    "content_identity": first_source,
                    "file_id": "2" * 32,
                    "relative_path": "Movie.mkv",
                    "exact_size": 8192,
                    "slice_count": 2,
                },
                {
                    "content_identity": second_source,
                    "file_id": "3" * 32,
                    "relative_path": "Other.mkv",
                    "exact_size": 4096,
                    "slice_count": 1,
                },
            ],
        }
        client.request = AsyncMock(return_value=(200, {}, json.dumps(mapping).encode()))

        self.assertEqual(
            await client.map_par2_sources(
                [(recovery_identity, "release.par2", 100)],
                [
                    (second_source, "obfuscated.002", 4096),
                    (first_source, "obfuscated.001", 8192),
                ],
                recovery_set_id=mapping["set_id"],
            ),
            mapping,
        )
        method, path, body = client.request.await_args.args
        self.assertEqual((method, path), ("POST", "/v1/par2/map-sources"))
        self.assertEqual(
            json.loads(body),
            {
                "files": [
                    {
                        "content_identity": recovery_identity,
                        "relative_path": "release.par2",
                        "expected_size": 100,
                    }
                ],
                "sources": [
                    {
                        "content_identity": second_source,
                        "relative_path": "obfuscated.002",
                        "expected_size": 4096,
                    },
                    {
                        "content_identity": first_source,
                        "relative_path": "obfuscated.001",
                        "expected_size": 8192,
                    },
                ],
                "set_id": mapping["set_id"],
            },
        )

        client.request = AsyncMock(
            return_value=(
                200,
                {},
                json.dumps(
                    {
                        **mapping,
                        "mappings": list(reversed(mapping["mappings"])),
                    }
                ).encode(),
            )
        )
        reordered_mapping = await client.map_par2_sources(
            [(recovery_identity, "release.par2", 100)],
            [
                (first_source, "obfuscated.001", 8192),
                (second_source, "obfuscated.002", 4096),
            ],
        )
        self.assertEqual(
            reordered_mapping["mappings"], list(reversed(mapping["mappings"]))
        )

        client.request = AsyncMock(
            return_value=(
                422,
                {},
                b'{"version":1,"code":"par2_source_unmatched","retryable":false}',
            )
        )
        with self.assertRaises(EngineArchiveError) as raised:
            await client.map_par2_sources(
                [(recovery_identity, "release.par2", 100)],
                [(first_source, "obfuscated.001", 8192)],
            )
        self.assertEqual(raised.exception.code, "par2_source_unmatched")
        self.assertFalse(raised.exception.retryable)

    async def test_par2_repair_is_selected_id_bound_and_accepts_no_complete_sources(
        self,
    ):
        client = EngineClient("/missing/engine.json")
        recovery_identity = "a" * 64
        selected_file_id = "b" * 32
        repaired_identity = "c" * 64
        result = {
            "version": 1,
            "set_id": "d" * 32,
            "file_id": selected_file_id,
            "relative_path": "Movie.mkv",
            "identity": repaired_identity,
            "byte_size": 8192,
            "asset_revision": "e" * 64,
            "partial_source_mapped": False,
        }
        client.request = AsyncMock(return_value=(200, {}, json.dumps(result).encode()))

        self.assertEqual(
            await client.repair_par2(
                [(recovery_identity, "release.par2", 100)],
                [],
                selected_file_id,
                recovery_set_id=result["set_id"],
            ),
            result,
        )
        method, path, body = client.request.await_args.args
        self.assertEqual((method, path), ("POST", "/v1/par2/repair"))
        self.assertEqual(
            json.loads(body),
            {
                "files": [
                    {
                        "content_identity": recovery_identity,
                        "relative_path": "release.par2",
                        "expected_size": 100,
                    }
                ],
                "sources": [],
                "partial_sources": [],
                "set_id": result["set_id"],
                "selected_file_id": selected_file_id,
            },
        )
        client._provider_set_ids["e" * 64] = "P" * 22

        self.assertEqual(
            await client.repair_par2(
                [(recovery_identity, "release.par2", 100)],
                [],
                selected_file_id,
                partial_sources=[
                    (
                        [
                            (1, 100, "known@example.test"),
                            (2, 100, "missing@example.test"),
                        ],
                        "alt.video",
                    )
                ],
                account_partition=b"a" * 32,
                provider_set_generation="e" * 64,
            ),
            result,
        )
        _method, _path, body = client.request.await_args.args
        self.assertEqual(
            json.loads(body)["partial_sources"],
            [
                {
                    "postings": [
                        {
                            "number": 1,
                            "bytes": 100,
                            "message_id": "known@example.test",
                        },
                        {
                            "number": 2,
                            "bytes": 100,
                            "message_id": "missing@example.test",
                        },
                    ],
                    "group": "alt.video",
                    "account_partition": "61" * 32,
                    "provider_set_id": "P" * 22,
                }
            ],
        )

        client.request = AsyncMock(
            return_value=(
                503,
                {},
                b'{"version":1,"code":"repair_busy","retryable":true}',
            )
        )
        with self.assertRaises(EngineArchiveError) as raised:
            await client.repair_par2(
                [(recovery_identity, "release.par2", 100)],
                [],
                selected_file_id,
            )
        self.assertEqual(raised.exception.code, "repair_busy")
        self.assertTrue(raised.exception.retryable)

        client.request = AsyncMock(
            return_value=(
                422,
                {},
                b'{"version":1,"code":"repair_insufficient","retryable":false,"required_recovery_blocks":17}',
            )
        )
        with self.assertRaises(EngineArchiveError) as raised:
            await client.repair_par2(
                [(recovery_identity, "release.par2", 100)],
                [],
                selected_file_id,
            )
        self.assertEqual(raised.exception.code, "repair_insufficient")
        self.assertEqual(raised.exception.required_recovery_blocks, 17)

        for invalid_hint in (False, 0, 32_769, "17"):
            client.request = AsyncMock(
                return_value=(
                    422,
                    {},
                    json.dumps(
                        {
                            "version": 1,
                            "code": "repair_insufficient",
                            "retryable": False,
                            "required_recovery_blocks": invalid_hint,
                        }
                    ).encode(),
                )
            )
            with self.assertRaisesRegex(
                EngineUnavailable, "invalid PAR2 repair failure"
            ):
                await client.repair_par2(
                    [(recovery_identity, "release.par2", 100)],
                    [],
                    selected_file_id,
                )

        for invalid in (
            dict(result, file_id="e" * 32),
            dict(result, identity="A" * 64),
            dict(result, asset_revision="A" * 64),
            dict(result, relative_path="../Movie.mkv"),
            dict(result, partial_source_mapped=1),
        ):
            client.request = AsyncMock(
                return_value=(200, {}, json.dumps(invalid).encode())
            )
            with self.assertRaisesRegex(EngineUnavailable, "invalid PAR2 repair"):
                await client.repair_par2(
                    [(recovery_identity, "release.par2", 100)],
                    [],
                    selected_file_id,
                )

    async def test_par2_repair_rejects_invalid_selected_id_before_request(self):
        client = EngineClient("/missing/engine.json")
        client.request = AsyncMock()

        with self.assertRaisesRegex(ValueError, "PAR2 repair request"):
            await client.repair_par2(
                [("a" * 64, "release.par2", 100)],
                [],
                "A" * 32,
            )

        client.request.assert_not_awaited()

        with self.assertRaisesRegex(ValueError, "partial source"):
            await client.repair_par2(
                [("a" * 64, "release.par2", 100)],
                [],
                "b" * 32,
                partial_sources=(),
            )

    async def test_par2_repair_uses_the_metadata_request_budget(self):
        client = EngineClient("/missing/engine.json")
        generation = "e" * 64
        client._provider_set_ids[generation] = "P" * 22
        client.request = AsyncMock(
            return_value=(
                422,
                {},
                b'{"version":1,"code":"repair_scope_exceeds_budget","retryable":false}',
            )
        )
        postings = [
            (number, 750_000, f"{number}.abcdefghijklmnopqrstuvwxyz@example.test")
            for number in range(1, 20_001)
        ]

        with self.assertRaises(EngineArchiveError) as raised:
            await client.repair_par2(
                [("a" * 64, "release.par2", 100)],
                [],
                "b" * 32,
                partial_sources=[(postings, "alt.video")],
                account_partition=b"a" * 32,
                provider_set_generation=generation,
            )

        self.assertEqual(raised.exception.code, "repair_scope_exceeds_budget")
        payload = client.request.await_args.args[2]
        self.assertGreater(len(payload), MAX_ENGINE_CONTROL_BYTES)
        self.assertLessEqual(len(payload), MAX_NZB_METADATA_BYTES)

    async def test_raw_composite_open_read_and_close_contract_is_closed(self):
        client = EngineClient("/missing/engine.json")
        first = "a" * 64
        second = "b" * 64
        identity = "c" * 64
        reader_lease_id = "L" * 22
        plan = {
            "set_identity": identity,
            "kind": {"layout": "raw_split"},
            "exact_size": 30,
            "volumes": [
                {
                    "content_identity": first,
                    "relative_path": "movie.mkv.001",
                    "number": 0,
                    "exact_size": 10,
                },
                {
                    "content_identity": second,
                    "relative_path": "movie.mkv.002",
                    "number": 1,
                    "exact_size": 20,
                },
            ],
        }
        client.request = AsyncMock(
            side_effect=[
                (
                    200,
                    {},
                    json.dumps(
                        {
                            "version": 1,
                            "identity": identity,
                            "exact_size": 30,
                            "etag": identity,
                            "plan": plan,
                        }
                    ).encode(),
                ),
                (
                    201,
                    {},
                    json.dumps(
                        {
                            "version": 1,
                            "source_identity": identity,
                            "reader_lease_id": reader_lease_id,
                            "extension": True,
                        }
                    ).encode(),
                ),
                (206, {"content-range": "bytes 8-12/30"}, b"abcde"),
                (204, {}, b""),
            ]
        )
        volumes = [
            (first, "movie.mkv.001", 10),
            (second, "movie.mkv.002", 20),
        ]

        self.assertEqual(
            await client.open_raw_composite(volumes),
            (identity, 30, identity),
        )
        self.assertEqual(
            client.request.await_args_list[0].args[:2],
            ("POST", "/v1/raw-composites"),
        )
        self.assertEqual(
            await client.open_raw_composite_reader(identity),
            reader_lease_id,
        )
        self.assertEqual(
            await client.read_raw_composite_range(
                identity,
                reader_lease_id,
                30,
                8,
                12,
            ),
            b"abcde",
        )
        self.assertEqual(
            client.request.await_args_list[2].args[:2],
            ("POST", f"/v1/raw-composites/{identity}/read"),
        )
        await client.close_raw_composite_reader(identity, reader_lease_id)

        client.request = AsyncMock(
            return_value=(
                200,
                {},
                json.dumps(
                    {
                        "version": 1,
                        "identity": "d" * 64,
                        "exact_size": 30,
                        "etag": "d" * 64,
                        "plan": plan,
                    }
                ).encode(),
            )
        )
        with self.assertRaisesRegex(EngineUnavailable, "invalid raw composite"):
            await client.open_raw_composite(volumes)

    async def test_native_session_range_is_exact_and_bounded(self):
        client = EngineClient("/missing/engine.json")
        identity = "A" * 22
        reader_lease_id = "L" * 22
        client.request = AsyncMock(
            return_value=(
                206,
                {
                    "content-range": "bytes 2-4/6",
                    "x-comet-usenet-salvage": "none",
                    "x-comet-usenet-salvaged-bytes": "0",
                    "x-comet-usenet-salvaged-holes": "0",
                },
                b"CDE",
            )
        )

        self.assertEqual(
            await client.read_session_range(identity, reader_lease_id, 6, 2, 4),
            b"CDE",
        )
        self.assertEqual(
            client.request.await_args.args[:2],
            ("POST", f"/v1/sessions/{identity}/read"),
        )
        with self.assertRaisesRegex(ValueError, "session range"):
            await client.read_session_range(identity, reader_lease_id, 6, 4, 2)
        with self.assertRaisesRegex(ValueError, "session range"):
            await client.read_session_range(
                identity,
                reader_lease_id,
                MAX_USENET_LOGICAL_BYTES + 1,
                0,
                0,
            )

        client.request = AsyncMock(
            return_value=(
                206,
                {
                    "content-range": "bytes 2-4/6",
                    "x-comet-usenet-salvage": "zero-fill",
                    "x-comet-usenet-salvaged-bytes": "3",
                    "x-comet-usenet-salvaged-holes": "1",
                },
                b"\0\0\0",
            )
        )
        self.assertEqual(
            await client.read_session_range(identity, reader_lease_id, 6, 2, 4),
            b"\0\0\0",
        )
        client.request.return_value = (
            206,
            {
                "content-range": "bytes 2-4/6",
                "x-comet-usenet-salvage": "none",
                "x-comet-usenet-salvaged-bytes": "3",
                "x-comet-usenet-salvaged-holes": "1",
            },
            b"CDE",
        )
        with self.assertRaisesRegex(EngineUnavailable, "salvage state"):
            await client.read_session_range(identity, reader_lease_id, 6, 2, 4)
        client.request.return_value = (
            206,
            {
                "content-range": "bytes 2-4/6",
                "x-comet-usenet-salvage": "zero-fill",
                "x-comet-usenet-salvaged-bytes": "4",
                "x-comet-usenet-salvaged-holes": "1",
            },
            b"\0\0\0",
        )
        with self.assertRaisesRegex(EngineUnavailable, "salvage state"):
            await client.read_session_range(identity, reader_lease_id, 6, 2, 4)

    async def test_native_session_range_preserves_retryable_engine_failures(self):
        client = EngineClient("/missing/engine.json")
        client.request = AsyncMock(
            return_value=(
                503,
                {},
                b'{"version":1,"code":"nntp_availability_unknown","retryable":true}',
            )
        )

        with self.assertRaises(EngineNntpError) as raised:
            await client.read_session_range("A" * 22, "L" * 22, 6, 2, 4)

        self.assertEqual(raised.exception.code, "nntp_availability_unknown")
        self.assertTrue(raised.exception.retryable)

    async def test_native_session_reader_uses_exact_stream_lifetime_routes(self):
        client = EngineClient("/missing/engine.json")
        identity = "A" * 22
        reader_lease_id = "L" * 22
        client.request = AsyncMock(
            side_effect=[
                (
                    201,
                    {},
                    json.dumps(
                        {
                            "version": 1,
                            "session_id": identity,
                            "reader_lease_id": reader_lease_id,
                            "extension": True,
                        }
                    ).encode(),
                ),
                (204, {}, b""),
            ]
        )

        self.assertEqual(
            await client.open_session_reader(identity),
            reader_lease_id,
        )
        await client.close_session_reader(identity, reader_lease_id)

        self.assertEqual(
            [call.args for call in client.request.await_args_list],
            [
                ("POST", f"/v1/sessions/{identity}/readers", b""),
                (
                    "DELETE",
                    f"/v1/sessions/{identity}/readers/{reader_lease_id}",
                    b"",
                ),
            ],
        )

    async def test_native_reader_open_preserves_typed_admission_failures(self):
        cases = (
            (
                "open_session_reader",
                "A" * 22,
                "session_reader_capacity",
                EngineNntpError,
                False,
            ),
            (
                "open_session_reader",
                "A" * 22,
                "session_busy",
                EngineNntpError,
                True,
            ),
            (
                "open_raw_composite_reader",
                "a" * 64,
                "raw_composite_reader_busy",
                EngineArchiveError,
                False,
            ),
            (
                "open_raw_composite_reader",
                "a" * 64,
                "raw_composite_busy",
                EngineArchiveError,
                True,
            ),
        )
        for method, identity, code, error_type, source_unavailable in cases:
            with self.subTest(method=method):
                client = EngineClient("/missing/engine.json")
                client.request = AsyncMock(
                    return_value=(
                        409,
                        {},
                        json.dumps(
                            {
                                "version": 1,
                                "code": code,
                                "retryable": True,
                            }
                        ).encode(),
                    )
                )

                with self.assertRaises(error_type) as raised:
                    await getattr(client, method)(identity)

                self.assertEqual(raised.exception.code, code)
                self.assertTrue(raised.exception.retryable)
                self.assertIs(raised.exception.source_unavailable, source_unavailable)

    async def test_engine_failure_version_rejects_boolean_alias(self):
        client = EngineClient("/missing/engine.json")
        client.request = AsyncMock(
            return_value=(
                409,
                {},
                b'{"version":true,"code":"session_reader_busy","retryable":true}',
            )
        )

        with self.assertRaisesRegex(EngineUnavailable, "invalid session-reader"):
            await client.open_session_reader("A" * 22)

    async def test_native_materialization_delegates_the_complete_posting_batch(self):
        client = EngineClient("/missing/engine.json")
        identity = "b" * 64
        asset_revision = "c" * 64
        client.request = AsyncMock(
            side_effect=[
                provider_set_registration(),
                (
                    200,
                    {},
                    f'{{"version":1,"identity":"{identity}","byte_size":3,'
                    f'"asset_revision":"{asset_revision}"}}'.encode(),
                ),
            ]
        )

        self.assertEqual(
            await client.materialize_nntp_postings(
                [
                    (1, 11, "first@example.test"),
                    (1, 12, "fallback@example.test"),
                    (2, 22, "second@example.test"),
                ],
                group="alt.video",
                servers=[
                    {
                        "provider_configuration_id": "primary",
                        "host": "xn--nws-jma.example.com",
                        "port": 119,
                        "tls_mode": "plaintext",
                        "allow_private": False,
                        "username": None,
                        "password": None,
                        "connections": 4,
                        "pipeline": 2,
                        "priority": 0,
                        "backup": False,
                    }
                ],
                account_partition=b"a" * 32,
                provider_set_generation="b" * 64,
            ),
            (identity, 3, asset_revision),
        )

        registration_call, materialization_call = client.request.await_args_list
        method, path, body = registration_call.args
        self.assertEqual((method, path), ("PUT", f"/v1/provider-sets/{'b' * 64}"))
        registration = json.loads(body)
        self.assertEqual(
            registration["servers"][0]["host"],
            "xn--nws-jma.example.com",
        )
        self.assertEqual(
            registration["servers"][0]["provider_configuration_id"], "primary"
        )
        self.assertEqual(registration["servers"][0]["connections"], 4)
        self.assertEqual(registration["servers"][0]["pipeline"], 2)
        self.assertFalse(registration["servers"][0]["backup"])
        method, path, body = materialization_call.args
        self.assertEqual((method, path), ("POST", "/v1/materializations"))
        payload = json.loads(body)
        self.assertEqual(
            [
                (posting["number"], posting["bytes"], posting["message_id"])
                for posting in payload["postings"]
            ],
            [
                (1, 11, "first@example.test"),
                (1, 12, "fallback@example.test"),
                (2, 22, "second@example.test"),
            ],
        )
        self.assertEqual(payload["group"], "alt.video")
        self.assertNotIn("servers", payload)
        self.assertEqual(payload["provider_set_id"], "P" * 22)
        self.assertNotIn("provider_set_generation", payload)

        with self.assertRaisesRegex(ValueError, "postings"):
            await client.materialize_nntp_postings(
                [(1, 1, "first@example.test"), (3, 1, "gap@example.test")],
                servers=registration["servers"],
                account_partition=b"a" * 32,
                provider_set_generation="b" * 64,
            )
        for declared_bytes in (0, True, 16 * 1024 * 1024 + 1):
            with self.subTest(declared_bytes=declared_bytes):
                with self.assertRaisesRegex(ValueError, "postings"):
                    await client.materialize_nntp_postings(
                        [(1, declared_bytes, "first@example.test")],
                        servers=registration["servers"],
                        account_partition=b"a" * 32,
                        provider_set_generation="b" * 64,
                    )
        client.request = AsyncMock(
            side_effect=[
                (
                    200,
                    {},
                    f'{{"version":1,"identity":"{identity}","byte_size":3}}'.encode(),
                ),
            ]
        )
        with self.assertRaisesRegex(EngineUnavailable, "invalid materialization"):
            await client.materialize_nntp_postings(
                [(1, 1, "first@example.test")],
                servers=registration["servers"],
                account_partition=b"a" * 32,
                provider_set_generation="b" * 64,
            )

        client.request = AsyncMock(
            return_value=(
                200,
                {},
                f'{{"version":1,"identity":"{identity}","byte_size":'
                f"{MAX_USENET_LOGICAL_BYTES + 1},"
                f'"asset_revision":"{asset_revision}"}}'.encode(),
            )
        )
        with self.assertRaisesRegex(EngineUnavailable, "invalid materialization"):
            await client.materialize_nntp_postings(
                [(1, 1, "first@example.test")],
                servers=registration["servers"],
                account_partition=b"a" * 32,
                provider_set_generation="b" * 64,
            )

        client.request = AsyncMock(
            side_effect=[
                (
                    503,
                    {},
                    b'{"version":1,"code":"native_busy","retryable":true}',
                ),
            ]
        )
        with self.assertRaises(EngineNntpError) as raised:
            await client.materialize_nntp_postings(
                [(1, 1, "first@example.test")],
                servers=registration["servers"],
                account_partition=b"a" * 32,
                provider_set_generation="b" * 64,
            )
        self.assertEqual(raised.exception.code, "native_busy")
        self.assertTrue(raised.exception.retryable)

    async def test_concurrent_materializations_register_one_provider_set(self):
        client = EngineClient("/missing/engine.json")
        identity = "c" * 64
        revision = "d" * 64
        registered = asyncio.Event()
        registration_calls = 0

        async def request(method, path, _body=b""):
            nonlocal registration_calls
            if method == "PUT":
                registration_calls += 1
                await asyncio.sleep(0)
                registered.set()
                return provider_set_registration()
            await registered.wait()
            return (
                200,
                {},
                (
                    f'{{"version":1,"identity":"{identity}",'
                    f'"byte_size":1,"asset_revision":"{revision}"}}'
                ).encode(),
            )

        client.request = AsyncMock(side_effect=request)
        kwargs = {
            "servers": [
                {
                    "provider_configuration_id": "primary",
                    "host": "news.example.test",
                    "port": 119,
                    "tls_mode": "plaintext",
                    "allow_private": False,
                    "username": None,
                    "password": None,
                    "connections": 4,
                    "pipeline": 2,
                    "priority": 0,
                    "backup": False,
                }
            ],
            "account_partition": b"a" * 32,
            "provider_set_generation": "b" * 64,
        }

        results = await asyncio.gather(
            *(
                client.materialize_nntp_postings(
                    [(1, 1, f"part-{index}@example.test")],
                    **kwargs,
                )
                for index in range(8)
            )
        )

        self.assertEqual(registration_calls, 1)
        self.assertEqual(results, [(identity, 1, revision)] * 8)
        self.assertEqual(
            sum(
                call.args[:2] == ("POST", "/v1/materializations")
                for call in client.request.await_args_list
            ),
            8,
        )

    async def test_native_inspection_registers_then_submits_only_bounded_probe_metadata(
        self,
    ):
        client = EngineClient("/missing/engine.json")
        artifact_sha256 = "a" * 64
        evidence = {
            "version": 1,
            "artifact_sha256": artifact_sha256,
            "inspection_state": "provisionally_streamable",
            "container": "mp4",
            "duration_millis": 90_500,
            "inspected_head_bytes": 2048,
            "inspected_tail_bytes": 1024,
        }
        client.request = AsyncMock(
            side_effect=[
                provider_set_registration(),
                (200, {}, json.dumps(evidence).encode()),
            ]
        )
        servers = [
            {
                "provider_configuration_id": "primary",
                "host": "news.example.test",
                "port": 119,
                "tls_mode": "plaintext",
                "allow_private": False,
                "username": "user",
                "password": "secret",
                "connections": 4,
                "pipeline": 2,
                "priority": 0,
                "backup": False,
            }
        ]

        self.assertEqual(
            await client.inspect_nntp_postings(
                artifact_sha256,
                [(1, 11, "first@example.test")],
                group="alt.video",
                servers=servers,
                account_partition=b"a" * 32,
                provider_set_generation="b" * 64,
            ),
            evidence,
        )
        registration_call, inspection_call = client.request.await_args_list
        self.assertEqual(
            registration_call.args[:2], ("PUT", f"/v1/provider-sets/{'b' * 64}")
        )
        registration = json.loads(registration_call.args[2])
        self.assertEqual(registration["servers"][0]["username"], "user")
        self.assertEqual(registration["servers"][0]["password"], "secret")
        method, path, body = inspection_call.args
        self.assertEqual(
            (method, path),
            ("POST", f"/v1/artifacts/{artifact_sha256}/native-inspect"),
        )
        payload = json.loads(body)
        self.assertNotIn("expected_extension", payload)
        self.assertEqual(payload["postings"][0]["bytes"], 11)
        self.assertNotIn("servers", payload)
        self.assertEqual(payload["provider_set_id"], "P" * 22)

    async def test_native_inspection_rejects_only_consumed_unbounded_evidence(
        self,
    ):
        client = EngineClient("/missing/engine.json")
        artifact_sha256 = "a" * 64
        servers = [
            {
                "provider_configuration_id": "primary",
                "host": "news.example.test",
                "port": 119,
                "tls_mode": "plaintext",
                "allow_private": False,
                "username": None,
                "password": None,
                "connections": 4,
                "pipeline": 2,
                "priority": 0,
                "backup": False,
            }
        ]
        invalid = {
            "version": 1,
            "artifact_sha256": "c" * 64,
            "inspection_state": "provisionally_streamable",
            "duration_millis": None,
            "inspected_head_bytes": 2 * 1024 * 1024 + 1,
            "inspected_tail_bytes": 0,
        }
        client.request = AsyncMock(
            side_effect=[
                provider_set_registration(),
                (200, {}, json.dumps(invalid).encode()),
            ]
        )

        with self.assertRaisesRegex(EngineUnavailable, "inspection"):
            await client.inspect_nntp_postings(
                artifact_sha256,
                [(1, 11, "first@example.test")],
                servers=servers,
                account_partition=b"a" * 32,
                provider_set_generation="b" * 64,
            )
        client.request = AsyncMock(
            return_value=(
                200,
                {},
                json.dumps(
                    {
                        **invalid,
                        "artifact_sha256": artifact_sha256,
                        "inspected_head_bytes": 1024,
                    }
                ).encode(),
            )
        )
        evidence = await client.inspect_nntp_postings(
            artifact_sha256,
            [(1, 11, "first@example.test")],
            servers=servers,
            account_partition=b"a" * 32,
            provider_set_generation="b" * 64,
        )
        self.assertNotIn("container", evidence)

    async def test_materialization_inspection_is_closed_and_identity_bound(self):
        client = EngineClient("/missing/engine.json")
        identity = "a" * 64
        evidence = {
            "version": 1,
            "materialization_identity": identity,
            "source_identity": identity,
            "inspection_state": "provisionally_streamable",
            "container": "matroska",
            "duration_millis": None,
            "inspected_head_bytes": 4096,
            "inspected_tail_bytes": 0,
        }
        client.request = AsyncMock(
            return_value=(200, {}, json.dumps(evidence).encode())
        )

        self.assertEqual(
            await client.inspect_materialization(identity, 4096),
            evidence,
        )
        method, path, body = client.request.await_args.args
        self.assertEqual(
            (method, path),
            ("POST", f"/v1/materializations/{identity}/native-inspect"),
        )
        self.assertEqual(
            json.loads(body),
            {"expected_size": 4096},
        )

        client.request = AsyncMock(
            return_value=(
                200,
                {},
                json.dumps({**evidence, "materialization_identity": "b" * 64}).encode(),
            )
        )
        with self.assertRaisesRegex(EngineUnavailable, "materialization inspection"):
            await client.inspect_materialization(identity, 4096)

    async def test_materialization_inspection_preserves_typed_failures(self):
        client = EngineClient("/missing/engine.json")
        client.request = AsyncMock(
            return_value=(
                422,
                {},
                b'{"version":1,"code":"container_signature_mismatch","retryable":false}',
            )
        )

        with self.assertRaises(EngineArchiveError) as raised:
            await client.inspect_materialization("a" * 64, 100)

        self.assertEqual(raised.exception.code, "container_signature_mismatch")
        self.assertFalse(raised.exception.retryable)

    async def test_raw_composite_inspection_is_source_bound(self):
        client = EngineClient("/missing/engine.json")
        identity = "a" * 64
        evidence = {
            "version": 1,
            "source_identity": identity,
            "inspection_state": "provisionally_streamable",
            "container": "mp4",
            "duration_millis": 1000,
            "inspected_head_bytes": 2048,
            "inspected_tail_bytes": 1024,
        }
        client.request = AsyncMock(
            return_value=(200, {}, json.dumps(evidence).encode())
        )

        self.assertEqual(
            await client.inspect_raw_composite(identity, 4096),
            evidence,
        )
        method, path, body = client.request.await_args.args
        self.assertEqual(
            (method, path),
            ("POST", f"/v1/raw-composites/{identity}/native-inspect"),
        )
        self.assertEqual(
            json.loads(body),
            {"expected_size": 4096},
        )

    async def test_native_catalog_sends_the_canonical_identity_and_validates_assets(
        self,
    ):
        client = EngineClient("/missing/engine.json")
        artifact_sha256 = "a" * 64
        manifest_identity = "nm1:" + "b" * 64
        metadata = {"password": "archive-secret"}
        manifest = [{"subject": "release"}]
        digest = hashlib.sha256()
        digest.update(b"comet-nzb-asset-v1\0")
        digest.update(bytes.fromhex(artifact_sha256))
        digest.update((0).to_bytes(4, "big"))
        path = b"Movie.mkv"
        digest.update(len(path).to_bytes(4, "big"))
        digest.update(path)
        asset = {
            "asset_id": digest.hexdigest(),
            "file_index": 0,
            "relative_path": "Movie.mkv",
            "declared_bytes": 42,
            "kind": "video",
        }
        client.request = AsyncMock(
            return_value=(
                200,
                {},
                json.dumps(
                    {
                        "version": 1,
                        "artifact_sha256": artifact_sha256,
                        "assets": [asset],
                    }
                ).encode(),
            )
        )

        self.assertEqual(
            await client.catalog_nntp_artifact(
                artifact_sha256, manifest_identity, metadata, manifest
            ),
            [asset],
        )
        method, path, body = client.request.await_args.args
        self.assertEqual(
            (method, path),
            ("POST", f"/v1/artifacts/{artifact_sha256}/native-catalog"),
        )
        self.assertEqual(
            json.loads(body),
            {
                "manifest_identity": manifest_identity,
                "metadata": metadata,
                "manifest": manifest,
            },
        )

        await client.catalog_nntp_artifact(
            artifact_sha256,
            manifest_identity,
            metadata,
            manifest,
            selection_hint=("Movie.mkv", 42),
        )
        self.assertEqual(
            json.loads(client.request.await_args.args[2])["selection_hint"],
            {"relative_path": "Movie.mkv", "exact_size": 42},
        )

        with self.assertRaisesRegex(ValueError, "selection hint"):
            await client.catalog_nntp_artifact(
                artifact_sha256,
                manifest_identity,
                metadata,
                manifest,
                selection_hint=(
                    "Movie.mkv",
                    MAX_USENET_LOGICAL_BYTES + 1,
                ),
            )

        invalid = {**asset, "file_index": 1}
        client.request = AsyncMock(
            return_value=(
                200,
                {},
                json.dumps(
                    {
                        "version": 1,
                        "artifact_sha256": artifact_sha256,
                        "assets": [invalid],
                    }
                ).encode(),
            )
        )
        with self.assertRaisesRegex(EngineUnavailable, "asset catalog"):
            await client.catalog_nntp_artifact(
                artifact_sha256, manifest_identity, metadata, manifest
            )
        client.request = AsyncMock(
            return_value=(
                200,
                {},
                json.dumps(
                    {
                        "version": 1,
                        "artifact_sha256": artifact_sha256,
                        "assets": [
                            {
                                **asset,
                                "declared_bytes": MAX_USENET_LOGICAL_BYTES + 1,
                            }
                        ],
                    }
                ).encode(),
            )
        )
        with self.assertRaisesRegex(EngineUnavailable, "asset catalog"):
            await client.catalog_nntp_artifact(
                artifact_sha256, manifest_identity, metadata, manifest
            )

    async def test_native_session_open_delegates_the_posting_map_without_materializing(
        self,
    ):
        client = EngineClient("/missing/engine.json")
        identity = "C" * 22
        revision = "d" * 64
        asset_revision = "e" * 64
        client.request = AsyncMock(
            side_effect=[
                provider_set_registration(),
                (
                    200,
                    {},
                    f'{{"version":1,"identity":"{identity}","byte_size":9,'
                    f'"revision":"{revision}","asset_revision":"{asset_revision}"}}'.encode(),
                ),
            ]
        )
        servers = [
            {
                "provider_configuration_id": "primary",
                "host": "news.example.test",
                "port": 119,
                "tls_mode": "plaintext",
                "allow_private": False,
                "username": None,
                "password": None,
                "connections": 4,
                "pipeline": 2,
                "priority": 0,
                "backup": False,
            }
        ]

        self.assertEqual(
            await client.open_nntp_session(
                [(1, 11, "first@example.test"), (2, 22, "second@example.test")],
                servers=servers,
                account_partition=b"a" * 32,
                provider_set_generation="b" * 64,
            ),
            (identity, 9, revision, asset_revision),
        )
        registration_call, session_call = client.request.await_args_list
        self.assertEqual(
            registration_call.args[:2], ("PUT", f"/v1/provider-sets/{'b' * 64}")
        )
        method, path, body = session_call.args
        self.assertEqual((method, path), ("POST", "/v1/sessions"))
        payload = json.loads(body)
        self.assertIs(payload["allow_degraded_playback"], False)
        self.assertIs(payload["preparation"], False)
        self.assertEqual(payload["postings"][1]["message_id"], "second@example.test")
        self.assertEqual(payload["postings"][1]["bytes"], 22)
        self.assertNotIn("servers", payload)

        client.request = AsyncMock(
            return_value=(
                200,
                {},
                f'{{"version":1,"identity":"{identity}","byte_size":9,'
                f'"revision":"{revision}","asset_revision":null}}'.encode(),
            )
        )
        await client.open_nntp_session(
            [(1, 11, "first@example.test")],
            servers=servers,
            account_partition=b"a" * 32,
            provider_set_generation="b" * 64,
            allow_degraded_playback=True,
            preparation=True,
        )
        payload = json.loads(client.request.await_args.args[2])
        self.assertIs(payload["allow_degraded_playback"], True)
        self.assertIs(payload["preparation"], True)
