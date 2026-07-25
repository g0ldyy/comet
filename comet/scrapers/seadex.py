from comet.core.logger import log_scraper_error
from comet.scrapers.base import BaseScraper
from comet.scrapers.models import ScrapeRequest
from comet.services.anime import anime_mapper


class SeaDexScraper(BaseScraper):
    BASE_URL = "https://releases.moe"

    async def scrape(self, request: ScrapeRequest):
        if not anime_mapper.is_loaded():
            return []

        anilist_id = await anime_mapper.get_anilist_id(request.media_id)
        if not anilist_id:
            return []

        torrents = []
        try:
            async with self.session.get(
                f"{self.BASE_URL}/api/collections/entries/records?expand=trs&filter=alID={anilist_id}",
            ) as response:
                if response.status != 200:
                    return []
                data = await response.json()

            if not isinstance(data, dict) or not isinstance(data.get("items"), list):
                return []

            for item in data["items"]:
                if not isinstance(item, dict) or not isinstance(
                    item.get("expand"), dict
                ):
                    continue
                torrent_items = item["expand"].get("trs")
                if not isinstance(torrent_items, list):
                    continue
                for torrent in torrent_items:
                    if not isinstance(torrent, dict):
                        continue
                    info_hash = torrent.get("infoHash")
                    files = torrent.get("files")
                    if (
                        not isinstance(info_hash, str)
                        or not info_hash
                        or info_hash == "<redacted>"
                        or not isinstance(files, list)
                    ):
                        continue

                    for idx, file in enumerate(files):
                        if not isinstance(file, dict):
                            continue
                        name = file.get("name")
                        if (
                            not isinstance(name, str)
                            or not name
                            or "length" not in file
                        ):
                            continue
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

        except Exception as e:
            log_scraper_error("SeaDex", self.BASE_URL, request.media_id, e)

        return torrents
