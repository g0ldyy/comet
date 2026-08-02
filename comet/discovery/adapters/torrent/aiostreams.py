import base64
from functools import cache

from comet.core.provider_json import is_success_status
from comet.discovery.torrent_base import TorrentDiscoveryAdapter
from comet.discovery.torrent_models import ScrapeRequest


@cache
def _authorization(credentials: str) -> str:
    encoded = base64.b64encode(credentials.encode()).decode()
    return f"Basic {encoded}"


class AiostreamsScraper(TorrentDiscoveryAdapter):
    url_setting = "AIOSTREAMS_URL"
    credential_setting = "AIOSTREAMS_USER_UUID_AND_PASSWORD"

    def __init__(
        self,
        manager,
        session,
        url: str,
        credentials: str | None = None,
    ):
        super().__init__(manager, session, url)
        self.headers = (
            {}
            if credentials is None
            else {"Authorization": _authorization(credentials)}
        )

    @staticmethod
    def _parse_stream(torrent):
        if not isinstance(torrent, dict):
            raise ValueError("AIOStreams result is invalid")

        title = torrent.get("filename")
        info_hash = torrent.get("infoHash")
        sources = torrent.get("sources", [])
        if (
            not isinstance(title, str)
            or not title
            or not isinstance(info_hash, str)
            or not info_hash
            or not isinstance(sources, list)
        ):
            raise ValueError("AIOStreams result is invalid")

        tracker = "AIOStreams"
        indexer = torrent.get("indexer")
        if isinstance(indexer, str) and indexer:
            tracker += f"|{indexer}"

        return {
            "title": title,
            "infoHash": info_hash,
            "fileIndex": torrent.get("fileIdx"),
            "seeders": torrent.get("seeders"),
            "size": torrent.get("size"),
            "tracker": tracker,
            "sources": sources,
        }

    async def scrape(self, request: ScrapeRequest):
        async with self.session.get(
            f"{self.url}/api/v1/search",
            params={"type": request.media_type, "id": request.media_id},
            headers=self.headers,
        ) as response:
            if not is_success_status(response.status):
                raise RuntimeError(f"HTTP {response.status}")
            results = await response.json()
        if not isinstance(results, dict) or not isinstance(results.get("data"), dict):
            raise ValueError("AIOStreams response is invalid")
        streams = results["data"].get("results")
        if not isinstance(streams, list):
            raise ValueError("AIOStreams response is invalid")
        return [self._parse_stream(torrent) for torrent in streams]
