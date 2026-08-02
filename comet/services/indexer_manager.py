import asyncio
import json
import xml.etree.ElementTree as ET
from datetime import UTC, datetime

import aiohttp

from comet.core.constants import indexer_timeout
from comet.core.models import normalize_indexer_name, settings
from comet.core.provider_json import is_success_status
from comet.observability import log
from comet.utils.http_client import read_bounded_body

MAX_INDEXER_RESPONSE_BYTES = 2 * 1024 * 1024
_MAX_ACTIVE_INDEXERS = 64
_MAX_INDEXER_ID_BYTES = 128


class InvalidIndexerResponse(ValueError):
    pass


class IndexerRefreshError(RuntimeError):
    pass


def _bounded_indexer_id(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        return len(value.encode("utf-8")) <= _MAX_INDEXER_ID_BYTES
    except UnicodeEncodeError:
        return False


def _indexer_name(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    return normalize_indexer_name(value)


def _reject_json_constant(_value):
    raise InvalidIndexerResponse("invalid indexer JSON constant")


def decode_indexer_json(document: bytes):
    try:
        return json.loads(
            document.decode("utf-8"),
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise InvalidIndexerResponse("invalid indexer JSON") from None


async def read_indexer_json(response):
    try:
        document = await read_bounded_body(response, MAX_INDEXER_RESPONSE_BYTES)
    except ValueError:
        raise InvalidIndexerResponse("invalid indexer JSON") from None
    return decode_indexer_json(document)


async def read_indexer_xml(response):
    try:
        document = await read_bounded_body(response, MAX_INDEXER_RESPONSE_BYTES)
    except ValueError:
        raise InvalidIndexerResponse("invalid indexer XML") from None
    if b"<!entity" in document.lower():
        raise InvalidIndexerResponse("invalid indexer XML")
    try:
        root = ET.fromstring(document)
    except ET.ParseError:
        raise InvalidIndexerResponse("invalid indexer XML") from None
    if root.tag != "indexers":
        raise InvalidIndexerResponse("invalid indexer XML")
    return root


def _active_jackett_ids(root, configured_ids: list[str]) -> list[str]:
    configured = {value.casefold() for value in configured_ids}
    active_ids = []
    seen = set()
    for indexer in root.findall("indexer"):
        indexer_id = indexer.get("id")
        if not _bounded_indexer_id(indexer_id) or indexer_id.casefold() in seen:
            continue
        if configured:
            title = indexer.find("title")
            name = _indexer_name(title.text if title is not None else None)
            if indexer_id.casefold() not in configured and name not in configured:
                continue
        seen.add(indexer_id.casefold())
        active_ids.append(indexer_id)
        if len(active_ids) == _MAX_ACTIVE_INDEXERS:
            break
    return active_ids


def _active_prowlarr_ids(
    indexers, statuses, configured_ids: list[str], current_time: datetime
) -> list[str]:
    if not isinstance(indexers, list) or not isinstance(statuses, list):
        raise InvalidIndexerResponse("invalid Prowlarr indexer response")

    status_map = {}
    for status in statuses:
        if (
            not isinstance(status, dict)
            or isinstance(status.get("indexerId"), bool)
            or not isinstance(status.get("indexerId"), int)
        ):
            continue
        indexer_id = status["indexerId"]
        status_map[indexer_id] = status
    configured = {value.casefold() for value in configured_ids}
    active_ids = []
    seen = set()
    for indexer in indexers:
        if not isinstance(indexer, dict):
            continue
        indexer_id = indexer.get("id")
        if (
            indexer.get("enable") is not True
            or indexer.get("protocol") != "torrent"
            or not isinstance(indexer_id, int)
            or isinstance(indexer_id, bool)
            or indexer_id in seen
        ):
            continue

        status = status_map.get(indexer_id)
        if status is not None:
            disabled_till = status.get("disabledTill")
            if isinstance(disabled_till, str):
                try:
                    disabled_until = datetime.fromisoformat(disabled_till)
                except ValueError:
                    pass
                else:
                    if (
                        disabled_until.tzinfo is not None
                        and disabled_until > current_time
                    ):
                        continue

        indexer_id_text = str(indexer_id)
        if configured:
            candidates = {
                indexer_id_text.casefold(),
                _indexer_name(indexer.get("name")),
                _indexer_name(indexer.get("definitionName")),
            }
            if configured.isdisjoint(candidates):
                continue
        seen.add(indexer_id)
        active_ids.append(indexer_id_text)
        if len(active_ids) == _MAX_ACTIVE_INDEXERS:
            break
    return active_ids


class IndexerManager:
    def __init__(self):
        self.session: aiohttp.ClientSession | None = None
        self.refresh_interval = settings.INDEXER_MANAGER_UPDATE_INTERVAL
        self.original_jackett_config = settings.JACKETT_INDEXERS.copy()
        self.original_prowlarr_config = settings.PROWLARR_INDEXERS.copy()
        self.active_jackett_config = self.original_jackett_config.copy()
        self.active_prowlarr_config = self.original_prowlarr_config.copy()
        self.jackett_initialized = asyncio.Event()
        self.prowlarr_initialized = asyncio.Event()
        self._configuration_changed = asyncio.Event()

    async def get_session(self):
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(auto_decompress=False)
        return self.session

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()
        self.session = None

    def reconfigure(self, config) -> None:
        self.refresh_interval = config.INDEXER_MANAGER_UPDATE_INTERVAL
        self.original_jackett_config = config.JACKETT_INDEXERS.copy()
        self.original_prowlarr_config = config.PROWLARR_INDEXERS.copy()
        self.active_jackett_config = self.original_jackett_config.copy()
        self.active_prowlarr_config = self.original_prowlarr_config.copy()
        self.jackett_initialized.clear()
        self.prowlarr_initialized.clear()
        self._configuration_changed.set()

    async def _fetch_prowlarr_json(self, session, path: str, headers: dict):
        async with session.get(
            f"{settings.PROWLARR_URL}{path}",
            headers={
                **headers,
                "Accept": "application/json",
                "Accept-Encoding": "identity",
            },
            allow_redirects=False,
            timeout=indexer_timeout(),
        ) as response:
            if not is_success_status(response.status):
                return response.status, None
            return response.status, await read_indexer_json(response)

    async def update_jackett(self):
        try:
            if (
                not settings.is_any_context_enabled(settings.SCRAPE_JACKETT)
                or not settings.JACKETT_URL
                or not settings.JACKETT_API_KEY
            ):
                return

            session = await self.get_session()
            url = f"{settings.JACKETT_URL}/api/v2.0/indexers/!status:failing/results/torznab/api"
            params = {
                "apikey": settings.JACKETT_API_KEY,
                "t": "indexers",
                "configured": "true",
            }
            async with session.get(
                url,
                params=params,
                headers={
                    "Accept": "application/xml",
                    "Accept-Encoding": "identity",
                },
                allow_redirects=False,
                timeout=indexer_timeout(),
            ) as response:
                if not is_success_status(response.status):
                    raise IndexerRefreshError(f"Jackett HTTP {response.status}")

                root = await read_indexer_xml(response)
                active_ids = _active_jackett_ids(root, self.original_jackett_config)

                self.active_jackett_config = active_ids

        finally:
            self.jackett_initialized.set()

    async def update_prowlarr(self):
        try:
            if (
                not settings.is_any_context_enabled(settings.SCRAPE_PROWLARR)
                or not settings.PROWLARR_URL
                or not settings.PROWLARR_API_KEY
            ):
                return

            session = await self.get_session()
            headers = {"X-Api-Key": settings.PROWLARR_API_KEY}

            responses = await asyncio.gather(
                self._fetch_prowlarr_json(session, "/api/v1/indexer", headers),
                self._fetch_prowlarr_json(session, "/api/v1/indexerstatus", headers),
            )

            (indexers_status, indexers), (statuses_status, statuses) = responses

            if not (
                is_success_status(indexers_status)
                and is_success_status(statuses_status)
            ):
                raise IndexerRefreshError(
                    f"Prowlarr HTTP {indexers_status}/{statuses_status}"
                )

            current_time = datetime.now(UTC)
            active_ids = _active_prowlarr_ids(
                indexers,
                statuses,
                self.original_prowlarr_config,
                current_time,
            )

            self.active_prowlarr_config = active_ids

        finally:
            self.prowlarr_initialized.set()

    async def run(self):
        while True:
            self._configuration_changed.clear()
            for source, refresh in (
                ("jackett", self.update_jackett),
                ("prowlarr", self.update_prowlarr),
            ):
                try:
                    await refresh()
                except (
                    TimeoutError,
                    aiohttp.ClientError,
                    InvalidIndexerResponse,
                    IndexerRefreshError,
                ) as exc:
                    log.warning(
                        "indexer.refresh.failed",
                        "Indexer refresh failed",
                        provider_name=source,
                        operation="refresh",
                        error_code="dependency_warning",
                        exc=exc,
                    )
            try:
                await asyncio.wait_for(
                    self._configuration_changed.wait(),
                    timeout=self.refresh_interval,
                )
            except TimeoutError:
                pass


indexer_manager = IndexerManager()


def active_jackett_indexers() -> list[str]:
    return indexer_manager.active_jackett_config


def active_prowlarr_indexers() -> list[str]:
    return indexer_manager.active_prowlarr_config
