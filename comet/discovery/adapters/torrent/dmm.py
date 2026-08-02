from comet.core.models import database, settings
from comet.discovery.torrent_base import TorrentDiscoveryAdapter
from comet.discovery.torrent_models import ScrapeRequest


class DMMScraper(TorrentDiscoveryAdapter):
    def __init__(self, manager, session):
        super().__init__(manager, session, "DMM")

    async def scrape(self, request: ScrapeRequest):
        if not settings.DMM_INGEST_ENABLED:
            return []

        torrents = []
        title_clauses = []
        params = {}
        for index, title in enumerate(request.query_titles):
            key = f"title_query_{index}"
            title_clauses.append(f"parsed_title LIKE :{key}")
            params[key] = f"%{title}%"

        query = f"""
            SELECT info_hash, filename, size
            FROM dmm_entries
            WHERE ({" OR ".join(title_clauses)})
        """

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

        return torrents
