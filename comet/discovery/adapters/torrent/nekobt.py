from comet.core.provider_json import is_success_status
from comet.discovery.torrent_base import (
    TorrentDiscoveryAdapter,
    deduplicate_torrents,
    gather_concurrently,
    parse_valid_items,
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
        if not isinstance(item, dict):
            raise ValueError("NekoBT result is not an object")

        info_hash = item.get("infohash")
        title = item.get("title") or item.get("auto_title")
        magnet = item.get("magnet") or item.get("private_magnet")
        if (
            not isinstance(info_hash, str)
            or not info_hash
            or not isinstance(title, str)
            or not title
        ):
            raise ValueError("NekoBT result is incomplete")

        try:
            if isinstance(item["seeders"], bool) or isinstance(item["filesize"], bool):
                raise ValueError
            seeders = int(item["seeders"])
            size = int(item["filesize"])
        except (KeyError, TypeError, ValueError):
            raise ValueError("NekoBT result has invalid numeric fields") from None

        return {
            "title": title,
            "infoHash": info_hash,
            "fileIndex": None,
            "seeders": seeders,
            "size": size,
            "tracker": "NekoBT",
            "sources": (
                extract_trackers_from_magnet(magnet) if isinstance(magnet, str) else []
            ),
        }

    async def _fetch_page(self, params: dict) -> tuple[list[dict], bool, str | None]:
        async with self.session.get(BASE_URL, params=params) as resp:
            if not is_success_status(resp.status):
                raise RuntimeError(f"NekoBT returned HTTP {resp.status}")
            payload = await resp.json()

        if not isinstance(payload, dict):
            raise ValueError("NekoBT response is not an object")
        if payload.get("error"):
            raise RuntimeError("NekoBT returned an error")

        data = payload.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("results"), list):
            raise ValueError("NekoBT response is missing its results")
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

        more = data.get("more")
        if not isinstance(more, bool):
            raise ValueError("NekoBT response has an invalid pagination flag")
        return parse_valid_items(results, self._parse_torrent), more, media_id

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
