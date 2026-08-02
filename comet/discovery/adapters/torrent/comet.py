from comet.core.provider_json import is_success_status
from comet.discovery.torrent_base import TorrentDiscoveryAdapter
from comet.discovery.torrent_models import ScrapeRequest


class CometScraper(TorrentDiscoveryAdapter):
    url_setting = "COMET_URL"

    def __init__(self, manager, session, url: str):
        super().__init__(manager, session, url)

    @staticmethod
    def _parse_stream(torrent):
        if not isinstance(torrent, dict):
            raise ValueError("Comet result is invalid")

        description = torrent.get("description")
        info_hash = torrent.get("infoHash")
        behavior_hints = torrent.get("behaviorHints")
        sources = torrent.get("sources", [])
        if (
            not isinstance(description, str)
            or not description
            or not isinstance(info_hash, str)
            or not info_hash
            or not isinstance(sources, list)
        ):
            raise ValueError("Comet result is invalid")

        first_line = description.split("\n", 1)[0]
        title = first_line.removeprefix("📄 ")
        if not title:
            raise ValueError("Comet result is invalid")

        seeders = None
        if "👤 " in description:
            try:
                seeders = int(description.split("👤 ", 1)[1].split(" ", 1)[0])
            except (IndexError, ValueError):
                pass

        tracker = None
        if "🔎 " in description:
            tracker = description.split("🔎 ", 1)[1].split("\n", 1)[0]

        return {
            "title": title,
            "infoHash": info_hash.lower(),
            "fileIndex": torrent.get("fileIdx"),
            "seeders": seeders,
            "size": (
                behavior_hints.get("videoSize")
                if isinstance(behavior_hints, dict)
                else None
            ),
            "tracker": f"Comet|{tracker}" if tracker is not None else "Comet",
            "sources": sources,
        }

    async def scrape(self, request: ScrapeRequest):
        async with self.session.get(
            f"{self.url}/stream/{request.media_type}/{request.media_id}.json",
        ) as response:
            if not is_success_status(response.status):
                raise RuntimeError(f"HTTP {response.status}")
            results = await response.json()
        if not isinstance(results, dict) or not isinstance(
            results.get("streams"), list
        ):
            raise ValueError("Comet response is invalid")
        return [self._parse_stream(torrent) for torrent in results["streams"]]
