import re

from comet.core.logger import log_scraper_error
from comet.metadata.tmdb import TMDBApi
from comet.scrapers.base import BaseScraper
from comet.scrapers.models import ScrapeRequest
from comet.utils.formatting import size_to_bytes
from comet.services.torrent_manager import (_extract_info_hash_from_magnet, extract_trackers_from_magnet, extract_title_from_magnet)
from comet.utils.parsing import parse_media_id


def slugify(value: str) -> str:
    """
    Convert a string to a slug: lowercase, hyphens, alphanumeric only.
    Example: 'Avengers: Endgame' -> 'avengers-endgame'
    """
    import unicodedata
    value = unicodedata.normalize('NFKD', value)
    value = value.encode('ascii', 'ignore').decode('ascii')
    value = value.lower()
    value = re.sub(r"[^a-z0-9\s-]", "", value)
    value = re.sub(r"[\s_-]+", "-", value)
    value = value.strip("-")
    return value

class PelisPandaScraper(BaseScraper):
    BASE_URL = "https://pelispanda.org"
    impersonate = "chrome"

    def __init__(self, manager, session):
        super().__init__(manager, session)

    async def scrape(self, request: ScrapeRequest):
        torrents = []
        try:
            tmdb = TMDBApi(self.session)

            if request.media_type == "series":
                imdb_id, season, episode = parse_media_id("series", request.media_id)
            else:
                imdb_id = request.media_id
                season = episode = None

            if not imdb_id:
                return []

            # Get TMDB ID
            tmdb_id = await tmdb.get_tmdb_id_from_imdb(imdb_id)
            if not tmdb_id:
                return []

            # Get metadata and translated titles
            metadata = await tmdb.get_metadata_by_tmdb_id(tmdb_id, request.media_type)
            translated_titles = await tmdb.get_translations_by_tmdb_id(tmdb_id, request.media_type)
            
            if not metadata or not translated_titles:
                return []

            # Build slug from title
            slug_title = (
                translated_titles.get("es-MX")
                or metadata.get("original_title")
                or metadata.get("original_name")
            )
            if not slug_title:
                return []

            slug = slugify(slug_title)

            # Choose endpoint based on media type
            endpoint_type = "serie" if request.media_type == "series" else "movie"
            async with self.session.get(
                f"{self.BASE_URL}/wp-json/wpreact/v1/{endpoint_type}/{slug}",
            ) as response:
                results = await response.json()

            # If TV show, make extra request to get downloads
            if request.media_type == "series":
                async with self.session.get(
                    f"{self.BASE_URL}/wp-json/wpreact/v1/serie/{slug}/related",
                ) as related_response:
                    related_results = await related_response.json()
                if related_results and "downloads" in related_results:
                    if results and "downloads" in results:
                        results["downloads"].extend(related_results["downloads"])
                    else:
                        results = related_results

            if not results or "downloads" not in results:
                return []

            downloads = results["downloads"]

            # Filter for matching season and episode if TV show
            if request.media_type == "series" and season and episode:
                downloads = [
                    d for d in downloads
                    if str(d.get("season")) == str(season) and str(d.get("episode")) == str(episode)
                ]

            for torrent in downloads:
                magnet_url = torrent.get("download_link")
                if not magnet_url or not magnet_url.startswith("magnet:"):
                    continue

                # Extract title from magnet link 
                title = extract_title_from_magnet(magnet_url)

                torrent["title"] = title
                torrent["infoHash"] = _extract_info_hash_from_magnet(magnet_url)
                torrent["sources"] = extract_trackers_from_magnet(magnet_url)
                torrent["size"] = size_to_bytes(torrent.get("size", "0B"))
                torrent["tracker"] = "Pelispanda"
                torrent["fileIndex"] = None  
                torrent["seeders"] = None

                torrents.append(torrent)
        except Exception as e:
            log_scraper_error("Pelispanda", self.url, request.media_id, e)

        return torrents




