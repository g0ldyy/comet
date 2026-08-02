from comet.core.provider_json import is_success_status
from comet.discovery.torrent_base import TorrentDiscoveryAdapter
from comet.discovery.torrent_models import ScrapeRequest


class PeerflixScraper(TorrentDiscoveryAdapter):
    BASE_URL = "https://peerflix.mov"

    @staticmethod
    def _parse_stream(stream):
        if not isinstance(stream, dict):
            raise ValueError("Peerflix result is invalid")

        description = stream.get("description")
        info_hash = stream.get("infoHash")
        sources = stream.get("sources", [])
        if (
            not isinstance(description, str)
            or not description
            or not isinstance(info_hash, str)
            or not info_hash
            or not isinstance(sources, list)
        ):
            raise ValueError("Peerflix result is invalid")

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
        if not isinstance(results, dict) or not isinstance(
            results.get("streams"), list
        ):
            raise ValueError("Peerflix response is invalid")
        return [self._parse_stream(stream) for stream in results["streams"]]
