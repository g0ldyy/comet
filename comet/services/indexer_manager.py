import asyncio
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Optional

import aiohttp

from comet.core.constants import INDEXER_TIMEOUT
from comet.core.logger import logger
from comet.core.models import settings


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
                    active_ids = []

                    for indexer in root.findall("indexer"):
                        indexer_id = indexer.get("id")
                        if not indexer_id:
                            continue
                        active_ids.append(indexer_id)

                    # Filter if original config exists
                    if self.original_jackett_config:
                        config_set = {x.lower() for x in self.original_jackett_config}
                        filtered_ids = []
                        for indexer in root.findall("indexer"):
                            pid = indexer.get("id")
                            title = indexer.find("title")
                            name = title.text if title is not None else ""
                            if pid.lower() in config_set or name.lower() in config_set:
                                filtered_ids.append(pid)
                        active_ids = filtered_ids

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

                indexers_task = session.get(
                    f"{settings.PROWLARR_URL}/api/v1/indexer",
                    headers=headers,
                    timeout=INDEXER_TIMEOUT,
                )
                statuses_task = session.get(
                    f"{settings.PROWLARR_URL}/api/v1/indexerstatus",
                    headers=headers,
                    timeout=INDEXER_TIMEOUT,
                )

                responses = await asyncio.gather(
                    indexers_task, statuses_task, return_exceptions=True
                )

                if any(isinstance(r, Exception) for r in responses):
                    logger.warning("Failed to fetch Prowlarr indexers or statuses")
                    return

                resp_idx, resp_stat = responses

                if resp_idx.status != 200 or resp_stat.status != 200:
                    logger.warning(
                        f"Prowlarr error: Indexers {resp_idx.status}, Status {resp_stat.status}"
                    )
                    return

                indexers = await resp_idx.json()

                status_map = {s["indexerId"]: s for s in statuses}
                active_ids = []
                current_time = datetime.now(timezone.utc)

                for indexer in indexers:
                    if not indexer.get("enable"):
                        continue

                    if indexer.get("protocol") != "torrent":
                        continue

                    idx_id = indexer.get("id")

                    # Check health
                    status = status_map.get(idx_id, {})
                    disabled_till = status.get("disabledTill")
                    if disabled_till:
                        try:
                            dt = datetime.fromisoformat(
                                disabled_till.replace("Z", "+00:00")
                            )
                            if dt > current_time:
                                continue
                        except ValueError:
                            pass  # Ignore parsing error, assume enabled

                    active_ids.append(str(idx_id))

                # Apply original config filter configuration
                if self.original_prowlarr_config:
                    config_set = {x.lower() for x in self.original_prowlarr_config}
                    filtered_ids = []
                    for indexer in indexers:
                        idx_id_str = str(indexer.get("id"))
                        if idx_id_str not in active_ids:
                            continue

                        name = indexer.get("name", "").lower()
                        def_name = indexer.get("definitionName", "").lower()

                        if (
                            name in config_set
                            or def_name in config_set
                            or idx_id_str in config_set  # support ID in config too
                        ):
                            filtered_ids.append(idx_id_str)
                    active_ids = filtered_ids

                if sorted(settings.PROWLARR_INDEXERS) != sorted(active_ids):
                    settings.PROWLARR_INDEXERS = active_ids

                    # Map IDs to names for logging
                    id_to_name = {
                        str(i.get("id")): i.get("name", str(i.get("id")))
                        for i in indexers
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
