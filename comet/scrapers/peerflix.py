from comet.core.logger import log_scraper_error
from comet.scrapers.base import BaseScraper
from comet.scrapers.models import ScrapeRequest


class PeerflixScraper(BaseScraper):
    BASE_URL = "https://peerflix.mov"

    @staticmethod
    def _parse_stream(stream):
        if not isinstance(stream, dict):
            return None

        description = stream.get("description")
        info_hash = stream.get("infoHash")
        sources = stream.get("sources")
        if (
            not isinstance(description, str)
            or not description
            or not isinstance(info_hash, str)
            or not info_hash
            or "fileIdx" not in stream
            or not isinstance(sources, list)
        ):
            return None

        parts = description.split("🌐")
        tracker = parts[1] if len(parts) > 1 else None
        return {
            "title": description.split("\n")[0],
            "infoHash": info_hash.lower(),
            "fileIndex": stream["fileIdx"],
            "seeders": stream.get("seed"),
            "size": stream.get("sizebytes"),
            "tracker": f"Peerflix|{tracker}"
            if tracker and tracker != "Peerflix"
            else "Peerflix",
            "sources": sources,
        }

    async def scrape(self, request: ScrapeRequest):
        torrents = []
        try:
            async with self.session.get(
                f"{self.BASE_URL}/stream/{request.media_type}/{request.media_id}.json",
            ) as response:
                if response.status == 404:
                    return []
                results = await response.json()

            if not isinstance(results, dict) or not isinstance(
                results.get("streams"), list
            ):
                return []

            for stream in results["streams"]:
                parsed = self._parse_stream(stream)
                if parsed is not None:
                    torrents.append(parsed)
        except Exception as e:
            log_scraper_error("Peerflix", self.BASE_URL, request.media_id, e)

        return torrents
