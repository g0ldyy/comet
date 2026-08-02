"""Configured Stremio-addon ingestion for standard real ``nzbUrl`` streams."""

import asyncio
import hashlib
import json
from dataclasses import dataclass
from urllib.parse import quote, urlsplit
from uuid import UUID

from comet.core.models import settings
from comet.core.sources import (
    REAL_NZB_PROVIDER_KINDS,
    LocatorKind,
    LocatorPolicy,
    NzbArtifactRef,
    ReleaseCandidate,
    TransportKind,
)
from comet.discovery.models import DiscoveryBatch, DiscoveryContext, MediaQuery
from comet.playback.base import Actionability, ProviderStatus, Readiness
from comet.usenet.outbound import OutboundUrlError, fetch_http_bytes

_MAX_JSON_BYTES = 2 * 1024 * 1024
_MAX_NZB_BYTES = 150 * 1024 * 1024
_MAX_NZB_ATTEMPTS = 20


@dataclass(frozen=True, slots=True)
class StremioAddonConfiguration:
    configuration_id: str
    base_url: str
    manifest_url: str
    origin: str
    authorization: str | None
    max_results: int


def stremio_addon_configuration(
    configuration_id: str,
    options: object,
) -> StremioAddonConfiguration:
    if not isinstance(options, dict):
        raise ValueError("Stremio addon options must be an object")
    raw_manifest = options.get("manifestUrl")
    raw_base = options.get("baseUrl")
    if raw_manifest is not None:
        manifest_url, origin = _configured_url(raw_manifest)
        base_url = manifest_url.rpartition("/")[0]
        if not base_url:
            raise ValueError("Stremio addon manifest URL is invalid")
    elif raw_base is not None:
        base_url, origin = _configured_url(raw_base)
        manifest_url = base_url + "/manifest.json"
    else:
        raise ValueError("Stremio addon URL is required")
    authorization = options.get("authorization")
    if authorization is not None and (
        not isinstance(authorization, str)
        or not 1 <= len(authorization) <= 2_048
        or authorization != authorization.strip()
        or not _is_utf8(authorization)
        or any(
            ord(character) < 32 or ord(character) == 127 for character in authorization
        )
    ):
        raise ValueError("Stremio addon authorization is invalid")
    maximum = options.get("maxResults", 3)
    if (
        isinstance(maximum, bool)
        or not isinstance(maximum, int)
        or not 1 <= maximum <= 10
    ):
        raise ValueError("Stremio addon result limit is invalid")
    if not isinstance(configuration_id, str) or not _canonical_uuid(configuration_id):
        raise ValueError("Stremio addon configuration ID is invalid")
    return StremioAddonConfiguration(
        configuration_id,
        base_url,
        manifest_url,
        origin,
        authorization,
        maximum,
    )


class StremioAddonAdapter:
    def __init__(
        self,
        configuration: StremioAddonConfiguration,
        *,
        broker=None,
        governor=None,
        governor_scope: bytes | None = None,
    ):
        self._configuration = configuration
        self._broker = broker
        self._governor = governor
        self._governor_scope = governor_scope

    async def validate_config(self) -> ProviderStatus:
        try:
            payload = _json_object(
                await self._fetch(
                    self._configuration.manifest_url,
                    maximum=256 * 1024,
                    accept="application/json",
                )
            )
            resources = payload.get("resources")
            if not isinstance(resources, list) or not any(
                item == "stream"
                or (isinstance(item, dict) and item.get("name") == "stream")
                for item in resources
            ):
                raise ValueError("Stremio addon has no stream resource")
        except ValueError:
            return ProviderStatus(
                Readiness.TERMINAL_FAILURE,
                Actionability.NONE,
                code="addon_manifest_invalid",
            )
        except OutboundUrlError:
            return ProviderStatus(
                Readiness.RETRYABLE_FAILURE,
                Actionability.NONE,
                code="addon_unavailable",
            )
        return ProviderStatus(Readiness.READY, Actionability.NONE)

    async def search(
        self,
        query: MediaQuery,
        context: DiscoveryContext,
    ) -> DiscoveryBatch:
        if "usenet" not in context.branches:
            return DiscoveryBatch()
        if (
            self._broker is None
            or not isinstance(context.account_partition, bytes)
            or len(context.account_partition) != 32
        ):
            return DiscoveryBatch(
                diagnostics=("Stremio addon NZB brokerage is unavailable",)
            )
        if context.cancelled():
            return DiscoveryBatch(diagnostics=("Stremio addon search was cancelled",))
        if context.hard_deadline is None:
            return await self._search(query, context)
        remaining = context.hard_deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return DiscoveryBatch(
                diagnostics=("Stremio addon search deadline expired",)
            )
        try:
            async with asyncio.timeout(remaining):
                return await self._search(query, context)
        except TimeoutError:
            return DiscoveryBatch(
                diagnostics=("Stremio addon search deadline expired",)
            )

    async def _search(
        self,
        query: MediaQuery,
        context: DiscoveryContext,
    ) -> DiscoveryBatch:
        payload = _json_object(
            await self._fetch(
                self._stream_url(query),
                maximum=_MAX_JSON_BYTES,
                accept="application/json",
            )
        )
        streams = payload.get("streams")
        if not isinstance(streams, list):
            raise ValueError("Stremio addon stream response is invalid")
        candidates = []
        artifact_identities = set()
        seen_nzb_urls = set()
        incomplete = False
        attempts = 0
        for stream in streams:
            if len(candidates) >= self._configuration.max_results:
                break
            if context.cancelled():
                incomplete = True
                break
            if not isinstance(stream, dict):
                continue
            nzb_url = stream.get("nzbUrl")
            if (
                not isinstance(nzb_url, str)
                or not 1 <= len(nzb_url) <= 4_096
                or not _is_utf8(nzb_url)
            ):
                continue
            if nzb_url in seen_nzb_urls:
                continue
            seen_nzb_urls.add(nzb_url)
            if attempts >= _MAX_NZB_ATTEMPTS:
                incomplete = True
                break
            attempts += 1
            document = await self._fetch(
                nzb_url,
                maximum=_MAX_NZB_BYTES,
                accept=(
                    "application/x-nzb, application/xml, text/xml, "
                    "application/gzip, */*"
                ),
            )
            artifact = await self._broker.ingest_bytes(
                document,
                owner_configuration_partition=context.account_partition,
            )
            if artifact.artifact_sha256 in artifact_identities:
                continue
            artifact_identities.add(artifact.artifact_sha256)
            title = _stream_title(stream, query)
            digest = hashlib.sha256(
                (
                    "stremio-addon:"
                    + self._configuration.configuration_id
                    + ":"
                    + artifact.artifact_sha256
                ).encode()
            ).hexdigest()
            locator = NzbArtifactRef(
                locator_id=f"sa1:locator:{digest}",
                kind=LocatorKind.NZB_ARTIFACT,
                policy=LocatorPolicy(
                    REAL_NZB_PROVIDER_KINDS,
                    owner_configuration_partition=context.account_partition,
                    expires_at=int(artifact.expires_at),
                ),
                artifact_sha256=artifact.artifact_sha256,
                manifest_identity=artifact.nm1,
            )
            candidates.append(
                ReleaseCandidate(
                    candidate_id=f"sa1:{digest}",
                    media_id=query.media_id,
                    scope=query.scope,
                    transport=TransportKind.USENET,
                    title=title,
                    locators=(locator,),
                    source="Stremio addon",
                )
            )
        complete = len(candidates) >= self._configuration.max_results or not incomplete
        diagnostics = (
            ("Stremio addon result scan is incomplete",) if not complete else ()
        )
        return DiscoveryBatch(
            tuple(candidates),
            diagnostics=diagnostics,
            coverage=(frozenset({"usenet"}) if complete else frozenset()),
        )

    def _stream_url(self, query: MediaQuery) -> str:
        media_type = query.media_type
        media_id = query.media_id
        if media_type == "series":
            if query.season is None or query.episode is None:
                raise ValueError("Stremio addon episode identity is incomplete")
            media_id += f":{query.season}:{query.episode}"
        return (
            f"{self._configuration.base_url}/stream/{media_type}/"
            f"{quote(media_id, safe=':')}.json"
        )

    async def _fetch(
        self,
        url: str,
        *,
        maximum: int,
        accept: str,
    ) -> bytes:
        lease = None
        if self._governor is not None:
            lease = await self._governor.acquire_concurrency(
                self._governor_scope,
                "stremio_addon_http",
                limit=4,
                owner_request_id=hashlib.sha256(url.encode()).hexdigest(),
                lease_seconds=25,
            )
            if lease is None:
                raise OutboundUrlError("Stremio addon capacity is busy")
        try:
            return await fetch_http_bytes(
                url,
                max_bytes=maximum,
                headers={"Accept": accept},
                origin_headers=(
                    {"Authorization": self._configuration.authorization}
                    if self._configuration.authorization is not None
                    else None
                ),
                credential_origin=self._configuration.origin,
                allowed_private_origins=(
                    frozenset({self._configuration.origin})
                    if self._configuration.origin
                    in settings.USENET_PRIVATE_UPSTREAM_ORIGINS
                    else frozenset()
                ),
            )
        finally:
            if lease is not None:
                await lease.release()


def _configured_url(value: object) -> tuple[str, str]:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 4_096
        or not _is_utf8(value)
    ):
        raise ValueError("Stremio addon URL is invalid")
    if any(
        character.isspace() or ord(character) < 32 or ord(character) == 127
        for character in value
    ):
        raise ValueError("Stremio addon URL is invalid")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Stremio addon URL is invalid") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port == 0
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Stremio addon URL is invalid")
    host = parsed.hostname.rstrip(".").lower()
    if not host:
        raise ValueError("Stremio addon URL is invalid")
    effective_port = port or (443 if parsed.scheme == "https" else 80)
    origin_host = f"[{host}]" if ":" in host else host
    origin = f"{parsed.scheme}://{origin_host}:{effective_port}"
    if (
        parsed.scheme != "https"
        and origin not in settings.USENET_PRIVATE_UPSTREAM_ORIGINS
    ):
        raise ValueError("Stremio addon requires HTTPS or an allowed origin")
    path = parsed.path.rstrip("/")
    base = f"{parsed.scheme}://{parsed.netloc.lower()}{path}"
    return base, origin


def _canonical_uuid(value: str) -> bool:
    try:
        return str(UUID(value)) == value
    except ValueError:
        return False


def _is_utf8(value: str) -> bool:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _json_object(document: bytes) -> dict:
    if not document:
        raise ValueError("Stremio addon response is invalid")

    try:
        payload = json.loads(
            document,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                ValueError("Stremio addon response number is invalid")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Stremio addon response is invalid") from exc
    if not isinstance(payload, dict):
        raise ValueError("Stremio addon response is invalid")
    return payload


def _stream_title(stream: dict, query: MediaQuery) -> str:
    for value in (stream.get("title"), stream.get("name")):
        if (
            isinstance(value, str)
            and 1 <= len(value) <= 1_024
            and _is_utf8(value)
            and not any(ord(character) < 32 for character in value)
        ):
            return value
    return query.title_aliases[0] if query.title_aliases else "Upstream NZB"
