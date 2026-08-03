from comet.core.provider_json import is_success_status
from comet.discovery.torrent_base import (
    TorrentDiscoveryAdapter,
    deduplicate_torrents,
    gather_concurrently,
)
from comet.discovery.torrent_models import ScrapeRequest
from comet.services.torrent_manager import extract_trackers_from_magnet

BASE_URL = "https://nekobt.to/api/v1/torrents/search"
PAGE_LIMIT = 100
_MAX_RESULTS_PER_SEARCH = 10_000


class NekoBTScraper(TorrentDiscoveryAdapter):
    anime_only_setting = "NEKOBT_ANIME_ONLY"

    def __init__(self, manager, session):
        super().__init__(manager, session)

    def _parse_torrent(self, item: dict) -> dict:
        info_hash = item["infohash"]
        title = item["title"] or item["auto_title"]
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
        async with self.session.get(BASE_URL, params=params) as resp:
            if not is_success_status(resp.status):
                raise RuntimeError(f"NekoBT returned HTTP {resp.status}")
            payload = await resp.json()

        if payload.get("error"):
            raise RuntimeError("NekoBT returned an error")

        data = payload["data"]
        results = data["results"]

        recommended = data.get("recommended_media")
        similar = data.get("similar_media")
        media_id = (
            recommended["id"] if recommended else similar[0]["id"] if similar else None
        )

        return [self._parse_torrent(item) for item in results], data["more"], media_id

    async def _fetch_all(self, base_params: dict) -> tuple[list[dict], str | None]:
        params = {**base_params, "limit": PAGE_LIMIT, "offset": 0}
        torrents, more, media_id = await self._fetch_page(params)
        if len(torrents) > _MAX_RESULTS_PER_SEARCH:
            raise ValueError("NekoBT search exceeds the result limit")

        if not more:
            return torrents, media_id

        for offset in range(
            PAGE_LIMIT,
            _MAX_RESULTS_PER_SEARCH,
            PAGE_LIMIT,
        ):
            if not more:
                return torrents, media_id
            params["offset"] = offset
            page_torrents, more, _ = await self._fetch_page(params)
            torrents.extend(page_torrents)
            if len(torrents) > _MAX_RESULTS_PER_SEARCH:
                raise ValueError("NekoBT search exceeds the result limit")
        if more:
            raise ValueError("NekoBT search exceeds the result limit")
        return torrents, media_id

    async def scrape(self, request: ScrapeRequest) -> list[dict]:
        query_results = await gather_concurrently(
            self._fetch_all({"query": title}) for title in request.query_titles
        )
        torrents = [
            torrent for query_torrents, _ in query_results for torrent in query_torrents
        ]
        media_ids = tuple(
            dict.fromkeys(media_id for _, media_id in query_results if media_id)
        )
        media_results = await gather_concurrently(
            self._fetch_all({"media_id": media_id}) for media_id in media_ids
        )
        torrents.extend(
            torrent for media_torrents, _ in media_results for torrent in media_torrents
        )
        return deduplicate_torrents(torrents)
