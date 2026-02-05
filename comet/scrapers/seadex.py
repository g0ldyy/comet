from comet.core.logger import log_scraper_error
from comet.scrapers.base import BaseScraper
from comet.scrapers.models import ScrapeRequest
from comet.services.anime import anime_mapper


class SeadexScraper(BaseScraper):
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

            for item in data.get("items", []):
                for torrent in item.get("expand", {}).get("trs", []):
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

        except Exception as e:
            log_scraper_error("Seadex", self.BASE_URL, request.media_id, e)

        return torrents
