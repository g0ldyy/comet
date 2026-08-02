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
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            raise ValueError("SeaDex response is invalid")
        for item in data["items"]:
            if not isinstance(item, dict) or not isinstance(item.get("expand"), dict):
                raise ValueError("SeaDex result is invalid")
            torrent_items = item["expand"].get("trs", [])
            if not isinstance(torrent_items, list):
                raise ValueError("SeaDex result is invalid")
            for torrent in torrent_items:
                if not isinstance(torrent, dict):
                    raise ValueError("SeaDex result is invalid")
                info_hash = torrent.get("infoHash")
                if info_hash == "<redacted>":
                    continue
                files = torrent.get("files")
                if (
                    not isinstance(info_hash, str)
                    or not info_hash
                    or not isinstance(files, list)
                ):
                    raise ValueError("SeaDex result is invalid")
                for idx, file in enumerate(files):
                    if not isinstance(file, dict):
                        raise ValueError("SeaDex result is invalid")
                    name = file.get("name")
                    if not isinstance(name, str) or not name or "length" not in file:
                        raise ValueError("SeaDex result is invalid")
                    torrents.append(
                        {
                            "title": name,
                            "infoHash": info_hash,
                            "fileIndex": idx,
                            "seeders": None,
                            "size": file["length"],
                            "tracker": "SeaDex",
                            "sources": [],
                        }
                    )

        return torrents
