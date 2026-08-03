"""Client for the replica-local Usenet engine Unix socket."""

import asyncio
import hashlib
from pathlib import Path

import orjson

from comet.observability import log
from comet.usenet.archive_paths import normalize_archive_relative_path
from comet.usenet.engine_stats import (
    ENGINE_STAT_BOOLEAN_FIELDS,
    ENGINE_STAT_FIELDS,
    ENGINE_STAT_INTEGER_FIELDS,
)
from comet.usenet.engine_transport import (
    MAX_ENGINE_NZB_METADATA_BYTES,
    MAX_ENGINE_PROVIDER_SET_BYTES,
    MAX_ENGINE_RANGE_BYTES,
    EngineDescriptor,
    EngineTransport,
    EngineUnavailable,
    _decode_engine_failure,
    _has_fields,
)
from comet.usenet.identity import archive_member_identity as _archive_member_identity
from comet.usenet.identity import is_sha256_hex
from comet.usenet.limits import (
    MAX_ARCHIVE_VOLUMES,
    MAX_NZB_FILES,
    MAX_NZB_METADATA_BYTES,
    MAX_NZB_SEGMENTS,
    MAX_PAR2_VOLUMES,
    MAX_USENET_LOGICAL_BYTES,
)

MAX_ENGINE_ARTICLE_BYTES = 16 * 1024 * 1024
MAX_ENGINE_STRUCTURAL_END_BYTES = 2 * 1024 * 1024
MAX_ARCHIVE_PASSPHRASE_BYTES = 4 * 1024
_ASSET_KINDS = frozenset(
    {
        "video",
        "archive",
        "split",
        "logical_split",
        "logical_archive",
        "par2",
        "par2_source",
    }
)
_NNTP_AUTH_FAILURES = frozenset({"nntp_auth_failed", "nntp_auth_required"})
_NNTP_SOURCE_FAILURES = frozenset(
    {
        "invalid_yenc_header",
        "invalid_yenc_field",
        "invalid_yenc_crc",
        "invalid_yenc_part",
        "invalid_yenc_range",
        "missing_ybegin",
        "missing_yend",
        "missing_yenc_field",
        "missing_yenc_crc",
        "nntp_article_missing",
        "trailing_yenc_data",
        "truncated_yenc_escape",
        "yenc_length_mismatch",
        "yenc_crc_mismatch",
    }
)


class EngineParseError(RuntimeError):
    pass


class EngineNntpError(RuntimeError):
    def __init__(self, code: str, *, retryable: bool = False):
        super().__init__(code)
        self.code = code
        self.retryable = retryable
        self.auth_failed = code in _NNTP_AUTH_FAILURES
        self.source_unavailable = code == "session_unavailable"
        self.source_failure = code in _NNTP_SOURCE_FAILURES


class EngineArchiveError(RuntimeError):
    def __init__(
        self,
        code: str,
        *,
        retryable: bool,
        required_recovery_blocks: int | None = None,
    ):
        super().__init__(code)
        self.code = code
        self.retryable = retryable
        self.required_recovery_blocks = required_recovery_blocks
        self.auth_failed = False
        self.source_unavailable = code == "raw_composite_unavailable"
        self.source_failure = code in _NNTP_SOURCE_FAILURES


def _validate_message_id(message_id: object) -> None:
    if (
        not isinstance(message_id, str)
        or not message_id
        or len(message_id) > 998
        or any(character.isspace() or ord(character) < 32 for character in message_id)
    ):
        raise ValueError("NNTP message identifier is invalid")
    bracketed = message_id.startswith("<")
    if bracketed != message_id.endswith(">"):
        raise ValueError("NNTP message identifier is invalid")
    value = message_id[1:-1] if bracketed else message_id
    if not value or "<" in value or ">" in value:
        raise ValueError("NNTP message identifier is invalid")


def _validate_materialization_identity(identity: object) -> None:
    if not is_sha256_hex(identity):
        raise ValueError("materialization identity must be lowercase SHA-256")


def _validate_asset_revision(revision: object) -> None:
    if not is_sha256_hex(revision):
        raise ValueError("asset revision must be lowercase SHA-256")


def _valid_logical_size(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, int)
        and 1 <= value <= MAX_USENET_LOGICAL_BYTES
    )


def _validate_archive_passphrase(passphrase: str | None) -> None:
    if passphrase is None:
        return
    if not isinstance(passphrase, str) or not passphrase:
        raise ValueError("archive passphrase is invalid")
    try:
        encoded = passphrase.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("archive passphrase is invalid") from exc
    if len(encoded) > MAX_ARCHIVE_PASSPHRASE_BYTES or any(
        character.isascii() and not character.isprintable() for character in passphrase
    ):
        raise ValueError("archive passphrase is invalid")


def _prepare_archive_volume_request(
    volumes: list[tuple[str, str, int]],
) -> tuple[
    bytes,
    dict[str, object],
    set[str],
    dict[str, tuple[str, int]],
]:
    if not isinstance(volumes, list) or not 1 <= len(volumes) <= MAX_ARCHIVE_VOLUMES:
        raise ValueError("archive volume plan request is invalid")
    payload_volumes = []
    identities = set()
    paths = set()
    expected = {}
    total_expected_size = 0
    for volume in volumes:
        if not isinstance(volume, tuple) or len(volume) != 3:
            raise ValueError("archive volume plan request is invalid")
        identity, relative_path, expected_size = volume
        _validate_materialization_identity(identity)
        normalized_path = normalize_archive_relative_path(relative_path)
        if (
            normalized_path is None
            or not _valid_logical_size(expected_size)
            or identity in identities
            or normalized_path.lower() in paths
        ):
            raise ValueError("archive volume plan request is invalid")
        identities.add(identity)
        paths.add(normalized_path.lower())
        expected[identity] = (normalized_path, expected_size)
        total_expected_size += expected_size
        if total_expected_size > MAX_USENET_LOGICAL_BYTES:
            raise ValueError("archive volume plan request is invalid")
        payload_volumes.append(
            {
                "content_identity": identity,
                "relative_path": relative_path,
                "expected_size": expected_size,
            }
        )
    request = {"volumes": payload_volumes}
    payload = orjson.dumps(
        request,
    )
    if len(payload) > MAX_ENGINE_NZB_METADATA_BYTES:
        raise ValueError("archive volume plan request exceeds the engine input limit")
    return payload, request, identities, expected


def _prepare_par2_volume_request(
    files: list[tuple[str, str, int]],
    *,
    error: str,
) -> tuple[bytes, dict[str, object], set[str], dict[str, tuple[str, int]]]:
    if len(files) > MAX_PAR2_VOLUMES:
        raise ValueError(error)
    return _prepare_archive_volume_request(files)


def _prepare_session_archive_request(
    volumes: list[tuple[str, str, str, int]],
    *,
    passphrase: str | None = None,
) -> tuple[bytes, dict[str, object], set[str], dict[str, tuple[str, int]]]:
    if not isinstance(volumes, list) or not 1 <= len(volumes) <= MAX_ARCHIVE_VOLUMES:
        raise ValueError("session archive request is invalid")
    payload_volumes = []
    revisions = set()
    session_ids = set()
    paths = set()
    expected = {}
    total_size = 0
    for volume in volumes:
        if not isinstance(volume, tuple) or len(volume) != 4:
            raise ValueError("session archive request is invalid")
        session_id, revision, relative_path, exact_size = volume
        _validate_session_identity(session_id)
        _validate_asset_revision(revision)
        normalized_path = normalize_archive_relative_path(relative_path)
        if (
            normalized_path is None
            or not _valid_logical_size(exact_size)
            or session_id in session_ids
            or revision in revisions
            or normalized_path.lower() in paths
        ):
            raise ValueError("session archive request is invalid")
        total_size += exact_size
        if total_size > MAX_USENET_LOGICAL_BYTES:
            raise ValueError("session archive request is invalid")
        session_ids.add(session_id)
        revisions.add(revision)
        paths.add(normalized_path.lower())
        expected[revision] = (normalized_path, exact_size)
        payload_volumes.append(
            {
                "session_id": session_id,
                "revision": revision,
                "relative_path": normalized_path,
                "expected_size": exact_size,
            }
        )
    request = {"volumes": payload_volumes}
    if passphrase is not None:
        request["passphrase"] = passphrase
    payload = orjson.dumps(request)
    if len(payload) > MAX_ENGINE_NZB_METADATA_BYTES:
        raise ValueError("session archive request exceeds the engine input limit")
    return payload, request, revisions, expected


def _validate_archive_volume_plan(
    plan: object,
    identities: set[str],
    expected: dict[str, tuple[str, int]],
) -> dict[str, object]:
    if not _has_fields(
        plan,
        {"set_identity", "kind", "exact_size", "volumes"},
    ):
        raise EngineUnavailable("Usenet engine returned invalid archive plan data")
    set_identity = plan["set_identity"]
    kind = plan["kind"]
    planned_volumes = plan["volumes"]
    exact_size = plan["exact_size"]
    if (
        not is_sha256_hex(set_identity)
        or not isinstance(kind, dict)
        or not isinstance(planned_volumes, list)
        or len(planned_volumes) != len(identities)
        or not _valid_logical_size(exact_size)
    ):
        raise EngineUnavailable("Usenet engine returned invalid archive plan data")
    layout = kind.get("layout")
    if layout == "raw_split":
        if len(planned_volumes) < 2:
            raise EngineUnavailable("Usenet engine returned invalid archive plan data")
    elif layout in {"single_archive", "multi_volume_archive"}:
        if kind.get("format") not in {
            "rar4",
            "rar5",
            "seven_zip",
            "zip",
            "gzip",
            "tar",
        }:
            raise EngineUnavailable("Usenet engine returned invalid archive plan data")
        if layout == "single_archive" and len(planned_volumes) != 1:
            raise EngineUnavailable("Usenet engine returned invalid archive plan data")
    else:
        raise EngineUnavailable("Usenet engine returned invalid archive plan data")
    seen = set()
    total = 0
    for number, volume in enumerate(planned_volumes):
        if not _has_fields(
            volume,
            {"content_identity", "relative_path", "number", "exact_size"},
        ):
            raise EngineUnavailable("Usenet engine returned invalid archive plan data")
        identity = volume["content_identity"]
        size = volume["exact_size"]
        relative_path = volume["relative_path"]
        if (
            identity not in expected
            or identity in seen
            or isinstance(volume["number"], bool)
            or not isinstance(volume["number"], int)
            or volume["number"] != number
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size != expected[identity][1]
            or not isinstance(relative_path, str)
            or relative_path != expected[identity][0]
        ):
            raise EngineUnavailable("Usenet engine returned invalid archive plan data")
        seen.add(identity)
        total += size
    if seen != identities or total != exact_size:
        raise EngineUnavailable("Usenet engine returned invalid archive plan data")
    return plan


def _validate_session_identity(identity: object) -> None:
    if (
        not isinstance(identity, str)
        or len(identity) != 22
        or any(
            not character.isascii()
            or (not character.isalnum() and character not in "-_")
            for character in identity
        )
    ):
        raise ValueError("session identity must be 128-bit base64url")


def _validate_provider_set_identity(identity: object) -> None:
    try:
        _validate_session_identity(identity)
    except ValueError as exc:
        raise ValueError(
            "native provider-set identity must be 128-bit base64url"
        ) from exc


def _normalize_nntp_postings(
    postings: object,
    *,
    error: str,
) -> list[dict[str, object]]:
    if not isinstance(postings, list) or not 1 <= len(postings) <= MAX_NZB_SEGMENTS:
        raise ValueError(error)
    normalized = []
    previous_number = 0
    for posting in postings:
        if (
            not isinstance(posting, tuple)
            or len(posting) != 3
            or isinstance(posting[0], bool)
            or not isinstance(posting[0], int)
            or posting[0] < previous_number
            or posting[0] > previous_number + 1
            or (previous_number == 0 and posting[0] != 1)
            or isinstance(posting[1], bool)
            or not isinstance(posting[1], int)
            or not 1 <= posting[1] <= MAX_ENGINE_ARTICLE_BYTES
        ):
            raise ValueError(error)
        _validate_message_id(posting[2])
        previous_number = posting[0]
        normalized.append(
            {"number": posting[0], "bytes": posting[1], "message_id": posting[2]}
        )
    return normalized


class EngineClient(EngineTransport):
    def __init__(self, descriptor_path: str | Path):
        super().__init__(descriptor_path)
        self._provider_set_ids: dict[str, str] = {}
        self._provider_set_locks: dict[str, asyncio.Lock] = {}
        self._reported_degraded_sessions: set[str] = set()

    def _load_descriptor(self) -> EngineDescriptor:
        previous = self._cached_descriptor
        try:
            descriptor = super()._load_descriptor()
        except EngineUnavailable:
            self._provider_set_ids.clear()
            raise
        if previous is None or previous.runtime_id != descriptor.runtime_id:
            self._provider_set_ids.clear()
        return descriptor

    async def health(self) -> dict:
        status, _headers, body = await self.request("GET", "/v1/health")
        if status != 200:
            raise EngineUnavailable("Usenet engine is unhealthy")
        try:
            payload = orjson.loads(body)
        except ValueError as exc:
            raise EngineUnavailable(
                "Usenet engine returned invalid health data"
            ) from exc
        if (
            not _has_fields(payload, {"version", "mode"})
            or isinstance(payload.get("version"), bool)
            or payload["version"] != 1
            or not isinstance(payload["mode"], str)
            or not payload["mode"]
        ):
            raise EngineUnavailable("Usenet engine returned invalid health data")
        return payload

    async def drain(self) -> None:
        """Stop admission and let the replica-local runtime finish active work."""
        status, _headers, body = await self.request("POST", "/v1/drain")
        try:
            payload = orjson.loads(body)
        except ValueError as exc:
            raise EngineUnavailable(
                "Usenet engine returned invalid drain data"
            ) from exc
        if (
            status != 202
            or not _has_fields(payload, {"version", "draining"})
            or payload["version"] != 1
            or payload["draining"] is not True
        ):
            raise EngineUnavailable("Usenet engine rejected drain")

    async def resume(self) -> None:
        """Resume admission on the replica-local runtime."""
        status, _headers, body = await self.request("POST", "/v1/resume")
        try:
            payload = orjson.loads(body)
        except ValueError as exc:
            raise EngineUnavailable(
                "Usenet engine returned invalid resume data"
            ) from exc
        if (
            status != 200
            or not _has_fields(payload, {"version", "draining"})
            or payload["version"] != 1
            or payload["draining"] is not False
        ):
            raise EngineUnavailable("Usenet engine rejected resume")

    async def stats(self) -> dict[str, int | bool]:
        """Read one bounded, credential-free native runtime snapshot."""
        status, _headers, body = await self.request("GET", "/v1/stats")
        try:
            payload = orjson.loads(body)
        except ValueError as exc:
            raise EngineUnavailable(
                "Usenet engine returned invalid stats data"
            ) from exc
        if (
            status != 200
            or not _has_fields(payload, {"version", *ENGINE_STAT_FIELDS})
            or isinstance(payload.get("version"), bool)
            or payload.get("version") != 1
            or any(
                not isinstance(payload.get(field), bool)
                for field in ENGINE_STAT_BOOLEAN_FIELDS
            )
            or any(
                isinstance(payload.get(field), bool)
                or not isinstance(payload.get(field), int)
                or not 0 <= payload[field] <= 2**64 - 1
                for field in ENGINE_STAT_INTEGER_FIELDS
            )
        ):
            raise EngineUnavailable("Usenet engine returned invalid stats data")
        return {field: payload[field] for field in ENGINE_STAT_FIELDS}

    async def parse_nzb(self, artifact_sha256: str, document: bytes) -> dict:
        """Parse a brokered NZB document without authorizing native media work."""
        if not is_sha256_hex(artifact_sha256):
            raise ValueError("artifact_sha256 must be lowercase SHA-256")
        if (
            not isinstance(document, bytes)
            or not document
            or len(document) > MAX_NZB_METADATA_BYTES
        ):
            raise ValueError("NZB document exceeds the parser input limit")
        if hashlib.sha256(document).hexdigest() != artifact_sha256:
            raise ValueError("NZB document does not match its artifact identity")
        status, _headers, body = await self.request(
            "POST", f"/v1/artifacts/{artifact_sha256}/parse", document
        )
        try:
            payload = orjson.loads(body)
        except ValueError as exc:
            raise EngineUnavailable(
                "Usenet engine returned invalid parse data"
            ) from exc
        if status != 200:
            code, _retryable = _decode_engine_failure(
                payload,
                label="NZB parse",
            )
            raise EngineParseError(code)
        expected_keys = {
            "version",
            "files",
            "segments",
            "nh1",
            "nm1",
            "metadata",
            "manifest",
        }
        if (
            not _has_fields(payload, expected_keys)
            or isinstance(payload.get("version"), bool)
            or payload.get("version") != 2
            or isinstance(payload.get("files"), bool)
            or not isinstance(payload.get("files"), int)
            or not 1 <= payload["files"] <= MAX_NZB_FILES
            or isinstance(payload.get("segments"), bool)
            or not isinstance(payload.get("segments"), int)
            or not 1 <= payload["segments"] <= MAX_NZB_SEGMENTS
            or not isinstance(payload.get("nh1"), str)
            or not isinstance(payload.get("nm1"), str)
            or not isinstance(payload.get("metadata"), dict)
            or any(
                not isinstance(key, str)
                or not isinstance(value, str)
                or not key
                or not value
                for key, value in payload["metadata"].items()
            )
            or not isinstance(payload.get("manifest"), list)
            or len(payload["manifest"]) != payload["files"]
            or len(payload["nh1"]) != 44
            or not payload["nh1"].startswith("nh1:")
            or any(
                character not in "0123456789abcdef" for character in payload["nh1"][4:]
            )
            or len(payload["nm1"]) != 68
            or not payload["nm1"].startswith("nm1:")
            or any(
                character not in "0123456789abcdef" for character in payload["nm1"][4:]
            )
            or any(
                not isinstance(file, dict)
                or not isinstance(file.get("postings"), list)
                or not file["postings"]
                for file in payload["manifest"]
            )
            or sum(len(file["postings"]) for file in payload["manifest"])
            != payload["segments"]
        ):
            raise EngineUnavailable("Usenet engine returned invalid parse data")
        return payload

    async def plan_archive_volumes(
        self,
        volumes: list[tuple[str, str, int]],
    ) -> dict[str, object]:
        """Reopen immutable parts and derive one header-proven archive/raw-split plan."""
        payload, _request, identities, expected = _prepare_archive_volume_request(
            volumes
        )
        status, _headers, body = await self.request("POST", "/v1/archive-plan", payload)
        try:
            response = orjson.loads(body)
        except ValueError as exc:
            raise EngineUnavailable(
                "Usenet engine returned invalid archive plan data"
            ) from exc
        if (
            not isinstance(response, dict)
            or isinstance(response.get("version"), bool)
            or response.get("version") != 1
        ):
            raise EngineUnavailable("Usenet engine returned invalid archive plan data")
        if status != 200:
            code, retryable = _decode_engine_failure(
                response,
                label="archive",
            )
            raise EngineArchiveError(code, retryable=retryable)
        if not _has_fields(response, {"version", "plan"}):
            raise EngineUnavailable("Usenet engine returned invalid archive plan data")
        return _validate_archive_volume_plan(response.get("plan"), identities, expected)

    async def catalog_stored_archive_volumes(
        self,
        volumes: list[tuple[str, str, int]],
    ) -> tuple[dict[str, object], list[dict[str, object]]]:
        """Map eligible stored RAR members without materializing a joined archive."""
        payload, _request, identities, expected = _prepare_archive_volume_request(
            volumes
        )
        return await self._catalog_archive_request(
            payload,
            identities,
            expected,
            "/v1/archive-direct/catalog",
        )

    async def catalog_session_archive_volumes(
        self,
        volumes: list[tuple[str, str, str, int]],
        *,
        passphrase: str | None = None,
    ) -> tuple[dict[str, object], list[dict[str, object]]]:
        """Catalog seekable archive members directly over sparse NNTP sessions."""
        _validate_archive_passphrase(passphrase)
        payload, _request, identities, expected = _prepare_session_archive_request(
            volumes, passphrase=passphrase
        )
        return await self._catalog_archive_request(
            payload,
            identities,
            expected,
            "/v1/session-archives/catalog",
        )

    async def catalog_nested_archive_volumes(
        self,
        volumes: list[tuple[str, str, int]],
        *,
        passphrase: str | None = None,
    ) -> tuple[dict[str, object], list[dict[str, object]]]:
        """Resolve bounded nested layers under one native cumulative budget."""
        _validate_archive_passphrase(passphrase)
        _payload, request, identities, expected = _prepare_archive_volume_request(
            volumes
        )
        if passphrase is not None:
            request["passphrase"] = passphrase
        payload = orjson.dumps(request)
        if len(payload) > MAX_ENGINE_NZB_METADATA_BYTES:
            raise ValueError(
                "nested archive catalog request exceeds the engine input limit"
            )
        return await self._catalog_archive_request(
            payload,
            identities,
            expected,
            "/v1/archive-nested/catalog",
            nested=True,
        )

    async def _catalog_archive_request(
        self,
        payload: bytes,
        identities: set[str],
        expected: dict[str, tuple[str, int]],
        path: str,
        *,
        nested: bool = False,
    ) -> tuple[dict[str, object], list[dict[str, object]]]:
        status, _headers, body = await self.request("POST", path, payload)
        try:
            response = orjson.loads(body)
        except ValueError as exc:
            raise EngineUnavailable(
                "Usenet engine returned invalid archive catalog data"
            ) from exc
        if (
            not isinstance(response, dict)
            or isinstance(response.get("version"), bool)
            or response.get("version") != 1
        ):
            raise EngineUnavailable(
                "Usenet engine returned invalid archive catalog data"
            )
        if status != 200:
            code, retryable = _decode_engine_failure(
                response,
                label="archive-catalog",
            )
            raise EngineArchiveError(code, retryable=retryable)
        if not _has_fields(response, {"version", "plan", "members"}):
            raise EngineUnavailable(
                "Usenet engine returned invalid archive catalog data"
            )
        plan = _validate_archive_volume_plan(response["plan"], identities, expected)
        members = response["members"]
        if not isinstance(members, list) or len(members) > MAX_NZB_FILES:
            raise EngineUnavailable(
                "Usenet engine returned invalid archive catalog data"
            )
        paths = set()
        for member in members:
            expected_keys = {
                "member_id",
                "relative_path",
                "exact_size",
                "kind",
            }
            if nested:
                expected_keys.add("selected_paths")
            if not _has_fields(member, expected_keys):
                raise EngineUnavailable(
                    "Usenet engine returned invalid archive catalog data"
                )
            member_id = member["member_id"]
            relative_path = member["relative_path"]
            exact_size = member["exact_size"]
            kind = member["kind"]
            selected_paths = member.get("selected_paths")
            if (
                not is_sha256_hex(member_id)
                or normalize_archive_relative_path(relative_path) != relative_path
                or not _valid_logical_size(exact_size)
                or kind not in _ASSET_KINDS
                or member_id
                != _archive_member_identity(
                    plan["set_identity"], relative_path, exact_size
                )
                or relative_path.lower() in paths
                or nested
                and (
                    kind != "video"
                    or not isinstance(selected_paths, list)
                    or not 1 <= len(selected_paths) <= 4
                    or any(
                        normalize_archive_relative_path(selected_path) != selected_path
                        for selected_path in selected_paths
                    )
                    or relative_path != "!/".join(selected_paths)
                )
            ):
                raise EngineUnavailable(
                    "Usenet engine returned invalid archive catalog data"
                )
            paths.add(relative_path.lower())
        return plan, members

    async def extract_nested_archive_volume_set(
        self,
        volumes: list[tuple[str, str, int]],
        expected_output_size: int,
        selected_paths: tuple[str, ...],
        *,
        passphrase: str | None = None,
    ) -> tuple[str, int, str]:
        """Materialize one nested member under one cumulative native budget."""
        _validate_archive_passphrase(passphrase)
        if (
            not _valid_logical_size(expected_output_size)
            or not isinstance(selected_paths, tuple)
            or not 1 <= len(selected_paths) <= 4
            or any(
                normalize_archive_relative_path(selected_path) != selected_path
                for selected_path in selected_paths
            )
        ):
            raise ValueError("nested archive extraction request is invalid")
        _payload, request, _identities, _expected = _prepare_archive_volume_request(
            volumes
        )
        request["expected_output_size"] = expected_output_size
        request["selected_paths"] = list(selected_paths)
        if passphrase is not None:
            request["passphrase"] = passphrase
        payload = orjson.dumps(
            request,
        )
        if len(payload) > MAX_ENGINE_NZB_METADATA_BYTES:
            raise ValueError(
                "nested archive extraction request exceeds the engine input limit"
            )
        status, _headers, body = await self.request(
            "POST", "/v1/archive-nested/extract", payload
        )
        try:
            response = orjson.loads(body)
        except ValueError as exc:
            raise EngineUnavailable(
                "Usenet engine returned invalid nested archive data"
            ) from exc
        if (
            not isinstance(response, dict)
            or isinstance(response.get("version"), bool)
            or response.get("version") != 1
        ):
            raise EngineUnavailable(
                "Usenet engine returned invalid nested archive data"
            )
        if status != 200:
            code, retryable = _decode_engine_failure(
                response,
                label="nested-archive",
            )
            raise EngineArchiveError(code, retryable=retryable)
        if not _has_fields(
            response,
            {"version", "identity", "byte_size", "asset_revision"},
        ):
            raise EngineUnavailable(
                "Usenet engine returned invalid nested archive data"
            )
        identity = response["identity"]
        byte_size = response["byte_size"]
        asset_revision = response["asset_revision"]
        try:
            _validate_materialization_identity(identity)
            _validate_asset_revision(asset_revision)
        except ValueError as exc:
            raise EngineUnavailable(
                "Usenet engine returned invalid nested archive data"
            ) from exc
        if (
            isinstance(byte_size, bool)
            or not isinstance(byte_size, int)
            or byte_size != expected_output_size
        ):
            raise EngineUnavailable(
                "Usenet engine returned invalid nested archive data"
            )
        return identity, byte_size, asset_revision

    @staticmethod
    def _validate_par2_catalog(catalog: object) -> dict[str, object]:
        if not _has_fields(
            catalog,
            {"set_id", "slice_size", "files", "recovery_exponents"},
        ):
            raise EngineUnavailable("Usenet engine returned invalid PAR2 catalog data")
        set_id = catalog["set_id"]
        slice_size = catalog["slice_size"]
        catalog_files = catalog["files"]
        recovery_exponents = catalog["recovery_exponents"]
        if (
            not isinstance(set_id, str)
            or len(set_id) != 32
            or any(character not in "0123456789abcdef" for character in set_id)
            or isinstance(slice_size, bool)
            or not isinstance(slice_size, int)
            or not 4 <= slice_size <= 16 * 1024 * 1024
            or slice_size % 4 != 0
            or not isinstance(catalog_files, list)
            or not 1 <= len(catalog_files) <= MAX_NZB_FILES
            or not isinstance(recovery_exponents, list)
            or len(recovery_exponents) > 32_768
        ):
            raise EngineUnavailable("Usenet engine returned invalid PAR2 catalog data")
        file_ids = set()
        paths = set()
        total_slices = 0
        for file in catalog_files:
            if not _has_fields(
                file,
                {
                    "file_id",
                    "relative_path",
                    "exact_size",
                    "full_md5",
                    "first_16k_md5",
                    "slice_count",
                },
            ):
                raise EngineUnavailable(
                    "Usenet engine returned invalid PAR2 catalog data"
                )
            file_id = file["file_id"]
            relative_path = file["relative_path"]
            exact_size = file["exact_size"]
            slice_count = file["slice_count"]
            if (
                not isinstance(file_id, str)
                or len(file_id) != 32
                or any(character not in "0123456789abcdef" for character in file_id)
                or file_id in file_ids
                or normalize_archive_relative_path(relative_path) != relative_path
                or relative_path.lower() in paths
                or not _valid_logical_size(exact_size)
                or any(
                    not isinstance(file[field], str)
                    or len(file[field]) != 32
                    or any(
                        character not in "0123456789abcdef" for character in file[field]
                    )
                    for field in ("full_md5", "first_16k_md5")
                )
                or isinstance(slice_count, bool)
                or not isinstance(slice_count, int)
                or not 1 <= slice_count <= 32_768
                or slice_count != (exact_size + slice_size - 1) // slice_size
            ):
                raise EngineUnavailable(
                    "Usenet engine returned invalid PAR2 catalog data"
                )
            file_ids.add(file_id)
            paths.add(relative_path.lower())
            total_slices += slice_count
            if total_slices > 32_768:
                raise EngineUnavailable(
                    "Usenet engine returned invalid PAR2 catalog data"
                )
        if any(
            isinstance(exponent, bool)
            or not isinstance(exponent, int)
            or not 0 <= exponent <= 65_535
            or index > 0
            and recovery_exponents[index - 1] >= exponent
            for index, exponent in enumerate(recovery_exponents)
        ):
            raise EngineUnavailable("Usenet engine returned invalid PAR2 catalog data")
        return catalog

    async def discover_par2_sets(
        self,
        files: list[tuple[str, str, int]],
    ) -> list[dict[str, object]]:
        """Discover complete recovery sets across bounded immutable PAR2 sidecars."""
        _payload, par2_request, identities, _expected = _prepare_par2_volume_request(
            files,
            error="PAR2 discovery request is invalid",
        )
        payload = orjson.dumps(
            {"files": par2_request["volumes"]},
        )
        status, _headers, body = await self.request(
            "POST", "/v1/par2/discover", payload
        )
        try:
            response = orjson.loads(body)
        except ValueError as exc:
            raise EngineUnavailable(
                "Usenet engine returned invalid PAR2 discovery data"
            ) from exc
        if status != 200:
            code, retryable = _decode_engine_failure(
                response,
                label="PAR2-discovery",
            )
            raise EngineArchiveError(code, retryable=retryable)
        if (
            not _has_fields(response, {"version", "sets"})
            or isinstance(response["version"], bool)
            or response["version"] != 1
            or not isinstance(response["sets"], list)
            or not 1 <= len(response["sets"]) <= MAX_NZB_FILES
        ):
            raise EngineUnavailable(
                "Usenet engine returned invalid PAR2 discovery data"
            )
        discovered_sets = []
        set_ids = set()
        total_files = 0
        total_slices = 0
        for discovered in response["sets"]:
            if not _has_fields(
                discovered,
                {
                    "set_id",
                    "slice_size",
                    "files",
                    "recovery_exponents",
                    "volume_content_identities",
                },
            ):
                raise EngineUnavailable(
                    "Usenet engine returned invalid PAR2 discovery data"
                )
            volume_identities = discovered["volume_content_identities"]
            catalog = {
                key: value
                for key, value in discovered.items()
                if key != "volume_content_identities"
            }
            self._validate_par2_catalog(catalog)
            set_id = catalog["set_id"]
            if (
                set_id in set_ids
                or not isinstance(volume_identities, list)
                or not 1 <= len(volume_identities) <= MAX_PAR2_VOLUMES
                or any(
                    not isinstance(identity, str) or identity not in identities
                    for identity in volume_identities
                )
                or len(set(volume_identities)) != len(volume_identities)
            ):
                raise EngineUnavailable(
                    "Usenet engine returned invalid PAR2 discovery data"
                )
            set_ids.add(set_id)
            total_files += len(catalog["files"])
            total_slices += sum(file["slice_count"] for file in catalog["files"])
            if total_files > MAX_NZB_FILES or total_slices > 32_768:
                raise EngineUnavailable(
                    "Usenet engine returned invalid PAR2 discovery data"
                )
            discovered_sets.append(discovered)
        return discovered_sets

    async def map_par2_sources(
        self,
        files: list[tuple[str, str, int]],
        sources: list[tuple[str, str, int]],
        *,
        recovery_set_id: str | None = None,
    ) -> dict[str, object]:
        """Map complete immutable sources to unique PAR2 File Description IDs."""
        _files_payload, files_request, _file_identities, _file_expected = (
            _prepare_par2_volume_request(
                files,
                error="PAR2 source map request is invalid",
            )
        )
        _sources_payload, sources_request, source_identities, source_expected = (
            _prepare_archive_volume_request(sources)
        )
        if recovery_set_id is not None and (
            not isinstance(recovery_set_id, str)
            or len(recovery_set_id) != 32
            or any(character not in "0123456789abcdef" for character in recovery_set_id)
        ):
            raise ValueError("PAR2 source map request is invalid")
        request = {
            "files": files_request["volumes"],
            "sources": sources_request["volumes"],
        }
        if recovery_set_id is not None:
            request["set_id"] = recovery_set_id
        payload = orjson.dumps(
            request,
        )
        status, _headers, body = await self.request(
            "POST", "/v1/par2/map-sources", payload
        )
        try:
            response = orjson.loads(body)
        except ValueError as exc:
            raise EngineUnavailable(
                "Usenet engine returned invalid PAR2 source mapping"
            ) from exc
        if status != 200:
            code, retryable = _decode_engine_failure(
                response,
                label="PAR2-source-mapping",
            )
            raise EngineArchiveError(code, retryable=retryable)
        if not _has_fields(
            response,
            {"version", "set_id", "slice_size", "mappings"},
        ):
            raise EngineUnavailable(
                "Usenet engine returned invalid PAR2 source mapping"
            )
        set_id = response["set_id"]
        slice_size = response["slice_size"]
        mappings = response["mappings"]
        if (
            isinstance(response["version"], bool)
            or response["version"] != 1
            or not isinstance(set_id, str)
            or len(set_id) != 32
            or any(character not in "0123456789abcdef" for character in set_id)
            or recovery_set_id is not None
            and set_id != recovery_set_id
            or isinstance(slice_size, bool)
            or not isinstance(slice_size, int)
            or not 4 <= slice_size <= 16 * 1024 * 1024
            or slice_size % 4 != 0
            or not isinstance(mappings, list)
            or len(mappings) != len(source_identities)
        ):
            raise EngineUnavailable(
                "Usenet engine returned invalid PAR2 source mapping"
            )
        mapped_identities = set()
        file_ids = set()
        paths = set()
        for mapping in mappings:
            if not _has_fields(
                mapping,
                {
                    "content_identity",
                    "file_id",
                    "relative_path",
                    "exact_size",
                    "slice_count",
                },
            ):
                raise EngineUnavailable(
                    "Usenet engine returned invalid PAR2 source mapping"
                )
            content_identity = mapping["content_identity"]
            file_id = mapping["file_id"]
            relative_path = mapping["relative_path"]
            exact_size = mapping["exact_size"]
            slice_count = mapping["slice_count"]
            expected = source_expected.get(content_identity)
            if (
                not isinstance(content_identity, str)
                or content_identity not in source_identities
                or content_identity in mapped_identities
                or not isinstance(file_id, str)
                or len(file_id) != 32
                or any(character not in "0123456789abcdef" for character in file_id)
                or file_id in file_ids
                or normalize_archive_relative_path(relative_path) != relative_path
                or relative_path.lower() in paths
                or isinstance(exact_size, bool)
                or not isinstance(exact_size, int)
                or expected is None
                or exact_size != expected[1]
                or isinstance(slice_count, bool)
                or not isinstance(slice_count, int)
                or slice_count != (exact_size + slice_size - 1) // slice_size
                or not 1 <= slice_count <= 32_768
            ):
                raise EngineUnavailable(
                    "Usenet engine returned invalid PAR2 source mapping"
                )
            mapped_identities.add(content_identity)
            file_ids.add(file_id)
            paths.add(relative_path.lower())
        if mapped_identities != source_identities:
            raise EngineUnavailable(
                "Usenet engine returned invalid PAR2 source mapping"
            )
        return response

    async def repair_par2(
        self,
        files: list[tuple[str, str, int]],
        sources: list[tuple[str, str, int]],
        selected_file_id: str,
        *,
        partial_sources: list[tuple[list[tuple[int, int, str]], str | None]]
        | None = None,
        account_partition: bytes | None = None,
        provider_set_generation: str | None = None,
        recovery_set_id: str | None = None,
    ) -> dict[str, object]:
        """Repair and publish one catalog-selected PAR2 source file."""
        _files_payload, files_request, _file_identities, _file_expected = (
            _prepare_par2_volume_request(
                files,
                error="PAR2 repair request is invalid",
            )
        )
        if not isinstance(sources, list):
            raise ValueError("PAR2 repair request is invalid")
        if sources:
            _sources_payload, sources_request, _source_identities, _source_expected = (
                _prepare_archive_volume_request(sources)
            )
            payload_sources = sources_request["volumes"]
        else:
            payload_sources = []
        if partial_sources is None:
            partial_sources = []
        if (
            not isinstance(partial_sources, list)
            or len(partial_sources) > MAX_NZB_FILES
            or bool(partial_sources)
            != (
                isinstance(account_partition, bytes)
                and len(account_partition) == 32
                and isinstance(provider_set_generation, str)
            )
            or not partial_sources
            and (account_partition is not None or provider_set_generation is not None)
        ):
            raise ValueError("PAR2 partial source request is invalid")
        if partial_sources:
            provider_set_id = self._provider_set_ids[provider_set_generation]
        payload_partial_sources = []
        total_postings = 0
        for partial_source in partial_sources:
            if (
                not isinstance(partial_source, tuple)
                or len(partial_source) != 2
                or not isinstance(partial_source[0], list)
                or not 1 <= len(partial_source[0]) <= MAX_NZB_SEGMENTS
            ):
                raise ValueError("PAR2 partial source request is invalid")
            postings, group = partial_source
            total_postings += len(postings)
            if total_postings > MAX_NZB_SEGMENTS:
                raise ValueError("PAR2 partial source request is invalid")
            payload_partial_sources.append(
                {
                    "postings": _normalize_nntp_postings(
                        postings,
                        error="PAR2 partial source request is invalid",
                    ),
                    "group": group,
                    "account_partition": account_partition.hex(),
                    "provider_set_id": provider_set_id,
                }
            )
        if (
            not isinstance(selected_file_id, str)
            or len(selected_file_id) != 32
            or any(
                character not in "0123456789abcdef" for character in selected_file_id
            )
            or recovery_set_id is not None
            and (
                not isinstance(recovery_set_id, str)
                or len(recovery_set_id) != 32
                or any(
                    character not in "0123456789abcdef" for character in recovery_set_id
                )
            )
        ):
            raise ValueError("PAR2 repair request is invalid")
        request = {
            "files": files_request["volumes"],
            "sources": payload_sources,
            "partial_sources": payload_partial_sources,
            "selected_file_id": selected_file_id,
        }
        if recovery_set_id is not None:
            request["set_id"] = recovery_set_id
        payload = orjson.dumps(
            request,
        )
        if len(payload) > MAX_ENGINE_NZB_METADATA_BYTES:
            raise ValueError("PAR2 repair request exceeds the engine input limit")
        status, _headers, body = await self.request("POST", "/v1/par2/repair", payload)
        try:
            response = orjson.loads(body)
        except ValueError as exc:
            raise EngineUnavailable(
                "Usenet engine returned invalid PAR2 repair data"
            ) from exc
        if (
            not isinstance(response, dict)
            or isinstance(response.get("version"), bool)
            or response.get("version") != 1
        ):
            raise EngineUnavailable("Usenet engine returned invalid PAR2 repair data")
        if status != 200:
            code, retryable = _decode_engine_failure(
                response,
                label="PAR2-repair",
            )
            required_recovery_blocks = response.get("required_recovery_blocks")
            if required_recovery_blocks is not None and (
                code != "repair_insufficient"
                or isinstance(required_recovery_blocks, bool)
                or not isinstance(required_recovery_blocks, int)
                or not 1 <= required_recovery_blocks <= 32_768
            ):
                raise EngineUnavailable(
                    "Usenet engine returned invalid PAR2 repair failure data"
                )
            raise EngineArchiveError(
                code,
                retryable=retryable,
                required_recovery_blocks=required_recovery_blocks,
            )
        if not _has_fields(
            response,
            {
                "version",
                "set_id",
                "file_id",
                "relative_path",
                "identity",
                "byte_size",
                "asset_revision",
                "partial_source_mapped",
            },
        ):
            raise EngineUnavailable("Usenet engine returned invalid PAR2 repair data")
        set_id = response["set_id"]
        file_id = response["file_id"]
        relative_path = response["relative_path"]
        identity = response["identity"]
        byte_size = response["byte_size"]
        asset_revision = response["asset_revision"]
        partial_source_mapped = response["partial_source_mapped"]
        if (
            not isinstance(set_id, str)
            or len(set_id) != 32
            or any(character not in "0123456789abcdef" for character in set_id)
            or recovery_set_id is not None
            and set_id != recovery_set_id
            or file_id != selected_file_id
            or normalize_archive_relative_path(relative_path) != relative_path
            or not is_sha256_hex(identity)
            or not is_sha256_hex(asset_revision)
            or not _valid_logical_size(byte_size)
            or not isinstance(partial_source_mapped, bool)
        ):
            raise EngineUnavailable("Usenet engine returned invalid PAR2 repair data")
        return response

    async def open_raw_composite(
        self,
        volumes: list[tuple[str, str, int]],
    ) -> tuple[str, int, str]:
        """Prove and open one replica-local direct raw-split byte source."""
        payload, _request, identities, expected = _prepare_archive_volume_request(
            volumes
        )
        status, _headers, body = await self.request(
            "POST", "/v1/raw-composites", payload
        )
        try:
            response = orjson.loads(body)
        except ValueError as exc:
            raise EngineUnavailable(
                "Usenet engine returned invalid raw composite data"
            ) from exc
        if (
            not isinstance(response, dict)
            or isinstance(response.get("version"), bool)
            or response.get("version") != 1
        ):
            raise EngineUnavailable("Usenet engine returned invalid raw composite data")
        if status != 200:
            code, retryable = _decode_engine_failure(
                response,
                label="raw-composite",
            )
            raise EngineArchiveError(code, retryable=retryable)
        if not _has_fields(
            response,
            {"version", "identity", "exact_size", "etag", "plan"},
        ):
            raise EngineUnavailable("Usenet engine returned invalid raw composite data")
        plan = _validate_archive_volume_plan(response["plan"], identities, expected)
        identity = response["identity"]
        exact_size = response["exact_size"]
        etag = response["etag"]
        if (
            plan["kind"].get("layout") != "raw_split"
            or not isinstance(identity, str)
            or identity != plan["set_identity"]
            or not isinstance(etag, str)
            or etag != identity
            or isinstance(exact_size, bool)
            or not isinstance(exact_size, int)
            or exact_size != plan["exact_size"]
        ):
            raise EngineUnavailable("Usenet engine returned invalid raw composite data")
        return identity, exact_size, etag

    async def open_stored_archive_member(
        self,
        volumes: list[tuple[str, str, int]],
        expected_output_size: int,
        selected_path: str,
    ) -> tuple[dict[str, object], str, int, str]:
        """Open one stored RAR member as immutable source ranges."""
        _payload, request, identities, expected = _prepare_archive_volume_request(
            volumes
        )
        return await self._open_stored_archive_request(
            request,
            identities,
            expected,
            expected_output_size,
            selected_path,
            "/v1/archive-direct/open",
        )

    async def open_session_archive_member(
        self,
        volumes: list[tuple[str, str, str, int]],
        expected_output_size: int,
        selected_path: str,
        *,
        passphrase: str | None = None,
    ) -> tuple[dict[str, object], str, int, str]:
        """Open one seekable archive member over sparse NNTP sessions."""
        _validate_archive_passphrase(passphrase)
        _payload, request, identities, expected = _prepare_session_archive_request(
            volumes, passphrase=passphrase
        )
        return await self._open_stored_archive_request(
            request,
            identities,
            expected,
            expected_output_size,
            selected_path,
            "/v1/session-archives/open",
        )

    async def _open_stored_archive_request(
        self,
        request: dict[str, object],
        identities: set[str],
        expected: dict[str, tuple[str, int]],
        expected_output_size: int,
        selected_path: str,
        path: str,
    ) -> tuple[dict[str, object], str, int, str]:
        if (
            not _valid_logical_size(expected_output_size)
            or normalize_archive_relative_path(selected_path) != selected_path
        ):
            raise ValueError("stored archive member request is invalid")
        request["expected_output_size"] = expected_output_size
        request["selected_path"] = selected_path
        payload = orjson.dumps(request)
        if len(payload) > MAX_ENGINE_NZB_METADATA_BYTES:
            raise ValueError(
                "stored archive member request exceeds the engine input limit"
            )
        status, _headers, body = await self.request("POST", path, payload)
        try:
            response = orjson.loads(body)
        except ValueError as exc:
            raise EngineUnavailable(
                "Usenet engine returned invalid stored archive data"
            ) from exc
        if (
            not isinstance(response, dict)
            or isinstance(response.get("version"), bool)
            or response.get("version") != 1
        ):
            raise EngineUnavailable(
                "Usenet engine returned invalid stored archive data"
            )
        if status != 200:
            code, retryable = _decode_engine_failure(
                response,
                label="stored-archive",
            )
            raise EngineArchiveError(code, retryable=retryable)
        if not _has_fields(
            response,
            {
                "version",
                "identity",
                "exact_size",
                "etag",
                "relative_path",
                "plan",
            },
        ):
            raise EngineUnavailable(
                "Usenet engine returned invalid stored archive data"
            )
        plan = _validate_archive_volume_plan(response["plan"], identities, expected)
        identity = response["identity"]
        exact_size = response["exact_size"]
        etag = response["etag"]
        if (
            plan["kind"].get("layout") not in {"single_archive", "multi_volume_archive"}
            or not is_sha256_hex(identity)
            or identity
            != _archive_member_identity(
                plan["set_identity"], selected_path, expected_output_size
            )
            or etag != identity
            or response["relative_path"] != selected_path
            or isinstance(exact_size, bool)
            or exact_size != expected_output_size
        ):
            raise EngineUnavailable(
                "Usenet engine returned invalid stored archive data"
            )
        return plan, identity, exact_size, etag

    async def read_raw_composite_range(
        self,
        identity: str,
        reader_lease_id: str,
        expected_size: int,
        start: int,
        end: int,
    ) -> bytes:
        """Read one bounded exact range from an opened raw-split composite."""
        _validate_materialization_identity(identity)
        _validate_session_identity(reader_lease_id)
        if (
            not _valid_logical_size(expected_size)
            or isinstance(start, bool)
            or not isinstance(start, int)
            or isinstance(end, bool)
            or not isinstance(end, int)
            or start < 0
            or end < start
            or end >= expected_size
            or end - start + 1 > MAX_ENGINE_RANGE_BYTES
        ):
            raise ValueError("raw composite range is invalid")
        payload = orjson.dumps(
            {
                "expected_size": expected_size,
                "start": start,
                "end": end,
                "reader_lease_id": reader_lease_id,
            },
        )
        status, headers, body = await self.request(
            "POST", f"/v1/raw-composites/{identity}/read", payload
        )
        if status != 206:
            try:
                response = orjson.loads(body)
            except ValueError as exc:
                raise EngineUnavailable(
                    "Usenet engine could not read the raw composite"
                ) from exc
            code, retryable = _decode_engine_failure(
                response,
                label="raw-composite-range",
            )
            raise EngineArchiveError(code, retryable=retryable)
        if headers.get("content-range") != f"bytes {start}-{end}/{expected_size}":
            raise EngineUnavailable(
                "Usenet engine returned an invalid raw composite range"
            )
        if len(body) != end - start + 1:
            raise EngineUnavailable(
                "Usenet engine returned a truncated raw composite range"
            )
        return body

    async def open_raw_composite_reader(self, identity: str) -> str:
        """Hold one reader slot across a raw-composite response stream."""
        _validate_materialization_identity(identity)
        return await self._open_native_reader(
            f"/v1/raw-composites/{identity}/readers",
            identity,
            "source_identity",
            "raw composite",
            EngineArchiveError,
        )

    async def close_raw_composite_reader(
        self,
        identity: str,
        reader_lease_id: str,
    ) -> None:
        """Release one raw-composite stream reader."""
        _validate_materialization_identity(identity)
        _validate_session_identity(reader_lease_id)
        await self._close_native_reader(
            f"/v1/raw-composites/{identity}/readers/{reader_lease_id}",
            "raw composite",
        )

    async def read_session_range(
        self,
        identity: str,
        reader_lease_id: str,
        expected_size: int,
        start: int,
        end: int,
    ) -> bytes:
        """Read one bounded range from a replica-local sparse native session."""
        _validate_session_identity(identity)
        _validate_session_identity(reader_lease_id)
        if (
            not _valid_logical_size(expected_size)
            or isinstance(start, bool)
            or not isinstance(start, int)
            or isinstance(end, bool)
            or not isinstance(end, int)
            or start < 0
            or end < start
            or end >= expected_size
            or end - start + 1 > MAX_ENGINE_RANGE_BYTES
        ):
            raise ValueError("session range is invalid")
        payload = orjson.dumps(
            {
                "expected_size": expected_size,
                "start": start,
                "end": end,
                "reader_lease_id": reader_lease_id,
            },
        )
        status, headers, body = await self.request(
            "POST", f"/v1/sessions/{identity}/read", payload
        )
        if status != 206:
            try:
                response = orjson.loads(body)
                code, retryable = _decode_engine_failure(
                    response,
                    label="session-range",
                )
            except ValueError as exc:
                raise EngineUnavailable(
                    "Usenet engine could not read the session"
                ) from exc
            raise EngineNntpError(code, retryable=retryable)
        if headers.get("content-range") != f"bytes {start}-{end}/{expected_size}":
            raise EngineUnavailable("Usenet engine returned an invalid session range")
        if len(body) != end - start + 1:
            raise EngineUnavailable("Usenet engine returned a truncated session range")
        salvage = headers.get("x-comet-usenet-salvage")
        salvaged_bytes_text = headers.get("x-comet-usenet-salvaged-bytes")
        salvaged_holes_text = headers.get("x-comet-usenet-salvaged-holes")
        if (
            salvage not in {"none", "zero-fill"}
            or salvaged_bytes_text is None
            or not salvaged_bytes_text.isascii()
            or not salvaged_bytes_text.isdigit()
            or salvaged_holes_text is None
            or not salvaged_holes_text.isascii()
            or not salvaged_holes_text.isdigit()
        ):
            raise EngineUnavailable("Usenet engine returned invalid salvage state")
        salvaged_bytes = int(salvaged_bytes_text)
        salvaged_holes = int(salvaged_holes_text)
        degraded = salvage == "zero-fill"
        if (
            salvaged_bytes > 2**64 - 1
            or salvaged_bytes > len(body)
            or salvaged_holes > 2
            or degraded != (salvaged_bytes > 0 and salvaged_holes > 0)
        ):
            raise EngineUnavailable("Usenet engine returned invalid salvage state")
        if degraded and identity not in self._reported_degraded_sessions:
            self._reported_degraded_sessions.add(identity)
            log.warning(
                "usenet.engine.salvage",
                "Usenet engine used bounded zero-fill salvage",
                salvaged_bytes=salvaged_bytes,
                salvaged_holes=salvaged_holes,
            )
        return body

    async def open_session_reader(self, identity: str) -> str:
        """Hold one bounded reader slot for an entire public response stream."""
        _validate_session_identity(identity)
        return await self._open_native_reader(
            f"/v1/sessions/{identity}/readers",
            identity,
            "session_id",
            "session",
            EngineNntpError,
        )

    async def _open_native_reader(
        self,
        path: str,
        identity: str,
        identity_field: str,
        label: str,
        error_class,
    ) -> str:
        status, _headers, body = await self.request("POST", path, b"")
        try:
            response = orjson.loads(body)
        except ValueError as exc:
            raise EngineUnavailable(
                f"Usenet engine could not open a {label} reader"
            ) from exc
        if status != 201:
            code, retryable = _decode_engine_failure(
                response,
                label=f"{label}-reader",
            )
            raise error_class(code, retryable=retryable)
        lease_id = (
            response.get("reader_lease_id") if isinstance(response, dict) else None
        )
        try:
            _validate_session_identity(lease_id)
        except ValueError as exc:
            raise EngineUnavailable(
                f"Usenet engine returned an invalid {label} reader"
            ) from exc
        required = {"version", identity_field, "reader_lease_id"}
        if (
            not _has_fields(response, required)
            or type(response["version"]) is not int
            or response["version"] != 1
            or response[identity_field] != identity
        ):
            raise EngineUnavailable(f"Usenet engine returned an invalid {label} reader")
        return lease_id

    async def close_session_reader(self, identity: str, reader_lease_id: str) -> None:
        """Release one stream-lifetime reader slot."""
        _validate_session_identity(identity)
        _validate_session_identity(reader_lease_id)
        await self._close_native_reader(
            f"/v1/sessions/{identity}/readers/{reader_lease_id}",
            "session",
        )

    async def _close_native_reader(self, path: str, label: str) -> None:
        status, _headers, body = await self.request("DELETE", path, b"")
        if status != 204 or body:
            raise EngineUnavailable(f"Usenet engine could not close the {label} reader")

    async def open_nntp_session(
        self,
        postings: list[tuple[int, int, str]],
        *,
        group: str | None = None,
        servers: list[dict[str, object]],
        account_partition: bytes,
        provider_set_generation: str,
        allow_degraded_playback: bool = False,
        preparation: bool = False,
    ) -> tuple[str, int, str, str | None]:
        """Open or reuse one sparse, replica-local random-access session."""
        return await self._submit_nntp_postings(
            "/v1/sessions",
            postings,
            group=group,
            servers=servers,
            account_partition=account_partition,
            provider_set_generation=provider_set_generation,
            allow_degraded_playback=allow_degraded_playback,
            preparation=preparation,
        )

    async def inspect_nntp_postings(
        self,
        artifact_sha256: str,
        postings: list[tuple[int, int, str]],
        *,
        group: str | None = None,
        servers: list[dict[str, object]],
        account_partition: bytes,
        provider_set_generation: str,
    ) -> dict[str, object]:
        """Probe verified head/tail bytes for one selected native media file."""
        _validate_materialization_identity(artifact_sha256)
        result = await self._submit_nntp_postings(
            f"/v1/artifacts/{artifact_sha256}/native-inspect",
            postings,
            group=group,
            servers=servers,
            account_partition=account_partition,
            provider_set_generation=provider_set_generation,
            inspection_artifact_sha256=artifact_sha256,
        )
        if not isinstance(result, dict):
            raise EngineUnavailable("Usenet engine returned invalid inspection data")
        return result

    async def inspect_materialization(
        self,
        identity: str,
        expected_size: int,
    ) -> dict[str, object]:
        """Probe one fully materialized file after independently reopening it."""
        _validate_materialization_identity(identity)
        return await self._inspect_immutable_source(
            f"/v1/materializations/{identity}/native-inspect",
            identity,
            expected_size,
            identity_field="materialization_identity",
            label="materialization",
        )

    async def inspect_raw_composite(
        self,
        identity: str,
        expected_size: int,
    ) -> dict[str, object]:
        """Probe one fingerprint-pinned raw-split source without concatenating it."""
        _validate_materialization_identity(identity)
        return await self._inspect_immutable_source(
            f"/v1/raw-composites/{identity}/native-inspect",
            identity,
            expected_size,
            identity_field="source_identity",
            label="raw composite",
        )

    async def _inspect_immutable_source(
        self,
        path: str,
        identity: str,
        expected_size: int,
        *,
        identity_field: str,
        label: str,
    ) -> dict[str, object]:
        if not _valid_logical_size(expected_size):
            raise ValueError(f"native {label} inspection is invalid")
        payload = orjson.dumps({"expected_size": expected_size})
        status, _headers, body = await self.request("POST", path, payload)
        try:
            response = orjson.loads(body)
        except ValueError as exc:
            raise EngineUnavailable(
                f"Usenet engine returned invalid {label} inspection data"
            ) from exc
        if status != 200:
            code, retryable = _decode_engine_failure(
                response,
                label=f"{label}-inspection",
            )
            raise EngineArchiveError(code, retryable=retryable)
        expected_keys = {
            "version",
            "source_identity",
            "inspection_state",
            "duration_millis",
            "inspected_head_bytes",
            "inspected_tail_bytes",
        }
        expected_keys.add(identity_field)
        duration = (
            response.get("duration_millis") if isinstance(response, dict) else None
        )
        head_bytes = (
            response.get("inspected_head_bytes") if isinstance(response, dict) else None
        )
        tail_bytes = (
            response.get("inspected_tail_bytes") if isinstance(response, dict) else None
        )
        if (
            not _has_fields(response, expected_keys)
            or isinstance(response.get("version"), bool)
            or response["version"] != 1
            or response[identity_field] != identity
            or response["source_identity"] != identity
            or response["inspection_state"] != "provisionally_streamable"
            or (
                duration is not None
                and (
                    isinstance(duration, bool)
                    or not isinstance(duration, int)
                    or not 0 <= duration <= 2**64 - 1
                )
            )
            or isinstance(head_bytes, bool)
            or not isinstance(head_bytes, int)
            or not 1 <= head_bytes <= MAX_ENGINE_STRUCTURAL_END_BYTES
            or isinstance(tail_bytes, bool)
            or not isinstance(tail_bytes, int)
            or not 0 <= tail_bytes <= MAX_ENGINE_STRUCTURAL_END_BYTES
        ):
            raise EngineUnavailable(
                f"Usenet engine returned invalid {label} inspection data"
            )
        return response

    async def catalog_nntp_artifact(
        self,
        artifact_sha256: str,
        manifest_identity: str,
        metadata: dict[str, str],
        manifest: list,
        *,
        selection_hint: tuple[str, int] | None = None,
    ) -> list[dict[str, object]]:
        """Rehydrate Rust's canonical manifest into a typed native asset catalog."""
        _validate_materialization_identity(artifact_sha256)
        if (
            not isinstance(manifest_identity, str)
            or len(manifest_identity) != 68
            or not manifest_identity.startswith("nm1:")
            or any(
                character not in "0123456789abcdef"
                for character in manifest_identity[4:]
            )
            or not isinstance(manifest, list)
            or not 1 <= len(manifest) <= MAX_NZB_FILES
            or not isinstance(metadata, dict)
            or any(
                not isinstance(key, str)
                or not isinstance(value, str)
                or not key
                or not value
                for key, value in metadata.items()
            )
        ):
            raise ValueError("native asset catalog request is invalid")
        request = {
            "manifest_identity": manifest_identity,
            "metadata": metadata,
            "manifest": manifest,
        }
        if selection_hint is not None:
            if (
                not isinstance(selection_hint, tuple)
                or len(selection_hint) != 2
                or not isinstance(selection_hint[0], str)
                or normalize_archive_relative_path(selection_hint[0])
                != selection_hint[0]
                or len(selection_hint[0].encode()) > 512
                or not _valid_logical_size(selection_hint[1])
            ):
                raise ValueError("native asset selection hint is invalid")
            request["selection_hint"] = {
                "relative_path": selection_hint[0],
                "exact_size": selection_hint[1],
            }
        payload = orjson.dumps(request)
        if len(payload) > MAX_ENGINE_NZB_METADATA_BYTES:
            raise ValueError(
                "native asset catalog request exceeds the engine input limit"
            )
        status, _headers, body = await self.request(
            "POST", f"/v1/artifacts/{artifact_sha256}/native-catalog", payload
        )
        try:
            response = orjson.loads(body)
        except ValueError as exc:
            raise EngineUnavailable(
                "Usenet engine returned invalid asset catalog data"
            ) from exc
        if status != 200:
            code, retryable = _decode_engine_failure(
                response,
                label="asset-catalog",
            )
            raise EngineNntpError(code, retryable=retryable)
        if (
            not isinstance(response, dict)
            or response.get("version") != 1
            or response.get("artifact_sha256") != artifact_sha256
            or not isinstance(response.get("assets"), list)
            or len(response["assets"]) > len(manifest)
        ):
            raise EngineUnavailable("Usenet engine returned invalid asset catalog data")
        file_indices = set()
        for asset in response["assets"]:
            if not _has_fields(
                asset,
                {
                    "asset_id",
                    "file_index",
                    "relative_path",
                    "declared_bytes",
                    "kind",
                },
            ):
                raise EngineUnavailable(
                    "Usenet engine returned invalid asset catalog data"
                )
            file_index = asset["file_index"]
            asset_id = asset["asset_id"]
            relative_path = asset["relative_path"]
            declared_bytes = asset["declared_bytes"]
            if (
                isinstance(file_index, bool)
                or not isinstance(file_index, int)
                or not 0 <= file_index < len(manifest)
                or file_index in file_indices
                or not is_sha256_hex(asset_id)
                or not isinstance(relative_path, str)
                or not relative_path
                or len(relative_path.encode()) > 2_048
                or not _valid_logical_size(declared_bytes)
                or asset["kind"] not in _ASSET_KINDS
            ):
                raise EngineUnavailable(
                    "Usenet engine returned invalid asset catalog data"
                )
            file_indices.add(file_index)
        return response["assets"]

    async def materialize_nntp_postings(
        self,
        postings: list[tuple[int, int, str]],
        *,
        group: str | None = None,
        servers: list[dict[str, object]],
        account_partition: bytes,
        provider_set_generation: str,
    ) -> tuple[str, int, str]:
        """Materialize through one ordered, provider-scoped NNTP server set."""
        return await self._submit_nntp_postings(
            "/v1/materializations",
            postings,
            group=group,
            servers=servers,
            account_partition=account_partition,
            provider_set_generation=provider_set_generation,
        )

    async def _submit_nntp_postings(
        self,
        path: str,
        postings: list[tuple[int, int, str]],
        *,
        group: str | None,
        servers: list[dict[str, object]],
        account_partition: bytes,
        provider_set_generation: str,
        allow_degraded_playback: bool = False,
        preparation: bool = False,
        inspection_artifact_sha256: str | None = None,
    ) -> tuple[str, int, str] | tuple[str, int, str, str | None] | dict[str, object]:
        normalized_postings = _normalize_nntp_postings(
            postings,
            error="native materialization postings are invalid",
        )
        request_payload = {
            "postings": normalized_postings,
            "group": group,
            "account_partition": account_partition.hex(),
            "provider_set_id": "A" * 22,
        }
        if path == "/v1/sessions":
            request_payload["allow_degraded_playback"] = allow_degraded_playback
            request_payload["preparation"] = preparation
        provider_set_id_marker = b'"provider_set_id":"AAAAAAAAAAAAAAAAAAAAAA"'
        payload = orjson.dumps(request_payload)
        if len(payload) > MAX_ENGINE_NZB_METADATA_BYTES:
            raise ValueError(
                "native materialization request exceeds the engine input limit"
            )
        registration = orjson.dumps(
            {
                "servers": servers,
                "account_partition": account_partition.hex(),
            },
        )
        if len(registration) > MAX_ENGINE_PROVIDER_SET_BYTES:
            raise ValueError(
                "native provider-set request exceeds the engine input limit"
            )
        provider_set_id = await self._provider_set_id(
            provider_set_generation,
            registration,
        )
        payload = payload.replace(
            provider_set_id_marker,
            f'"provider_set_id":"{provider_set_id}"'.encode(),
            1,
        )

        status, _headers, body = await self.request("POST", path, payload)
        try:
            response = orjson.loads(body)
        except ValueError as exc:
            raise EngineUnavailable(
                "Usenet engine returned invalid materialization data"
            ) from exc
        if not isinstance(response, dict):
            raise EngineUnavailable(
                "Usenet engine returned invalid materialization data"
            )
        if inspection_artifact_sha256 is not None:
            duration = response.get("duration_millis")
            head_bytes = response.get("inspected_head_bytes")
            tail_bytes = response.get("inspected_tail_bytes")
            if status != 200:
                code, retryable = _decode_engine_failure(
                    response,
                    label="native-inspection",
                )
                raise EngineNntpError(code, retryable=retryable)
            if (
                response.get("version") != 1
                or response.get("artifact_sha256") != inspection_artifact_sha256
                or response.get("inspection_state") != "provisionally_streamable"
                or (
                    duration is not None
                    and (
                        isinstance(duration, bool)
                        or not isinstance(duration, int)
                        or not 0 <= duration <= 2**64 - 1
                    )
                )
                or isinstance(head_bytes, bool)
                or not isinstance(head_bytes, int)
                or not 1 <= head_bytes <= MAX_ENGINE_STRUCTURAL_END_BYTES
                or isinstance(tail_bytes, bool)
                or not isinstance(tail_bytes, int)
                or not 0 <= tail_bytes <= MAX_ENGINE_STRUCTURAL_END_BYTES
            ):
                raise EngineUnavailable(
                    "Usenet engine returned invalid inspection data"
                )
            return response
        if status != 200:
            code, retryable = _decode_engine_failure(
                response,
                label="materialization",
            )
            raise EngineNntpError(code, retryable=retryable)
        if (
            response.get("version") != 1
            or not isinstance(response.get("identity"), str)
            or not _valid_logical_size(response.get("byte_size"))
        ):
            raise EngineUnavailable(
                "Usenet engine returned invalid materialization data"
            )
        if path == "/v1/sessions":
            if not _has_fields(
                response,
                {
                    "version",
                    "identity",
                    "byte_size",
                    "revision",
                    "asset_revision",
                },
            ):
                raise EngineUnavailable(
                    "Usenet engine returned invalid random-access session data"
                )
            try:
                _validate_session_identity(response["identity"])
                _validate_materialization_identity(response.get("revision"))
                if response["asset_revision"] is not None:
                    _validate_asset_revision(response["asset_revision"])
            except ValueError as exc:
                raise EngineUnavailable(
                    "Usenet engine returned invalid random-access session data"
                ) from exc
            return (
                response["identity"],
                response["byte_size"],
                response["revision"],
                response["asset_revision"],
            )
        if not _has_fields(
            response,
            {"version", "identity", "byte_size", "asset_revision"},
        ):
            raise EngineUnavailable(
                "Usenet engine returned invalid materialization data"
            )
        try:
            _validate_materialization_identity(response["identity"])
            _validate_asset_revision(response["asset_revision"])
        except ValueError as exc:
            raise EngineUnavailable(
                "Usenet engine returned invalid materialization data"
            ) from exc
        return (
            response["identity"],
            response["byte_size"],
            response["asset_revision"],
        )

    async def _provider_set_id(
        self,
        generation: str,
        registration: bytes,
    ) -> str:
        if provider_set_id := self._provider_set_ids.get(generation):
            return provider_set_id
        lock = self._provider_set_locks.setdefault(generation, asyncio.Lock())
        async with lock:
            if provider_set_id := self._provider_set_ids.get(generation):
                return provider_set_id
            status, _headers, body = await self.request(
                "PUT",
                f"/v1/provider-sets/{generation}",
                registration,
            )
            try:
                registered = orjson.loads(body)
            except ValueError as exc:
                raise EngineUnavailable(
                    "Usenet engine returned invalid provider-set data"
                ) from exc
            if status != 200:
                code, retryable = _decode_engine_failure(
                    registered,
                    label="provider-set",
                )
                raise EngineNntpError(code, retryable=retryable)
            provider_set_id = (
                registered.get("provider_set_id")
                if isinstance(registered, dict)
                else None
            )
            if (
                not isinstance(registered, dict)
                or set(registered)
                != {
                    "version",
                    "provider_set_id",
                    "generation",
                }
                or registered["version"] != 1
                or registered["generation"] != generation
            ):
                raise EngineUnavailable(
                    "Usenet engine returned invalid provider-set data"
                )
            try:
                _validate_provider_set_identity(provider_set_id)
            except ValueError as exc:
                raise EngineUnavailable(
                    "Usenet engine returned invalid provider-set data"
                ) from exc
            self._provider_set_ids[generation] = provider_set_id
            return provider_set_id
