import asyncio
import time
import aiohttp
import httpx
import uuid
import orjson

from fastapi import APIRouter, Request, BackgroundTasks
from fastapi.responses import (
    RedirectResponse,
    StreamingResponse,
    FileResponse,
    Response,
)
from starlette.background import BackgroundTask
from RTN import Torrent, sort_torrents

from comet.debrid.manager import getDebrid
from comet.utils.general import (
    config_check,
    get_debrid_extension,
    get_indexer_manager,
    get_zilean,
    get_torrentio,
    get_mediafusion,
    filter,
    get_torrent_hash,
    translate,
    get_balanced_hashes,
    format_title,
    get_client_ip,
    get_aliases,
    add_torrent_to_cache,
)
from comet.utils.logger import logger
from comet.utils.models import database, rtn, settings, trackers

streams = APIRouter()


@streams.get("/stream/{type}/{id}.json")
async def stream_noconfig(request: Request, type: str, id: str):
    return {
        "streams": [
            {
                "name": "[⚠️] Comet",
                "description": f"{request.url.scheme}://{request.url.netloc}/configure",
                "url": "https://comet.fast",
            }
        ]
    }


@streams.get("/{b64config}/stream/{type}/{id}.json")
async def stream(
    request: Request,
    b64config: str,
    type: str,
    id: str,
    background_tasks: BackgroundTasks,
):
    config = config_check(b64config)
    if not config:
        return {
            "streams": [
                {
                    "name": "[⚠️] Comet",
                    "description": "Invalid Comet config.",
                    "url": "https://comet.fast",
                }
            ]
        }

    connector = aiohttp.TCPConnector(limit=0)
    async with aiohttp.ClientSession(
        connector=connector, raise_for_status=True
    ) as session:
        full_id = id
        season = None
        episode = None
        if type == "series":
            info = id.split(":")
            id = info[0]
            season = int(info[1])
            episode = int(info[2])

        year = None
        year_end = None
        try:
            kitsu = False
            if id == "kitsu":
                kitsu = True
                get_metadata = await session.get(
                    f"https://kitsu.io/api/edge/anime/{season}"
                )
                metadata = await get_metadata.json()
                name = metadata["data"]["attributes"]["canonicalTitle"]
                season = 1
            else:
                get_metadata = await session.get(
                    f"https://v3.sg.media-imdb.com/suggestion/a/{id}.json"
                )
                metadata = await get_metadata.json()
                element = metadata["d"][
                    0
                    if metadata["d"][0]["id"]
                    not in ["/imdbpicks/summer-watch-guide", "/emmys"]
                    else 1
                ]

                for element in metadata["d"]:
                    if "/" not in element["id"]:
                        break

                name = element["l"]
                year = element.get("y")

                if "yr" in element:
                    year_end = int(element["yr"].split("-")[1])
        except Exception as e:
            logger.warning(f"Exception while getting metadata for {id}: {e}")

            return {
                "streams": [
                    {
                        "name": "[⚠️] Comet",
                        "description": f"Can't get metadata for {id}",
                        "url": "https://comet.fast",
                    }
                ]
            }

        name = translate(name)
        log_name = name
        if type == "series":
            log_name = f"{name} S{season:02d}E{episode:02d}"

        if (
            settings.PROXY_DEBRID_STREAM
            and settings.PROXY_DEBRID_STREAM_PASSWORD
            == config["debridStreamProxyPassword"]
            and config["debridApiKey"] == ""
        ):
            config["debridService"] = (
                settings.PROXY_DEBRID_STREAM_DEBRID_DEFAULT_SERVICE
            )
            config["debridApiKey"] = settings.PROXY_DEBRID_STREAM_DEBRID_DEFAULT_APIKEY

        if config["debridApiKey"] == "":
            services = ["realdebrid", "alldebrid", "premiumize", "torbox", "debridlink"]
            debrid_emoji = "⬇️"
        else:
            services = [config["debridService"]]
            debrid_emoji = "⚡"

        results = []
        if (
            config["debridStreamProxyPassword"] != ""
            and settings.PROXY_DEBRID_STREAM
            and settings.PROXY_DEBRID_STREAM_PASSWORD
            != config["debridStreamProxyPassword"]
        ):
            results.append(
                {
                    "name": "[⚠️] Comet",
                    "description": "Debrid Stream Proxy Password incorrect.\nStreams will not be proxied.",
                    "url": "https://comet.fast",
                }
            )

        indexers = config["indexers"].copy()
        if settings.SCRAPE_TORRENTIO:
            indexers.append("torrentio")
        if settings.SCRAPE_MEDIAFUSION:
            indexers.append("mediafusion")
        if settings.ZILEAN_URL:
            indexers.append("dmm")
        indexers_json = orjson.dumps(indexers).decode("utf-8")

        all_sorted_ranked_files = {}
        trackers_found = (
            set()
        )  # we want to check that we have a cache for each of the user's trackers
        the_time = time.time()
        cache_ttl = settings.CACHE_TTL

        for debrid_service in services:
            cached_results = await database.fetch_all(
                f"""
                    SELECT info_hash, tracker, data
                    FROM cache
                    WHERE debridService = :debrid_service
                    AND name = :name
                    AND ((cast(:season as INTEGER) IS NULL AND season IS NULL) OR season = cast(:season as INTEGER))
                    AND ((cast(:episode as INTEGER) IS NULL AND episode IS NULL) OR episode = cast(:episode as INTEGER))
                    AND tracker IN (SELECT cast(value as TEXT) FROM {'json_array_elements_text' if settings.DATABASE_TYPE == 'postgresql' else 'json_each'}(:indexers))
                    AND timestamp + :cache_ttl >= :current_time
                """,
                {
                    "debrid_service": debrid_service,
                    "name": name,
                    "season": season,
                    "episode": episode,
                    "indexers": indexers_json,
                    "cache_ttl": cache_ttl,
                    "current_time": the_time,
                },
            )
            for result in cached_results:
                trackers_found.add(result["tracker"].lower())

                hash = result["info_hash"]
                if "searched" in hash:
                    continue

                all_sorted_ranked_files[hash] = orjson.loads(
                    result["data"]
                )

        if len(all_sorted_ranked_files) != 0 and set(indexers).issubset(trackers_found):
            debrid_extension = get_debrid_extension(
                debrid_service, config["debridApiKey"]
            )
            balanced_hashes = get_balanced_hashes(all_sorted_ranked_files, config)

            for resolution in balanced_hashes:
                for hash in balanced_hashes[resolution]:
                    data = all_sorted_ranked_files[hash]["data"]
                    the_stream = {
                        "name": f"[{debrid_extension}{debrid_emoji}] Comet {data['resolution']}",
                        "description": format_title(data, config),
                        "torrentTitle": (
                            data["torrent_title"] if "torrent_title" in data else None
                        ),
                        "torrentSize": (
                            data["torrent_size"] if "torrent_size" in data else None
                        ),
                        "behaviorHints": {
                            "filename": data["raw_title"],
                            "bingeGroup": "comet|" + hash,
                        },
                    }

                    if config["debridApiKey"] != "":
                        the_stream["url"] = (
                            f"{request.url.scheme}://{request.url.netloc}/{b64config}/playback/{hash}/{data['index']}"
                        )
                    else:
                        the_stream["infoHash"] = hash
                        index = data["index"]
                        the_stream["fileIdx"] = (
                            1 if "|" in index else int(index)
                        )  # 1 because for Premiumize it's impossible to get the file index
                        the_stream["sources"] = trackers

                    results.append(the_stream)

            logger.info(
                f"{len(all_sorted_ranked_files)} cached results found for {log_name}"
            )

            return {"streams": results}

        if config["debridApiKey"] == "":
            return {
                "streams": [
                    {
                        "name": "[⚠️] Comet",
                        "description": "No cache found for Direct Torrenting.",
                        "url": "https://comet.fast",
                    }
                ]
            }
        logger.info(f"No cache found for {log_name} with user configuration")

        debrid = getDebrid(session, config, get_client_ip(request))

        check_premium = await debrid.check_premium()
        if not check_premium:
            additional_info = ""
            if config["debridService"] == "alldebrid":
                additional_info = "\nCheck your email!"

            return {
                "streams": [
                    {
                        "name": "[⚠️] Comet",
                        "description": f"Invalid {config['debridService']} account.{additional_info}",
                        "url": "https://comet.fast",
                    }
                ]
            }

        indexer_manager_type = settings.INDEXER_MANAGER_TYPE

        search_indexer = len(config["indexers"]) != 0
        torrents = []
        tasks = []
        if indexer_manager_type and search_indexer:
            logger.info(
                f"Start of {indexer_manager_type} search for {log_name} with indexers {config['indexers']}"
            )

            search_terms = [name]
            if type == "series":
                search_terms = []
                if not kitsu:
                    search_terms.append(f"{name} S{season:02d}E{episode:02d}")
                    search_terms.append(f"{name} s{season:02d}e{episode:02d}")
                else:
                    search_terms.append(f"{name} {episode}")
            tasks.extend(
                get_indexer_manager(
                    session, indexer_manager_type, config["indexers"], term
                )
                for term in search_terms
            )
        else:
            logger.info(
                f"No indexer {'manager ' if not indexer_manager_type else ''}{'selected by user' if indexer_manager_type else 'defined'} for {log_name}"
            )

        if settings.ZILEAN_URL:
            tasks.append(get_zilean(session, name, log_name, season, episode))

        if settings.SCRAPE_TORRENTIO:
            tasks.append(get_torrentio(log_name, type, full_id))

        if settings.SCRAPE_MEDIAFUSION:
            tasks.append(get_mediafusion(log_name, type, full_id))

        search_response = await asyncio.gather(*tasks)
        for results in search_response:
            for result in results:
                torrents.append(result)

        logger.info(
            f"{len(torrents)} unique torrents found for {log_name}"
            + (
                " with "
                + ", ".join(
                    part
                    for part in [
                        indexer_manager_type,
                        "Zilean" if settings.ZILEAN_URL else None,
                        "Torrentio" if settings.SCRAPE_TORRENTIO else None,
                        "MediaFusion" if settings.SCRAPE_MEDIAFUSION else None,
                    ]
                    if part
                )
                if any(
                    [
                        indexer_manager_type,
                        settings.ZILEAN_URL,
                        settings.SCRAPE_TORRENTIO,
                        settings.SCRAPE_MEDIAFUSION,
                    ]
                )
                else ""
            )
        )

        if len(torrents) == 0:
            return {"streams": []}

        if settings.TITLE_MATCH_CHECK:
            aliases = await get_aliases(
                session, "movies" if type == "movie" else "shows", id
            )

            indexed_torrents = [(i, torrents[i]["Title"]) for i in range(len(torrents))]
            chunk_size = 50
            chunks = [
                indexed_torrents[i : i + chunk_size]
                for i in range(0, len(indexed_torrents), chunk_size)
            ]

            remove_adult_content = (
                settings.REMOVE_ADULT_CONTENT and config["removeTrash"]
            )
            tasks = []
            for chunk in chunks:
                tasks.append(
                    filter(chunk, name, year, year_end, aliases, remove_adult_content)
                )

            filtered_torrents = await asyncio.gather(*tasks)
            index_less = 0
            for result in filtered_torrents:
                for filtered in result:
                    if not filtered[1]:
                        del torrents[filtered[0] - index_less]
                        index_less += 1
                        continue

            logger.info(
                f"{len(torrents)} torrents passed title match check for {log_name}"
            )

            if len(torrents) == 0:
                return {"streams": []}

        tasks = []
        for i in range(len(torrents)):
            tasks.append(get_torrent_hash(session, (i, torrents[i])))

        torrent_hashes = await asyncio.gather(*tasks)
        index_less = 0
        for hash in torrent_hashes:
            if not hash[1]:
                del torrents[hash[0] - index_less]
                index_less += 1
                continue

            torrents[hash[0] - index_less]["InfoHash"] = hash[1]

        logger.info(f"{len(torrents)} info hashes found for {log_name}")

        if len(torrents) == 0:
            return {"streams": []}

        files = await debrid.get_files(
            list({hash[1] for hash in torrent_hashes if hash[1] is not None}),
            type,
            season,
            episode,
            kitsu,
        )

        ranked_files = set()
        torrents_by_hash = {torrent["InfoHash"]: torrent for torrent in torrents}
        for hash in files:
            try:
                ranked_file = rtn.rank(
                    torrents_by_hash[hash]["Title"],
                    hash,
                    remove_trash=False,  # user can choose if he wants to remove it
                )

                ranked_files.add(ranked_file)
            except:
                pass

        sorted_ranked_files = sort_torrents(ranked_files)

        len_sorted_ranked_files = len(sorted_ranked_files)
        logger.info(
            f"{len_sorted_ranked_files} cached files found on {config['debridService']} for {log_name}"
        )

        if len_sorted_ranked_files == 0:
            if config["debridApiKey"] == "realdebrid":
                return {
                    "streams": [
                        {
                            "name": "[⚠️] Comet",
                            "description": "RealDebrid API is unstable!",
                            "url": "https://comet.fast",
                        }
                    ]
                }

            return {"streams": []}

        sorted_ranked_files = {
            key: (value.model_dump() if isinstance(value, Torrent) else value)
            for key, value in sorted_ranked_files.items()
        }
        for hash in sorted_ranked_files:  # needed for caching
            sorted_ranked_files[hash]["data"]["title"] = files[hash]["title"]
            sorted_ranked_files[hash]["data"]["torrent_title"] = torrents_by_hash[hash][
                "Title"
            ]
            sorted_ranked_files[hash]["data"]["tracker"] = torrents_by_hash[hash][
                "Tracker"
            ]
            sorted_ranked_files[hash]["data"]["size"] = files[hash]["size"]
            torrent_size = torrents_by_hash[hash]["Size"]
            sorted_ranked_files[hash]["data"]["torrent_size"] = (
                torrent_size if torrent_size else files[hash]["size"]
            )
            sorted_ranked_files[hash]["data"]["index"] = files[hash]["index"]

        background_tasks.add_task(
            add_torrent_to_cache, config, name, season, episode, sorted_ranked_files
        )

        logger.info(f"Results have been cached for {log_name}")

        debrid_extension = get_debrid_extension(config["debridService"])

        balanced_hashes = get_balanced_hashes(sorted_ranked_files, config)

        results = []
        if (
            config["debridStreamProxyPassword"] != ""
            and settings.PROXY_DEBRID_STREAM
            and settings.PROXY_DEBRID_STREAM_PASSWORD
            != config["debridStreamProxyPassword"]
        ):
            results.append(
                {
                    "name": "[⚠️] Comet",
                    "description": "Debrid Stream Proxy Password incorrect.\nStreams will not be proxied.",
                    "url": "https://comet.fast",
                }
            )

        for resolution in balanced_hashes:
            for hash in balanced_hashes[resolution]:
                data = sorted_ranked_files[hash]["data"]
                results.append(
                    {
                        "name": f"[{debrid_extension}⚡] Comet {data['resolution']}",
                        "description": format_title(data, config),
                        "torrentTitle": data["torrent_title"],
                        "torrentSize": data["torrent_size"],
                        "url": f"{request.url.scheme}://{request.url.netloc}/{b64config}/playback/{hash}/{data['index']}",
                        "behaviorHints": {
                            "filename": data["raw_title"],
                            "bingeGroup": "comet|" + hash,
                        },
                    }
                )

        return {"streams": results}


@streams.head("/{b64config}/playback/{hash}/{index}")
async def playback(b64config: str, hash: str, index: str):
    return RedirectResponse("https://stremio.fast", status_code=302)


class CustomORJSONResponse(Response):
    media_type = "application/json"

    def render(self, content) -> bytes:
        assert orjson is not None, "orjson must be installed"
        return orjson.dumps(content, option=orjson.OPT_INDENT_2)


@streams.get("/active-connections", response_class=CustomORJSONResponse)
async def active_connections(request: Request, password: str):
    if password != settings.DASHBOARD_ADMIN_PASSWORD:
        return "Invalid Password"

    active_connections = await database.fetch_all("SELECT * FROM active_connections")

    return {
        "total_connections": len(active_connections),
        "active_connections": active_connections,
    }


@streams.get("/{b64config}/playback/{hash}/{index}")
async def playback(request: Request, b64config: str, hash: str, index: str):
    config = config_check(b64config)
    if not config:
        return FileResponse("comet/assets/invalidconfig.mp4")

    if (
        settings.PROXY_DEBRID_STREAM
        and settings.PROXY_DEBRID_STREAM_PASSWORD == config["debridStreamProxyPassword"]
        and config["debridApiKey"] == ""
    ):
        config["debridService"] = settings.PROXY_DEBRID_STREAM_DEBRID_DEFAULT_SERVICE
        config["debridApiKey"] = settings.PROXY_DEBRID_STREAM_DEBRID_DEFAULT_APIKEY

    async with aiohttp.ClientSession(raise_for_status=True) as session:
        # Check for cached download link
        cached_link = await database.fetch_one(
            f"SELECT link, timestamp FROM download_links WHERE debrid_key = '{config['debridApiKey']}' AND hash = '{hash}' AND file_index = '{index}'"
        )

        current_time = time.time()
        download_link = None
        if cached_link:
            link = cached_link["link"]
            timestamp = cached_link["timestamp"]

            if current_time - timestamp < 3600:
                download_link = link
            else:
                # Cache expired, remove old entry
                await database.execute(
                    f"DELETE FROM download_links WHERE debrid_key = '{config['debridApiKey']}' AND hash = '{hash}' AND file_index = '{index}'"
                )

        ip = get_client_ip(request)

        if not download_link:
            debrid = getDebrid(
                session,
                config,
                ip
                if (
                    not settings.PROXY_DEBRID_STREAM
                    or settings.PROXY_DEBRID_STREAM_PASSWORD
                    != config["debridStreamProxyPassword"]
                )
                else "",
            )
            download_link = await debrid.generate_download_link(hash, index)
            if not download_link:
                return FileResponse("comet/assets/uncached.mp4")

            # Cache the new download link
            await database.execute(
                f"INSERT {'OR IGNORE ' if settings.DATABASE_TYPE == 'sqlite' else ''}INTO download_links (debrid_key, hash, file_index, link, timestamp) VALUES (:debrid_key, :hash, :file_index, :link, :timestamp){' ON CONFLICT DO NOTHING' if settings.DATABASE_TYPE == 'postgresql' else ''}",
                {
                    "debrid_key": config["debridApiKey"],
                    "hash": hash,
                    "file_index": index,
                    "link": download_link,
                    "timestamp": current_time,
                },
            )

        if (
            settings.PROXY_DEBRID_STREAM
            and settings.PROXY_DEBRID_STREAM_PASSWORD
            == config["debridStreamProxyPassword"]
        ):
            if settings.PROXY_DEBRID_STREAM_MAX_CONNECTIONS != -1:
                active_ip_connections = await database.fetch_all(
                    "SELECT ip, COUNT(*) as connections FROM active_connections GROUP BY ip"
                )
                if any(
                    connection["ip"] == ip
                    and connection["connections"]
                    >= settings.PROXY_DEBRID_STREAM_MAX_CONNECTIONS
                    for connection in active_ip_connections
                ):
                    return FileResponse("comet/assets/proxylimit.mp4")

            proxy = None

            class Streamer:
                def __init__(self, id: str):
                    self.id = id

                    self.client = httpx.AsyncClient(proxy=proxy, timeout=None)
                    self.response = None

                async def stream_content(self, headers: dict):
                    async with self.client.stream(
                        "GET", download_link, headers=headers
                    ) as self.response:
                        async for chunk in self.response.aiter_raw():
                            yield chunk

                async def close(self):
                    await database.execute(
                        f"DELETE FROM active_connections WHERE id = '{self.id}'"
                    )

                    if self.response is not None:
                        await self.response.aclose()
                    if self.client is not None:
                        await self.client.aclose()

            range_header = request.headers.get("range", "bytes=0-")

            try:
                response = await session.head(
                    download_link, headers={"Range": range_header}
                )
            except aiohttp.ClientResponseError as e:
                if e.status == 503 and config["debridService"] == "alldebrid":
                        proxy = (
                            settings.DEBRID_PROXY_URL
                        ) # proxy is not needed to proxy realdebrid stream

                        response = await session.head(
                            download_link, headers={"Range": range_header}, proxy=proxy
                        )
                else:
                    raise

            if response.status == 206 or (config["debridService"] == "torbox" and response.status == 200):
                id = str(uuid.uuid4())
                await database.execute(
                    f"INSERT  {'OR IGNORE ' if settings.DATABASE_TYPE == 'sqlite' else ''}INTO active_connections (id, ip, content, timestamp) VALUES (:id, :ip, :content, :timestamp){' ON CONFLICT DO NOTHING' if settings.DATABASE_TYPE == 'postgresql' else ''}",
                    {
                        "id": id,
                        "ip": ip,
                        "content": str(response.url),
                        "timestamp": current_time,
                    },
                )

                streamer = Streamer(id)

                return StreamingResponse(
                    streamer.stream_content({"Range": range_header}),
                    status_code=206,
                    headers={
                        "Content-Range": response.headers.get("Content-Range", range_header),
                        "Content-Length": response.headers.get("Content-Length"),
                        "Accept-Ranges": "bytes",
                    },
                    background=BackgroundTask(streamer.close),
                )

            return FileResponse("comet/assets/uncached.mp4")

        return RedirectResponse(download_link, status_code=302)
