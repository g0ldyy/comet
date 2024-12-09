import aiohttp
import time
import asyncio

from fastapi import APIRouter, Request, BackgroundTasks

from comet.utils.models import settings, database
from comet.metadata.manager import MetadataScraper
from comet.scrapers.manager import TorrentManager
from comet.utils.general import config_check, format_title, get_client_ip
from comet.debrid.manager import (
    get_debrid_extension,
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

        debrid_service = config["debridService"]
        torrent_manager = TorrentManager(
            debrid_service,
            config["debridApiKey"],
            get_client_ip(request),
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

        await torrent_manager.get_cached_torrents()
        if (
            len(torrent_manager.torrents) == 0
        ):  # no torrent, we search for an ongoing search before starting a new one
            cached = False
            ongoing_search = await database.fetch_one(
                "SELECT * FROM ongoing_searches WHERE media_id = :media_id AND timestamp + 120 >= :current_time",
                {"media_id": media_id, "current_time": time.time()},
            )
            if ongoing_search:
                while ongoing_search:
                    await asyncio.sleep(10)
                    ongoing_search = await database.fetch_one(
                        "SELECT * FROM ongoing_searches WHERE media_id = :media_id AND timestamp + 120 >= :current_time",
                        {"media_id": media_id, "current_time": time.time()},
                    )

                await (
                    torrent_manager.get_cached_torrents()
                )  # we verify that no cache is available
                if len(torrent_manager.torrents) != 0:
                    cached = True

            if not cached:
                await database.execute(
                    f"INSERT {'OR IGNORE ' if settings.DATABASE_TYPE == 'sqlite' else ''}INTO ongoing_searches VALUES (:media_id, :timestamp){' ON CONFLICT DO NOTHING' if settings.DATABASE_TYPE == 'postgresql' else ''}",
                    {"media_id": media_id, "timestamp": time.time()},
                )
                background_tasks.add_task(remove_ongoing_search_from_database, media_id)

                await torrent_manager.scrape_torrents(session)


        await torrent_manager.get_cached_availability()                
        if debrid_service != "torrent":
            one_cached = False
            for info_hash, torrent in torrent_manager.torrents.items():
                if torrent["cached"]:
                    one_cached = True
            if not one_cached:
                await torrent_manager.get_and_cache_debrid_availability(session)

        torrent_manager.rank_torrents(
            config["rtnSettings"],
            config["rtnRanking"],
            config["maxResultsPerResolution"],
            config["maxSize"],
        )

        debrid_extension = get_debrid_extension(debrid_service)
        torrents = torrent_manager.torrents

        results = []
        for info_hash, torrent in torrent_manager.ranked_torrents.items():
            torrent_data = torrents[info_hash]
            rtn_data = torrent_data["parsed"] if torrent_data["cached"] else torrent.data

            # here we put the config check with rtn_data

            debrid_emoji = (
                "🧲"
                if debrid_service == "torrent"
                else ("⚡" if torrent_data["cached"] else "⬇️")
            )

            the_stream = {
                "name": f"[{debrid_extension}{debrid_emoji}] Comet {rtn_data.resolution}",
                "description": format_title(
                    rtn_data,
                    torrent_data["title"],
                    torrent_data["seeders"],
                    torrent_data["size"],
                    torrent_data["tracker"],
                    config["resultFormat"],
                ),
                "behaviorHints": {
                    "bingeGroup": "comet|" + info_hash,
                    "videoSize": torrent_data["size"],
                    "filename": rtn_data.raw_title,
                },
            }

            if debrid_service == "torrent":
                the_stream["infoHash"] = info_hash
                the_stream["fileIdx"] = torrent_data["fileIndex"]
                the_stream["sources"] = torrent_data["sources"]
            else:
                the_stream["url"] = (
                    f"{request.url.scheme}://{request.url.netloc}/{b64config}/playback/{info_hash}/{torrent_data['fileIndex']}"
                )

            results.append(the_stream)

        return {"streams": results}
