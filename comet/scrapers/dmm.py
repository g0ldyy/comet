from comet.core.logger import logger
from comet.core.models import database, settings
from comet.scrapers.base import BaseScraper
from comet.scrapers.models import ScrapeRequest


class DMMScraper(BaseScraper):
    def __init__(self, manager, session):
        super().__init__(manager, session, "DMM")

    async def scrape(self, request: ScrapeRequest):
        if not settings.DMM_INGEST_ENABLED:
            return []

        torrents = []
        try:
            query = """
                SELECT * FROM dmm_entries 
                WHERE parsed_title LIKE :title_query
            """
            params = {"title_query": f"%{request.title}%"}

            if request.year:
                query += " AND (parsed_year = :year OR parsed_year IS NULL)"
                params["year"] = request.year

            entries = await database.fetch_all(query, params)

            for entry in entries:
                torrents.append(
                    {
                        "title": entry["filename"],
                        "infoHash": entry["info_hash"],
                        "fileIndex": None,
                        "seeders": None,
                        "size": entry["size"],
                        "tracker": "DMM",
                        "sources": [],
                    }
                )
        except Exception as e:
            logger.warning(
                f"Exception while getting torrents for {request.title} with DMM: {e}"
            )

        return torrents
