import asyncio
import hashlib
import json
import time
import aiohttp
import httpx

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, StreamingResponse, FileResponse
from starlette.background import BackgroundTask
from RTN import Torrent, sort_torrents

from comet.debrid.manager import getDebrid
from comet.utils.general import (
    bytes_to_size,
    config_check,
    get_debrid_extension,
    get_indexer_manager,
    get_zilean,
    get_torrentio,
    filter,
    get_torrent_hash,
    translate,
    get_balanced_hashes,
)
from comet.utils.logger import logger
from comet.utils.models import database, rtn, settings

streams = APIRouter()


@streams.get("/stream/{type}/{id}.json")
@streams.get("/{b64config}/stream/{type}/{id}.json")
async def stream(request: Request, b64config: str, type: str, id: str):
    config = config_check(b64config)
    if not config:
        return {
            "streams": [
                {
                    "name": "[⚠️] Comet",
                    "title": "Invalid Comet config.",
                    "url": "https://comet.fast",
                }
            ]
        }

    connector = aiohttp.TCPConnector(limit=0)
    async with aiohttp.ClientSession(connector=connector) as session:
        full_id = id
        season = None
        episode = None
        if type == "series":
            info = id.split(":")
            full_id = id
            id = info[0]
            season = int(info[1])
            episode = int(info[2])

        try:
            kitsu = False
            if id == "kitsu":
                kitsu = True
                get_metadata = await session.get(
                    f"https://kitsu.io/api/edge/anime/{season}"
                )
                get_metadata = await get_metadata.json()
                name = get_metadata["data"]["attributes"]["canonicalTitle"]
                season = 1
            else:
                get_metadata = await session.get(
                    f"https://v3.sg.media-imdb.com/suggestion/a/{id}.json"
                )
                metadata = await get_metadata.json()
                name = metadata["d"][0 if metadata["d"][0]["l"] != "Summer Watch Guide" else 1]["l"]
        except Exception as e:
            logger.warning(f"Exception while getting metadata for {id}: {e}")

            return {
                "streams": [
                    {
                        "name": "[⚠️] Comet",
                        "title": f"Can't get metadata for {id}",
                        "url": "https://comet.fast",
                    }
                ]
            }

        name = translate(name)
        log_name = name
        if type == "series":
            log_name = f"{name} S0{season}E0{episode}"

        cache_key = hashlib.md5(
            json.dumps(
                {
                    "debridService": config["debridService"],
                    "name": name,
                    "season": season,
                    "episode": episode,
                    "indexers": config["indexers"],
                }
            ).encode("utf-8")
        ).hexdigest()
        cached = await database.fetch_one(
            f"SELECT EXISTS (SELECT 1 FROM cache WHERE cacheKey = '{cache_key}')"
        )
        if cached[0] != 0:
            logger.info(f"Cache found for {log_name}")

            timestamp = await database.fetch_one(
                f"SELECT timestamp FROM cache WHERE cacheKey = '{cache_key}'"
            )
            if timestamp[0] + settings.CACHE_TTL < time.time():
                await database.execute(
                    f"DELETE FROM cache WHERE cacheKey = '{cache_key}'"
                )

                logger.info(f"Cache expired for {log_name}")
            else:
                sorted_ranked_files = await database.fetch_one(
                    f"SELECT results FROM cache WHERE cacheKey = '{cache_key}'"
                )
                sorted_ranked_files = json.loads(sorted_ranked_files[0])

                debrid_extension = get_debrid_extension(config["debridService"])

                balanced_hashes = await get_balanced_hashes(sorted_ranked_files, config)

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
                            "title": "Debrid Stream Proxy Password incorrect.\nStreams will not be proxied.",
                            "url": "https://comet.fast",
                        }
                    )

                for (
                    hash,
                    hash_data,
                ) in sorted_ranked_files.items():
                    for resolution, hash_list in balanced_hashes.items():
                        if hash in hash_list:
                            data = hash_data["data"]
                            results.append(
                                {
                                    "name": f"[{debrid_extension}⚡] Comet {data['resolution'][0] if data['resolution'] != [] else 'Unknown'}",
                                    "title": f"{data['title']}\n💾 {bytes_to_size(data['size'])} 🔎 {data['tracker'] if 'tracker' in data else '?'}",
                                    "torrentTitle": data["torrent_title"]
                                    if "torrent_title" in data
                                    else None,
                                    "torrentSize": data["torrent_size"]
                                    if "torrent_size" in data
                                    else None,
                                    "url": f"{request.url.scheme}://{request.url.netloc}/{b64config}/playback/{hash}/{data['index']}",
                                }
                            )

                            continue

                return {"streams": results}
        else:
            logger.info(f"No cache found for {log_name} with user configuration")

        debrid = getDebrid(session, config)

        check_premium = await debrid.check_premium()
        if not check_premium:
            additional_info = ""
            if config["debridService"] == "alldebrid":
                additional_info = "\nCheck your email!"

            return {
                "streams": [
                    {
                        "name": "[⚠️] Comet",
                        "title": f"Invalid {config['debridService']} account.{additional_info}",
                        "url": "https://comet.fast",
                    }
                ]
            }

        indexer_manager_type = settings.INDEXER_MANAGER_TYPE

        search_indexer = len(config["indexers"]) != 0
        torrents = []
        tasks = []
        if search_indexer:
            logger.info(
                f"Start of {indexer_manager_type} search for {log_name} with indexers {config['indexers']}"
            )

            search_terms = [name]
            if type == "series":
                if not kitsu:
                    search_terms.append(f"{name} S0{season}E0{episode}")
                else:
                    search_terms.append(f"{name} {episode}")
            tasks.extend(
                get_indexer_manager(
                    session, indexer_manager_type, config["indexers"], term
                )
                for term in search_terms
            )
        else:
            logger.info(f"No indexer selected by user for {log_name}")

        if settings.ZILEAN_URL:
            tasks.append(get_zilean(session, name, log_name, season, episode))

        if settings.SCRAPE_TORRENTIO:
            tasks.append(get_torrentio(log_name, type, full_id))

        search_response = await asyncio.gather(*tasks)
        for results in search_response:
            for result in results:
                torrents.append(result)

        logger.info(
            f"{len(torrents)} torrents found for {log_name} with {indexer_manager_type}{' and Zilean' if settings.ZILEAN_URL else ''}{' and Torrentio' if settings.SCRAPE_TORRENTIO else ''}"
        )

        if len(torrents) == 0:
            return {"streams": []}

        if settings.TITLE_MATCH_CHECK:
            indexed_torrents = [(i, torrents[i]["Title"]) for i in range(len(torrents))]
            chunk_size = 50
            chunks = [
                indexed_torrents[i : i + chunk_size]
                for i in range(0, len(indexed_torrents), chunk_size)
            ]

            tasks = []
            for chunk in chunks:
                tasks.append(filter(chunk, name))

            filtered_torrents = await asyncio.gather(*tasks)
            index_less = 0
            for result in filtered_torrents:
                for filtered in result:
                    if not filtered[1]:
                        del torrents[filtered[0] - index_less]
                        index_less += 1
                        continue

            logger.info(f"{len(torrents)} torrents passed title match check for {log_name}")

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
            [hash[1] for hash in torrent_hashes if hash[1] is not None],
            type,
            season,
            episode,
            kitsu,
        )

        ranked_files = set()
        for hash in files:
            # try:
            ranked_file = rtn.rank(
                files[hash]["title"],
                hash,  # , correct_title=name, remove_trash=True
            )
            # except:
            #     continue

            ranked_files.add(ranked_file)

        sorted_ranked_files = sort_torrents(ranked_files)

        logger.info(
            f"{len(sorted_ranked_files)} cached files found on {config['debridService']} for {log_name}"
        )

        if len(sorted_ranked_files) == 0:
            return {"streams": []}

        sorted_ranked_files = {
            key: (value.model_dump() if isinstance(value, Torrent) else value)
            for key, value in sorted_ranked_files.items()
        }
        torrents_by_hash = {torrent["InfoHash"]: torrent for torrent in torrents}
        for hash in sorted_ranked_files:  # needed for caching
            sorted_ranked_files[hash]["data"]["title"] = files[hash]["title"]
            sorted_ranked_files[hash]["data"]["torrent_title"] = torrents_by_hash[hash][
                "Title"
            ]
            sorted_ranked_files[hash]["data"]["tracker"] = torrents_by_hash[hash][
                "Tracker"
            ]
            sorted_ranked_files[hash]["data"]["size"] = files[hash]["size"]
            sorted_ranked_files[hash]["data"]["torrent_size"] = torrents_by_hash[hash][
                "Size"
            ]
            sorted_ranked_files[hash]["data"]["index"] = files[hash]["index"]

        json_data = json.dumps(sorted_ranked_files).replace("'", "''")
        await database.execute(
            f"INSERT OR IGNORE INTO cache (cacheKey, results, timestamp) VALUES ('{cache_key}', '{json_data}', {time.time()})"
        )
        logger.info(f"Results have been cached for {log_name}")

        debrid_extension = get_debrid_extension(config["debridService"])

        balanced_hashes = await get_balanced_hashes(sorted_ranked_files, config)

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
                    "title": "Debrid Stream Proxy Password incorrect.\nStreams will not be proxied.",
                    "url": "https://comet.fast",
                }
            )

        for hash, hash_data in sorted_ranked_files.items():
            for resolution, hash_list in balanced_hashes.items():
                if hash in hash_list:
                    data = hash_data["data"]
                    results.append(
                        {
                            "name": f"[{debrid_extension}⚡] Comet {data['resolution'][0] if data['resolution'] != [] else 'Unknown'}",
                            "title": f"{data['title']}\n💾 {bytes_to_size(data['size'])} 🔎 {data['tracker']}",
                            "torrentTitle": data["torrent_title"],
                            "torrentSize": data["torrent_size"],
                            "url": f"{request.url.scheme}://{request.url.netloc}/{b64config}/playback/{hash}/{data['index']}",
                        }
                    )

                    continue

        return {"streams": results}


@streams.head("/{b64config}/playback/{hash}/{index}")
async def playback(b64config: str, hash: str, index: str):
    return RedirectResponse("https://stremio.fast", status_code=302)


@streams.get("/{b64config}/playback/{hash}/{index}")
async def playback(request: Request, b64config: str, hash: str, index: str):
    config = config_check(b64config)
    if not config:
        return FileResponse("comet/assets/invalidconfig.mp4")

    async with aiohttp.ClientSession() as session:
        # Check for cached download link
        cached_link = await database.fetch_one(
            f"SELECT link, timestamp FROM download_links WHERE debrid_key = '{config['debridApiKey']}' AND hash = '{hash}' AND `index` = '{index}'"
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
                    f"DELETE FROM download_links WHERE debrid_key = '{config['debridApiKey']}' AND hash = '{hash}' AND `index` = '{index}'"
                )

        if not download_link:
            debrid = getDebrid(session, config)
            download_link = await debrid.generate_download_link(hash, index)
            if not download_link:
                return FileResponse("comet/assets/uncached.mp4")

            # Cache the new download link
            await database.execute("INSERT OR REPLACE INTO download_links (debrid_key, hash, `index`, link, timestamp) VALUES (:debrid_key, :hash, :index, :link, :timestamp)", {"debrid_key": config["debridApiKey"], "hash": hash, "index": index, "link": download_link, "timestamp": current_time})

        proxy = (
            debrid.proxy if config["debridService"] == "alldebrid" else None
        )  # proxy is not needed to proxy realdebrid stream

        if (
            settings.PROXY_DEBRID_STREAM
            and settings.PROXY_DEBRID_STREAM_PASSWORD
            == config["debridStreamProxyPassword"]
        ):

            class Streamer:
                def __init__(self):
                    self.client = httpx.AsyncClient(proxy=proxy)
                    self.response = None

                async def stream_content(self, headers: dict):
                    async with self.client.stream(
                        "GET", download_link, headers=headers
                    ) as self.response:
                        async for chunk in self.response.aiter_raw():
                            yield chunk

                async def close(self):
                    if self.response is not None:
                        await self.response.aclose()
                    if self.client is not None:
                        await self.client.aclose()
        
            range_header = request.headers.get("range", "bytes=0-")

            response = await session.head(download_link, headers={"Range": range_header})
            if response.status == 206:
                streamer = Streamer()

                return StreamingResponse(
                    streamer.stream_content({"Range": range_header}),
                    status_code=206,
                    headers={
                        "Content-Range": response.headers["Content-Range"],
                        "Content-Length": response.headers["Content-Length"],
                        "Accept-Ranges": "bytes",
                    },
                    background=BackgroundTask(streamer.close),
                    )

            return FileResponse("comet/assets/uncached.mp4")

        return RedirectResponse(download_link, status_code=302)
