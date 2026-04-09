from comet.core.logger import log_scraper_error
from comet.scrapers.base import BaseScraper
from comet.scrapers.models import ScrapeRequest


PEERFLIX_FLAG_LANGUAGES = {
    "🇪🇸": "Spanish",
}

PEERFLIX_EXISTING_LANGUAGE_MARKERS = {
    "Spanish": ("spanish", "espanol", "español", "castellano", "[esp]", " esp "),
}


def _apply_name_language(title: str, name: str | None) -> str:
    if not isinstance(name, str):
        return title

    title_lower = title.lower()
    for flag, language in PEERFLIX_FLAG_LANGUAGES.items():
        markers = PEERFLIX_EXISTING_LANGUAGE_MARKERS.get(language, (language.lower(),))
        if flag in name and not any(marker in title_lower for marker in markers):
            return f"{title} [{language}]"

    return title


class PeerflixScraper(BaseScraper):
    BASE_URL = "https://peerflix.mov"

    async def scrape(self, request: ScrapeRequest):
        torrents = []
        try:
            async with self.session.get(
                f"{self.BASE_URL}/stream/{request.media_type}/{request.media_id}.json",
            ) as response:
                if response.status == 404:
                    return []
                results = await response.json()

            for stream in results["streams"]:
                description = stream["description"]
                parts = description.split("🌐")
                tracker = parts[1] if len(parts) > 1 else None

                torrents.append(
                    {
                        "title": _apply_name_language(
                            description.split("\n")[0], stream.get("name")
                        ),
                        "infoHash": stream["infoHash"].lower(),
                        "fileIndex": stream["fileIdx"],
                        "seeders": stream.get("seed"),
                        "size": stream.get("sizebytes"),
                        "tracker": f"Peerflix|{tracker}"
                        if tracker and tracker != "Peerflix"
                        else "Peerflix",
                        "sources": stream["sources"],
                    }
                )
        except Exception as e:
            log_scraper_error("Peerflix", self.BASE_URL, request.media_id, e)

        return torrents
