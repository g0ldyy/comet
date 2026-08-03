from comet.core.provider_json import is_success_status
from comet.discovery.torrent_base import TorrentDiscoveryAdapter
from comet.discovery.torrent_models import ScrapeRequest


class PeerflixScraper(TorrentDiscoveryAdapter):
    BASE_URL = "https://peerflix.mov"

    @staticmethod
    def _parse_stream(stream):
        description = stream["description"]
        info_hash = stream["infoHash"]
        sources = stream.get("sources", [])

        parts = description.split("🌐")
        tracker = parts[1] if len(parts) > 1 else None
        return {
            "title": description.split("\n")[0],
            "infoHash": info_hash.lower(),
            "fileIndex": stream.get("fileIdx"),
            "seeders": stream.get("seed"),
            "size": stream.get("sizebytes"),
            "tracker": f"Peerflix|{tracker}"
            if tracker and tracker != "Peerflix"
            else "Peerflix",
            "sources": sources,
        }

    async def scrape(self, request: ScrapeRequest):
        async with self.session.get(
            f"{self.BASE_URL}/stream/{request.media_type}/{request.media_id}.json",
        ) as response:
            if not is_success_status(response.status):
                raise RuntimeError(f"HTTP {response.status}")
            results = await response.json()
        return [self._parse_stream(stream) for stream in results["streams"]]
