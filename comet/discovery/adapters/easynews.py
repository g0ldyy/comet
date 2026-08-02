"""Account-bound Easynews search discovery."""

import hashlib
import re
import uuid
from dataclasses import dataclass
from uuid import UUID

import aiohttp
import orjson

from comet.core.provider_json import is_success_status
from comet.core.sources import (
    REAL_NZB_PROVIDER_KINDS,
    EasynewsHttpRef,
    LocatorKind,
    LocatorPolicy,
    ReleaseCandidate,
    TransportKind,
)
from comet.discovery.models import DiscoveryBatch, DiscoveryContext, MediaQuery
from comet.playback.base import Actionability, ProviderStatus, Readiness
from comet.usenet.easynews import (
    EasynewsNzbError,
    authorization_header,
    bounded_retry_after,
    credential,
    generate_nzb,
)

_SEARCH_V2_URL = "https://members.easynews.com/2.0/search/solr-search/advanced"
_MAX_RESULTS = 100
_MAX_JSON_BYTES = 2 * 1024 * 1024
_MAX_SIGNED_64 = (1 << 63) - 1
_QUERY_WHITESPACE = re.compile(r"\s+")


class EasynewsSearchError(RuntimeError):
    def __init__(
        self,
        code: str,
        *,
        retryable: bool = False,
        retry_after: int | None = None,
        auth_failed: bool = False,
    ):
        super().__init__(code)
        self.code = code
        self.retryable = retryable
        self.retry_after = retry_after
        self.auth_failed = auth_failed


@dataclass(frozen=True, slots=True)
class EasynewsSearchAccount:
    username: str
    password: str
    provider_configuration_id: str | None
    source_configuration_id: str | None = None
    generated_provider_kinds: frozenset[str] = frozenset()

    def __post_init__(self):
        credential(self.username)
        credential(self.password)
        for value in (
            self.provider_configuration_id,
            self.source_configuration_id,
        ):
            if value is not None:
                _configuration_id(value)
        provider_kinds = self.generated_provider_kinds
        if (
            not isinstance(provider_kinds, frozenset)
            or provider_kinds
            and self.source_configuration_id is None
            or not provider_kinds <= REAL_NZB_PROVIDER_KINDS
        ):
            raise ValueError("Easynews generated provider kinds are invalid")


def _text(value: object, maximum: int) -> str | None:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or any(ord(character) < 32 for character in value)
    ):
        return None
    return value


def _known_passworded(row: dict) -> bool:
    value = row.get("passwd")
    return value is True or type(value) is int and value == 1 or value == "1"


def _normalize_search_row(row: object, payload: dict) -> dict | None:
    """Translate Easynews' compact search fields into the internal row shape."""
    if not isinstance(row, dict) or not {"0", "10", "11"} <= row.keys():
        return None
    extension = row["11"]
    if isinstance(extension, str):
        extension = extension.removeprefix(".")
    return {
        "file_id": row["0"],
        "filename": row["10"],
        "extension": extension,
        "dlFarm": payload.get("dlFarm"),
        "dlPort": payload.get("dlPort"),
        "size": row.get("rawSize"),
        "sig": row.get("sig"),
        "passwd": row.get("passwd"),
    }


class EasynewsSearchAdapter:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        account: EasynewsSearchAccount,
        *,
        governor=None,
        governor_scope: bytes | None = None,
        runtime_failure_recorder=None,
    ):
        self._session = session
        self._account = account
        self._governor = governor
        self._governor_scope = governor_scope
        self._runtime_failure_recorder = runtime_failure_recorder

    @staticmethod
    def _candidate_id(value: str) -> str:
        return "en1:" + hashlib.sha256(f"easynews:{value}".encode()).hexdigest()

    @staticmethod
    def _rows(payload: object) -> list[dict]:
        if not isinstance(payload, dict):
            raise EasynewsSearchError("easynews_search_invalid")
        data = payload.get("data")
        if not isinstance(data, list):
            raise EasynewsSearchError("easynews_search_invalid")
        rows = []
        for row in data:
            normalized = _normalize_search_row(row, payload)
            if normalized is not None:
                rows.append(normalized)
                if len(rows) == _MAX_RESULTS:
                    break
        return rows

    def _candidate(
        self, row: dict, query: MediaQuery, context: DiscoveryContext
    ) -> ReleaseCandidate | None:
        if _known_passworded(row):
            return None
        if (
            not isinstance(context.account_partition, bytes)
            or len(context.account_partition) != 32
        ):
            raise ValueError("Easynews discovery partition is invalid")
        file_identifier = _text(row.get("file_id"), 256)
        filename = _text(row.get("filename"), 512)
        extension = _text(row.get("extension"), 32)
        farm = row.get("dlFarm")
        port = row.get("dlPort")
        if isinstance(farm, int) and not isinstance(farm, bool):
            farm = str(farm)
        if isinstance(port, int) and not isinstance(port, bool):
            port = str(port)
        farm = _text(farm, 128)
        port = _text(port, 128)
        if not all((file_identifier, filename, extension, farm, port)):
            return None
        raw_signature = row.get("sig")
        signature = None
        if raw_signature is not None and raw_signature != "":
            signature = _text(raw_signature, 512)
        raw_size = row.get("size")
        size = (
            raw_size
            if (
                not isinstance(raw_size, bool)
                and isinstance(raw_size, int)
                and 1 <= raw_size <= _MAX_SIGNED_64
            )
            else None
        )
        locators = []
        if self._account.provider_configuration_id is not None:
            locators.append(
                self._locator(
                    file_identifier,
                    farm,
                    port,
                    file_identifier,
                    "",
                    filename,
                    extension,
                    None,
                    size,
                    allowed_provider_kinds=frozenset({"easynews"}),
                    exact_provider_configuration_id=(
                        self._account.provider_configuration_id
                    ),
                    account_configuration_id=(self._account.provider_configuration_id),
                    partition=context.account_partition,
                    transform="direct",
                )
            )
        if (
            self._account.source_configuration_id is not None
            and self._account.generated_provider_kinds
            and size is not None
            and (raw_signature is None or raw_signature == "" or signature is not None)
        ):
            locators.append(
                self._locator(
                    file_identifier,
                    farm,
                    port,
                    file_identifier,
                    "",
                    filename,
                    extension,
                    signature,
                    size,
                    allowed_provider_kinds=(self._account.generated_provider_kinds),
                    exact_provider_configuration_id=None,
                    account_configuration_id=(self._account.source_configuration_id),
                    partition=context.account_partition,
                    transform="generate",
                )
            )
        if not locators:
            return None
        return ReleaseCandidate(
            candidate_id=self._candidate_id(file_identifier),
            media_id=query.media_id,
            scope=query.scope,
            transport=TransportKind.USENET,
            title=filename,
            locators=tuple(locators),
            size=size,
            source="Easynews",
        )

    def _locator(
        self,
        file_identifier: str,
        farm: str,
        port: str,
        content_hash: str,
        item_identifier: str,
        filename: str,
        extension: str,
        signature: str | None,
        byte_size: int | None,
        *,
        allowed_provider_kinds: frozenset[str],
        exact_provider_configuration_id: str | None,
        account_configuration_id: str,
        partition: bytes,
        transform: str,
    ) -> EasynewsHttpRef:
        return EasynewsHttpRef(
            locator_id=self._candidate_id(f"locator:{transform}:{file_identifier}"),
            kind=LocatorKind.EASYNEWS_HTTP,
            policy=LocatorPolicy(
                allowed_provider_kinds,
                owner_configuration_partition=partition,
                exact_provider_configuration_id=(exact_provider_configuration_id),
            ),
            account_configuration_id=account_configuration_id,
            file_identifier=file_identifier,
            download_farm=farm,
            download_port=port,
            content_hash=content_hash,
            item_identifier=item_identifier,
            filename=filename,
            extension=extension,
            signature=signature,
            byte_size=byte_size,
        )

    async def generate_nzb(self, payload: dict) -> bytes:
        if (
            self._account.source_configuration_id is None
            or payload.get("account_configuration_id")
            != self._account.source_configuration_id
        ):
            raise ValueError("Easynews NZB origin is unavailable")
        lease = None
        if self._governor is not None:
            lease = await self._governor.acquire_concurrency(
                self._governor_scope,
                "easynews_generate_nzb",
                limit=4,
                owner_request_id=str(uuid.uuid4()),
                lease_seconds=125,
            )
            if lease is None:
                raise EasynewsNzbError(
                    "easynews_generate_busy",
                    retryable=True,
                )
        try:
            return await generate_nzb(
                self._session,
                payload,
                self._account.username,
                self._account.password,
            )
        finally:
            if lease is not None:
                await lease.release()

    async def search(
        self,
        query: MediaQuery,
        context: DiscoveryContext,
    ) -> DiscoveryBatch:
        if "usenet" not in context.branches:
            return DiscoveryBatch()
        query_text = _search_query(query)
        try:
            rows = await self._search_rows(query_text, context)
        except EasynewsSearchError as exc:
            await self._record_runtime_failure(exc)
            raise
        candidates = tuple(
            candidate
            for row in rows
            if (candidate := self._candidate(row, query, context)) is not None
        )
        return DiscoveryBatch(candidates, coverage=frozenset({"usenet"}))

    async def _record_runtime_failure(self, error: EasynewsSearchError) -> None:
        source_id = self._account.source_configuration_id
        if (
            self._runtime_failure_recorder is None
            or source_id is None
            or not (error.auth_failed or error.retryable)
        ):
            return
        await self._runtime_failure_recorder(
            source_id,
            ("auth_failed" if error.auth_failed else "transiently_unreachable"),
            ("credentials_rejected" if error.auth_failed else error.code),
            (None if error.auth_failed else error.retry_after or 30),
        )

    async def validate_config(self) -> ProviderStatus:
        """Prove authentication and the configured search contract."""
        context = DiscoveryContext(
            frozenset({"usenet"}),
            self._governor_scope,
            trace_id="easynews-capability-validation",
        )
        try:
            await self._search_rows(
                "comet-capability-validation",
                context,
            )
        except EasynewsSearchError as exc:
            if exc.code == "easynews_auth_failed":
                return ProviderStatus(
                    Readiness.TERMINAL_FAILURE,
                    Actionability.NONE,
                    code="credentials_rejected",
                    auth_failed=True,
                )
            return ProviderStatus(
                Readiness.RETRYABLE_FAILURE,
                Actionability.NONE,
                code="validation_unavailable",
            )
        return ProviderStatus(Readiness.READY, Actionability.NONE)

    async def _search_rows(
        self,
        query_text: str,
        context: DiscoveryContext,
    ) -> list[dict]:
        lease = None
        if self._governor is not None:
            lease = await self._governor.acquire_concurrency(
                self._governor_scope,
                "easynews_search_v2",
                limit=2,
                owner_request_id=context.trace_id or str(uuid.uuid4()),
                lease_seconds=10,
            )
            if lease is None:
                raise EasynewsSearchError("easynews_search_busy")
        try:
            async with self._session.get(
                _SEARCH_V2_URL,
                params=_search_params(query_text),
                headers={
                    "Accept": "application/json",
                    "Accept-Encoding": "identity",
                    **authorization_header(
                        self._account.username,
                        self._account.password,
                    ),
                },
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(
                    total=8,
                    connect=3,
                    sock_read=5,
                ),
            ) as response:
                if response.status in {401, 403}:
                    raise EasynewsSearchError(
                        "easynews_auth_failed",
                        auth_failed=True,
                    )
                if response.status == 429:
                    raise EasynewsSearchError(
                        "easynews_search_rate_limited",
                        retryable=True,
                        retry_after=bounded_retry_after(
                            response.headers.get("Retry-After")
                        ),
                    )
                if response.status >= 500:
                    raise EasynewsSearchError(
                        "easynews_search_unavailable",
                        retryable=True,
                    )
                if not is_success_status(response.status):
                    raise EasynewsSearchError("easynews_search_rejected")
                document = await _read_bounded(response, _MAX_JSON_BYTES)
                return self._rows(_json_object(document))
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise EasynewsSearchError(
                "easynews_search_unavailable",
                retryable=True,
            ) from exc
        finally:
            if lease is not None:
                await lease.release()


def _search_query(query: MediaQuery) -> str:
    title = next(
        (value for value in query.title_aliases if value.strip()),
        query.media_id,
    )
    normalized = _QUERY_WHITESPACE.sub(" ", title).strip()
    if query.season is not None:
        normalized += f" S{query.season:02d}"
        if query.episode is not None:
            normalized += f"E{query.episode:02d}"
    return normalized


def _configuration_id(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("Easynews configuration ID is invalid")
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise ValueError("Easynews configuration ID is invalid") from exc
    if str(parsed) != value:
        raise ValueError("Easynews configuration ID is invalid")
    return value


def _search_params(query_text: str) -> dict[str, str]:
    return {
        "gps": query_text,
        "pno": "1",
        "u": "1",
        "safeO": "0",
        "s1": "relevance",
        "s1d": "-",
        "fty[]": "VIDEO",
        "pby": str(_MAX_RESULTS),
        "sb": "1",
        "st": "adv",
        "sS": "3",
    }


async def _read_bounded(response, maximum: int) -> bytes:
    body = bytearray()
    async for chunk in response.content.iter_chunked(64 * 1024):
        body.extend(chunk)
        if len(body) > maximum:
            raise EasynewsSearchError("easynews_search_response_too_large")
    return bytes(body)


def _json_object(document: bytes) -> dict:
    if not document:
        raise EasynewsSearchError("easynews_search_invalid")

    try:
        payload = orjson.loads(document)
    except orjson.JSONDecodeError as exc:
        raise EasynewsSearchError("easynews_search_invalid") from exc
    if not isinstance(payload, dict):
        raise EasynewsSearchError("easynews_search_invalid")
    return payload
