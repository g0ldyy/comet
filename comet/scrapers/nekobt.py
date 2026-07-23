import asyncio

from comet.core.logger import log_scraper_error
from comet.scrapers.base import BaseScraper, deduplicate_torrents
from comet.scrapers.models import ScrapeRequest
from comet.services.torrent_manager import extract_trackers_from_magnet

BASE_URL = "https://nekobt.to/api/v1/torrents/search"
PAGE_LIMIT = 100


class NekoBTScraper(BaseScraper):
    def __init__(self, manager, session):
        super().__init__(manager, session)

    def _parse_torrent(self, item: dict) -> dict | None:
        if not isinstance(item, dict):
            return None

        info_hash = item.get("infohash")
        title = item.get("title") or item.get("auto_title")
        magnet = item.get("magnet") or item.get("private_magnet")
        if (
            not isinstance(info_hash, str)
            or not info_hash
            or not isinstance(title, str)
            or not title
            or not isinstance(magnet, str)
        ):
            return None

        try:
            seeders = int(item["seeders"])
            size = int(item["filesize"])
        except (KeyError, TypeError, ValueError):
            return None

        return {
            "title": title,
            "infoHash": info_hash,
            "fileIndex": None,
            "seeders": seeders,
            "size": size,
            "tracker": "NekoBT",
            "sources": extract_trackers_from_magnet(magnet),
        }

    async def _fetch_page(self, params: dict) -> tuple[list[dict], bool, str | None]:
        try:
            async with self.session.get(BASE_URL, params=params) as resp:
                if resp.status != 200:
                    return [], False, None
                payload = await resp.json()
        except Exception:
            return [], False, None

        if not isinstance(payload, dict) or payload.get("error"):
            return [], False, None

        data = payload.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("results"), list):
            return [], False, None
        results = data["results"]

        recommended = data.get("recommended_media")
        similar = data.get("similar_media")
        media_id = None
        if isinstance(recommended, dict) and isinstance(recommended.get("id"), str):
            media_id = recommended["id"]
        elif (
            isinstance(similar, list)
            and similar
            and isinstance(similar[0], dict)
            and isinstance(similar[0].get("id"), str)
        ):
            media_id = similar[0]["id"]

        torrents = []
        for item in results:
            if t := self._parse_torrent(item):
                torrents.append(t)

        more = data.get("more")
        return torrents, more if isinstance(more, bool) else False, media_id

    async def _fetch_all(self, base_params: dict) -> tuple[list[dict], str | None]:
        params = {**base_params, "limit": PAGE_LIMIT, "offset": 0}
        torrents, more, media_id = await self._fetch_page(params)

        if not more:
            return torrents, media_id

        offset = PAGE_LIMIT
        while more:
            params["offset"] = offset
            page_torrents, more, _ = await self._fetch_page(params)
            torrents.extend(page_torrents)
            offset += PAGE_LIMIT

        return torrents, media_id

    async def scrape(self, request: ScrapeRequest) -> list[dict]:
        try:
            query_results = await asyncio.gather(
                *(self._fetch_all({"query": title}) for title in request.query_titles)
            )
            torrents = [
                torrent
                for query_torrents, _ in query_results
                for torrent in query_torrents
            ]
            media_ids = tuple(
                dict.fromkeys(media_id for _, media_id in query_results if media_id)
            )
            media_results = await asyncio.gather(
                *(self._fetch_all({"media_id": media_id}) for media_id in media_ids)
            )
            torrents.extend(
                torrent
                for media_torrents, _ in media_results
                for torrent in media_torrents
            )
            return deduplicate_torrents(torrents)
        except Exception as e:
            log_scraper_error("NekoBT", BASE_URL, request.media_id, e)
            return []
