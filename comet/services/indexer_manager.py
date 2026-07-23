import asyncio
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Optional

import aiohttp

from comet.core.constants import INDEXER_TIMEOUT
from comet.core.logger import logger
from comet.core.models import settings


def _active_jackett_ids(root, configured_ids: list[str]) -> list[str]:
    configured = {value.lower() for value in configured_ids}
    active_ids = []
    for indexer in root.findall("indexer"):
        indexer_id = indexer.get("id")
        if not isinstance(indexer_id, str) or not indexer_id:
            continue
        if configured:
            title = indexer.find("title")
            name = (
                title.text if title is not None and isinstance(title.text, str) else ""
            )
            if indexer_id.lower() not in configured and name.lower() not in configured:
                continue
        active_ids.append(indexer_id)
    return active_ids


def _active_prowlarr_ids(
    indexers, statuses, configured_ids: list[str], current_time: datetime
) -> list[str]:
    if not isinstance(indexers, list) or not isinstance(statuses, list):
        return []

    status_map = {
        status["indexerId"]: status
        for status in statuses
        if isinstance(status, dict)
        and isinstance(status.get("indexerId"), int)
        and not isinstance(status["indexerId"], bool)
    }
    configured = {value.lower() for value in configured_ids}
    active_ids = []
    for indexer in indexers:
        if not isinstance(indexer, dict):
            continue
        indexer_id = indexer.get("id")
        if (
            indexer.get("enable") is not True
            or indexer.get("protocol") != "torrent"
            or not isinstance(indexer_id, int)
            or isinstance(indexer_id, bool)
            or indexer_id <= 0
        ):
            continue

        status = status_map.get(indexer_id)
        if status is not None:
            disabled_till = status.get("disabledTill")
            if disabled_till is not None:
                if not isinstance(disabled_till, str):
                    continue
                try:
                    disabled_until = datetime.fromisoformat(
                        disabled_till.replace("Z", "+00:00")
                    )
                except ValueError:
                    continue
                if disabled_until.tzinfo is None or disabled_until > current_time:
                    continue

        indexer_id_text = str(indexer_id)
        if configured:
            name = indexer.get("name")
            definition_name = indexer.get("definitionName")
            candidates = {
                indexer_id_text.lower(),
                name.lower() if isinstance(name, str) else "",
                definition_name.lower() if isinstance(definition_name, str) else "",
            }
            if configured.isdisjoint(candidates):
                continue
        active_ids.append(indexer_id_text)
    return active_ids


class IndexerManager:
    def __init__(self):
        self.session: Optional[aiohttp.ClientSession] = None
        self.refresh_interval = settings.INDEXER_MANAGER_UPDATE_INTERVAL
        self.original_jackett_config = settings.JACKETT_INDEXERS.copy()
        self.original_prowlarr_config = settings.PROWLARR_INDEXERS.copy()
        self.jackett_initialized = asyncio.Event()
        self.prowlarr_initialized = asyncio.Event()

    async def get_session(self):
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession()
        return self.session

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()
        self.session = None

    async def _fetch_prowlarr_json(self, session, path: str, headers: dict):
        async with session.get(
            f"{settings.PROWLARR_URL}{path}",
            headers=headers,
            timeout=INDEXER_TIMEOUT,
        ) as response:
            if response.status != 200:
                return response.status, None
            return response.status, await response.json()

    async def update_jackett(self):
        try:
            if (
                not settings.is_any_context_enabled(settings.SCRAPE_JACKETT)
                or not settings.JACKETT_URL
                or not settings.JACKETT_API_KEY
            ):
                return

            try:
                session = await self.get_session()
                url = f"{settings.JACKETT_URL}/api/v2.0/indexers/!status:failing/results/torznab/api"
                params = {
                    "apikey": settings.JACKETT_API_KEY,
                    "t": "indexers",
                    "configured": "true",
                }
                async with session.get(
                    url, params=params, timeout=INDEXER_TIMEOUT
                ) as response:
                    if response.status != 200:
                        logger.warning(
                            f"Failed to fetch Jackett indexers: {response.status}"
                        )
                        return

                    content = await response.text()
                    root = ET.fromstring(content)
                    active_ids = _active_jackett_ids(root, self.original_jackett_config)

                    if sorted(settings.JACKETT_INDEXERS) != sorted(active_ids):
                        settings.JACKETT_INDEXERS = active_ids
                        logger.log(
                            "COMET",
                            f"Updated Jackett indexers ({len(active_ids)}): {', '.join(active_ids)}",
                        )

            except Exception as e:
                logger.warning(f"Error updating Jackett indexers: {e}")

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

            try:
                session = await self.get_session()
                headers = {"X-Api-Key": settings.PROWLARR_API_KEY}

                responses = await asyncio.gather(
                    self._fetch_prowlarr_json(session, "/api/v1/indexer", headers),
                    self._fetch_prowlarr_json(
                        session, "/api/v1/indexerstatus", headers
                    ),
                    return_exceptions=True,
                )

                if any(isinstance(r, Exception) for r in responses):
                    logger.warning("Failed to fetch Prowlarr indexers or statuses")
                    return

                (indexers_status, indexers), (statuses_status, statuses) = responses

                if indexers_status != 200 or statuses_status != 200:
                    logger.warning(
                        f"Prowlarr error: Indexers {indexers_status}, Status {statuses_status}"
                    )
                    return

                current_time = datetime.now(timezone.utc)
                active_ids = _active_prowlarr_ids(
                    indexers,
                    statuses,
                    self.original_prowlarr_config,
                    current_time,
                )

                if sorted(settings.PROWLARR_INDEXERS) != sorted(active_ids):
                    settings.PROWLARR_INDEXERS = active_ids

                    # Map IDs to names for logging
                    id_to_name = {
                        str(i.get("id")): i.get("name", str(i.get("id")))
                        for i in indexers
                        if isinstance(i, dict)
                    }
                    active_names = [
                        id_to_name.get(idx_id, idx_id) for idx_id in active_ids
                    ]

                    logger.log(
                        "COMET",
                        f"Updated Prowlarr indexers ({len(active_ids)}): {', '.join(active_names)}",
                    )

            except Exception as e:
                logger.warning(f"Error updating Prowlarr indexers: {e}")

        finally:
            self.prowlarr_initialized.set()

    async def run(self):
        while True:
            await self.update_jackett()
            await self.update_prowlarr()
            await asyncio.sleep(self.refresh_interval)


indexer_manager = IndexerManager()
