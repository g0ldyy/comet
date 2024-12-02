import aiohttp
import time
import asyncio
import orjson

from fastapi import APIRouter, Request, BackgroundTasks

from comet.utils.models import settings, database
from comet.metadata.manager import MetadataScraper
from comet.scrapers.manager import TorrentScraper
from comet.utils.general import (
    config_check,
)

streams = APIRouter()


async def remove_ongoing_search_from_database(media_id: str):
    await database.execute(
        "DELETE FROM ongoing_searches WHERE media_id = :media_id",
        {"media_id": media_id},
    )


@streams.get("/stream/{media_type}/{media_id}.json")
@streams.get("/{b64config}/stream/{media_type}/{media_id}.json")
async def stream(
    request: Request,
    media_type: str,
    media_id: str,
    background_tasks: BackgroundTasks,
    b64config: str = None,
):
    config = config_check(b64config)

    connector = aiohttp.TCPConnector(limit=0)
    async with aiohttp.ClientSession(
        connector=connector, raise_for_status=True
    ) as session:
        metadata, aliases = await MetadataScraper(session).fetch_metadata_and_aliases(
            media_type, media_id
        )
        if metadata is None:
            return {
                "streams": [
                    {
                        "name": "[⚠️] Comet",
                        "description": "Unable to get metadata.",
                        "url": "https://comet.fast",
                    }
                ]
            }

        title = metadata["title"]
        year = metadata["year"]
        year_end = metadata["year_end"]
        season = metadata["season"]
        episode = metadata["episode"]

        log_title = f"({media_id}) {title}"
        if media_type == "series":
            log_title += f" S{season:02d}E{episode:02d}"

        torrent_scraper = TorrentScraper(
            media_type,
            media_id,
            title,
            year,
            year_end,
            season,
            episode,
            aliases,
            settings.REMOVE_ADULT_CONTENT and config["removeTrash"],
        )

        cached = True
        await torrent_scraper.get_cached_torrents()
        if (
            len(torrent_scraper.torrents) == 0
        ):  # no torrent, we search for an ongoing search before starting a new one
            cached = False
            ongoing_search = await database.fetch_one(
                "SELECT * FROM ongoing_searches WHERE media_id = :media_id AND timestamp + 120 >= :current_time",
                {"media_id": media_id, "current_time": time.time()},
            )
            if ongoing_search:
                while ongoing_search:
                    await asyncio.sleep(5)
                    ongoing_search = await database.fetch_one(
                        "SELECT * FROM ongoing_searches WHERE media_id = :media_id AND timestamp + 120 >= :current_time",
                        {"media_id": media_id, "current_time": time.time()},
                    )

            await database.execute(
                f"INSERT {'OR IGNORE ' if settings.DATABASE_TYPE == 'sqlite' else ''}INTO ongoing_searches (media_id, timestamp) VALUES (:media_id, :timestamp){' ON CONFLICT DO NOTHING' if settings.DATABASE_TYPE == 'postgresql' else ''}",
                {"media_id": media_id, "timestamp": time.time()},
            )

            background_tasks.add_task(remove_ongoing_search_from_database, media_id)

            await torrent_scraper.scrape_torrents(session)

        for torrent in torrent_scraper.torrents:
            torrent = orjson.loads(torrent) if cached else torrent
            print(torrent["title"])
        print(len(torrent_scraper.torrents))
