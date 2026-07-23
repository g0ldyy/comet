from comet.core.logger import log_scraper_error
from comet.scrapers.base import BaseScraper
from comet.scrapers.helpers.mediafusion import mediafusion_config
from comet.scrapers.models import ScrapeRequest


class MediaFusionScraper(BaseScraper):
    def __init__(
        self,
        manager,
        session,
        url: str,
        password: str | None = None,
    ):
        super().__init__(manager, session, url)
        self.password = password

    @staticmethod
    def _parse_stream(torrent):
        if not isinstance(torrent, dict):
            return None

        title_full = torrent.get("description")
        info_hash = torrent.get("infoHash")
        behavior_hints = torrent.get("behaviorHints")
        sources = torrent.get("sources", [])
        if (
            not isinstance(title_full, str)
            or not isinstance(info_hash, str)
            or not info_hash
            or not isinstance(behavior_hints, dict)
            or "videoSize" not in behavior_hints
            or not isinstance(sources, list)
        ):
            return None

        lines = title_full.split("\n")
        if len(lines) < 2 or "🔗 " not in lines[-1]:
            return None

        title = lines[0].replace("📂 ", "").replace("/", "")
        seeders = None
        if "👤" in lines[1]:
            try:
                seeders = int(lines[1].split("👤 ", 1)[1])
            except (IndexError, ValueError):
                return None

        tracker = lines[-1].split("🔗 ", 1)[1]
        return {
            "title": title,
            "infoHash": info_hash.lower(),
            "fileIndex": torrent.get("fileIdx"),
            "seeders": seeders,
            # This is the selected video size, not the pack size.
            "size": behavior_hints["videoSize"],
            "tracker": f"MediaFusion|{tracker}",
            "sources": sources,
        }

    async def scrape(self, request: ScrapeRequest):
        torrents = []
        try:
            headers = mediafusion_config.get_headers_for_password(self.password)

            async with self.session.get(
                f"{self.url}/stream/{request.media_type}/{request.media_id}.json",
                headers=headers,
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
            log_scraper_error("MediaFusion", self.url, request.media_id, e)

        return torrents
