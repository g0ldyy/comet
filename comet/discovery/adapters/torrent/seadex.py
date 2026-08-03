from comet.core.provider_json import is_success_status
from comet.discovery.torrent_base import TorrentDiscoveryAdapter
from comet.discovery.torrent_models import ScrapeRequest
from comet.services.anime import anime_mapper


class SeaDexScraper(TorrentDiscoveryAdapter):
    anime_only_setting = "SEADEX_ANIME_ONLY"

    BASE_URL = "https://releases.moe"

    async def scrape(self, request: ScrapeRequest):
        if not anime_mapper.is_loaded():
            return []

        anilist_id = await anime_mapper.get_anilist_id(request.media_id)
        if not anilist_id:
            return []

        torrents = []
        async with self.session.get(
            f"{self.BASE_URL}/api/collections/entries/records?expand=trs&filter=alID={anilist_id}",
        ) as response:
            if not is_success_status(response.status):
                raise RuntimeError(f"HTTP {response.status}")
            data = await response.json()
        for item in data["items"]:
            torrent_items = item["expand"].get("trs", [])
            for torrent in torrent_items:
                info_hash = torrent.get("infoHash")
                if not info_hash or info_hash == "<redacted>":
                    continue
                for idx, file in enumerate(torrent.get("files", [])):
                    torrents.append(
                        {
                            "title": file["name"],
                            "infoHash": info_hash,
                            "fileIndex": idx,
                            "seeders": None,
                            "size": file["length"],
                            "tracker": "SeaDex",
                            "sources": [],
                        }
                    )

        return torrents
