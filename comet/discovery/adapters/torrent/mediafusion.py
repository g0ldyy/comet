import base64
import json
from functools import cache

from comet.core.models import settings
from comet.core.provider_json import is_success_status
from comet.discovery.torrent_base import TorrentDiscoveryAdapter
from comet.discovery.torrent_models import ScrapeRequest


@cache
def _encoded_user_data(api_password: str, live_search: bool | None) -> str:
    config = {
        "ap": api_password,
        "nf": ["Disable"],
        "cf": ["Disable"],
        "lss": live_search,
    }
    return base64.urlsafe_b64encode(json.dumps(config).encode()).decode()


class MediaFusionScraper(TorrentDiscoveryAdapter):
    url_setting = "MEDIAFUSION_URL"
    credential_setting = "MEDIAFUSION_API_PASSWORD"

    def __init__(
        self,
        manager,
        session,
        url: str,
        password: str | None = None,
    ):
        super().__init__(manager, session, url)
        self.headers = {
            "encoded_user_data": _encoded_user_data(
                "" if password is None else password,
                settings.MEDIAFUSION_LIVE_SEARCH,
            )
        }

    @staticmethod
    def _parse_stream(torrent):
        if not isinstance(torrent, dict):
            raise ValueError("MediaFusion result is invalid")

        title_full = torrent.get("description")
        info_hash = torrent.get("infoHash")
        behavior_hints = torrent.get("behaviorHints")
        sources = torrent.get("sources", [])
        if (
            not isinstance(title_full, str)
            or not title_full
            or not isinstance(info_hash, str)
            or not info_hash
            or not isinstance(sources, list)
        ):
            raise ValueError("MediaFusion result is invalid")

        lines = title_full.split("\n")
        title = lines[0].removeprefix("📂 ").removesuffix("/")
        if not title:
            raise ValueError("MediaFusion result is invalid")
        seeders = None
        if len(lines) > 1 and "👤 " in lines[1]:
            try:
                seeders = int(lines[1].split("👤 ", 1)[1])
            except (IndexError, ValueError):
                pass

        tracker = lines[-1].split("🔗 ", 1)[1] if "🔗 " in lines[-1] else "MediaFusion"
        return {
            "title": title,
            "infoHash": info_hash.lower(),
            "fileIndex": torrent.get("fileIdx"),
            "seeders": seeders,
            # This is the selected video size, not the pack size.
            "size": (
                behavior_hints.get("videoSize")
                if isinstance(behavior_hints, dict)
                else None
            ),
            "tracker": f"MediaFusion|{tracker}",
            "sources": sources,
        }

    async def scrape(self, request: ScrapeRequest):
        async with self.session.get(
            f"{self.url}/stream/{request.media_type}/{request.media_id}.json",
            headers=self.headers,
        ) as response:
            if not is_success_status(response.status):
                raise RuntimeError(f"HTTP {response.status}")
            results = await response.json()
        if not isinstance(results, dict) or not isinstance(
            results.get("streams"), list
        ):
            raise ValueError("MediaFusion response is invalid")
        return [self._parse_stream(torrent) for torrent in results["streams"]]
