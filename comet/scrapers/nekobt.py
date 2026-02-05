from comet.core.logger import log_scraper_error
from comet.scrapers.base import BaseScraper
from comet.scrapers.models import ScrapeRequest
from comet.services.torrent_manager import extract_trackers_from_magnet

BASE_URL = "https://nekobt.to/api/v1/torrents/search"
PAGE_LIMIT = 100


class NekoBTScraper(BaseScraper):
    def __init__(self, manager, session):
        super().__init__(manager, session)

    def _parse_torrent(self, item: dict) -> dict | None:
        info_hash = item["infohash"]
        if not info_hash:
            return None

        title = item["title"] or item["auto_title"]
        if not title:
            return None

        magnet = item["magnet"] or item.get("private_magnet")

        return {
            "title": title,
            "infoHash": info_hash,
            "fileIndex": None,
            "seeders": int(item["seeders"]),
            "size": int(item["filesize"]),
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

        if payload["error"]:
            return [], False, None

        data = payload["data"]
        results = data["results"]

        recommended = data.get("recommended_media")
        similar = data.get("similar_media")
        media_id = (
            recommended["id"]
            if recommended
            else (similar[0]["id"] if similar else None)
        )

        torrents = []
        for item in results:
            if t := self._parse_torrent(item):
                torrents.append(t)

        return torrents, data["more"], media_id

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
            torrents, media_id = await self._fetch_all({"query": request.title})

            if media_id:
                media_torrents, _ = await self._fetch_all({"media_id": media_id})
                seen = {t["infoHash"] for t in torrents}
                for t in media_torrents:
                    if t["infoHash"] not in seen:
                        torrents.append(t)
                        seen.add(t["infoHash"])

            return torrents
        except Exception as e:
            log_scraper_error("NekoBT", BASE_URL, request.media_id, e)
            return []
