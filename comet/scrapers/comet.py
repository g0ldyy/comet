from comet.core.logger import log_scraper_error
from comet.scrapers.base import BaseScraper
from comet.scrapers.models import ScrapeRequest


class CometScraper(BaseScraper):
    def __init__(self, manager, session, url: str):
        super().__init__(manager, session, url)

    @staticmethod
    def _parse_stream(torrent):
        if not isinstance(torrent, dict):
            return None

        description = torrent.get("description")
        info_hash = torrent.get("infoHash")
        behavior_hints = torrent.get("behaviorHints")
        sources = torrent.get("sources", [])
        if (
            not isinstance(description, str)
            or not isinstance(info_hash, str)
            or not info_hash
            or not isinstance(behavior_hints, dict)
            or not isinstance(sources, list)
        ):
            return None

        first_line = description.split("\n", 1)[0]
        if "📄 " not in first_line:
            return None
        title = first_line.split("📄 ", 1)[1]
        if not title:
            return None

        seeders = None
        if "👤 " in description:
            try:
                seeders = int(description.split("👤 ", 1)[1].split(" ", 1)[0])
            except (IndexError, ValueError):
                return None

        tracker = None
        if "🔎 " in description:
            tracker = description.split("🔎 ", 1)[1].split("\n", 1)[0]

        return {
            "title": title,
            "infoHash": info_hash.lower(),
            "fileIndex": torrent.get("fileIdx"),
            "seeders": seeders,
            "size": behavior_hints.get("videoSize"),
            "tracker": f"Comet|{tracker}" if tracker is not None else "Comet",
            "sources": sources,
        }

    async def scrape(self, request: ScrapeRequest):
        torrents = []
        try:
            async with self.session.get(
                f"{self.url}/stream/{request.media_type}/{request.media_id}.json",
            ) as response:
                results = await response.json()

            if not isinstance(results, dict) or not isinstance(
                results.get("streams"), list
            ):
                return []

            for torrent in results["streams"]:
                parsed = self._parse_stream(torrent)
                if parsed is not None:
                    torrents.append(parsed)
        except Exception as e:
            log_scraper_error("Comet", self.url, request.media_id, e)

        return torrents
