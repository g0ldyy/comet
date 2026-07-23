from comet.core.logger import log_scraper_error
from comet.core.models import settings
from comet.scrapers.base import BaseScraper
from comet.scrapers.models import ScrapeRequest
from comet.services.torrent_manager import extract_trackers_from_magnet


class TorboxScraper(BaseScraper):
    def __init__(self, manager, session):
        super().__init__(manager, session)

    @staticmethod
    def _parse_torrent(torrent):
        if not isinstance(torrent, dict):
            return None

        title = torrent.get("raw_title")
        info_hash = torrent.get("hash")
        tracker = torrent.get("tracker")
        magnet = torrent.get("magnet")
        if (
            not isinstance(title, str)
            or not title
            or not isinstance(info_hash, str)
            or not info_hash
            or "size" not in torrent
            or not isinstance(tracker, str)
            or not tracker
            or not isinstance(magnet, str)
        ):
            return None

        return {
            "title": title,
            "infoHash": info_hash,
            "fileIndex": None,
            "seeders": torrent.get("last_known_seeders"),
            "size": torrent["size"],
            "tracker": f"TorBox|{tracker}",
            "sources": extract_trackers_from_magnet(magnet),
        }

    async def scrape(self, request: ScrapeRequest):
        torrents = []

        try:
            async with self.session.get(
                f"https://search-api.torbox.app/torrents/imdb:{request.media_only_id}",
                headers={"Authorization": f"Bearer {settings.TORBOX_API_KEY}"},
            ) as response:
                data = await response.json()

            if not isinstance(data, dict) or not isinstance(data.get("data"), dict):
                return []
            torrent_items = data["data"].get("torrents")
            if not isinstance(torrent_items, list):
                return []

            for torrent in torrent_items:
                parsed = self._parse_torrent(torrent)
                if parsed is not None:
                    torrents.append(parsed)
        except Exception as e:
            log_scraper_error(
                "TorBox", settings.TORBOX_API_KEY, request.media_only_id, e
            )

        return torrents
