import asyncio
import hashlib
import json
import time
import aiohttp
import httpx

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, StreamingResponse
from starlette.background import BackgroundTask
from RTN import Torrent, sort_torrents

from comet.debrid.manager import getDebrid
from comet.utils.general import (
    bytes_to_size,
    config_check,
    get_indexer_manager,
    get_zilean,
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

        season = None
        episode = None
        if type == "series":
            info = id.split(":")

            id = info[0]
            season = int(info[1])
            episode = int(info[2])

        try:
            get_metadata = await session.get(
                f"https://v3.sg.media-imdb.com/suggestion/a/{id}.json"
            )
            metadata = await get_metadata.json()

            name = metadata["d"][0 if len(metadata["d"]) == 1 else 1]["l"]
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

                if config["debridService"] == "realdebrid":
                    debrid_extension = "RD"
                elif config["debridService"] == "alldebrid":
                    debrid_extension = "AD"
                elif config["debridService"] == "premiumize":
                    debrid_extension = "PM"
                elif config["debridService"] == "torbox":
                    debrid_extension = "TB"

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
                ) in sorted_ranked_files.items():  # Like that to keep ranking order
                    for resolution, hash_list in balanced_hashes.items():
                        if hash in hash_list:
                            results.append(
                                {
                                    "name": f"[{debrid_extension}⚡] Comet {hash_data['data']['resolution'][0] if hash_data['data']['resolution'] else 'Unknown'}",
                                    "title": f"{hash_data['data']['title']}\n💾 {bytes_to_size(hash_data['data']['size'])}",
                                    "url": f"{request.url.scheme}://{request.url.netloc}/{b64config}/playback/{hash}/{hash_data['data']['index']}",
                                }
                            )

                            continue

                return {"streams": results}
        else:
            logger.info(f"No cache found for {log_name} with user configuration")

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
                search_terms.append(f"{name} S0{season}E0{episode}")
            tasks.extend(
                get_indexer_manager(
                    session, indexer_manager_type, config["indexers"], term
                )
                for term in search_terms
            )
        else:
            logger.info(f"No indexer selected by user for {log_name}")

        if settings.ZILEAN_URL:
            tasks.append(get_zilean(session, indexer_manager_type, name, log_name))

        search_response = await asyncio.gather(*tasks)
        for results in search_response:
            for result in results:
                torrents.append(result)

        logger.info(
            f"{len(torrents)} torrents found for {log_name} with {indexer_manager_type}{' and Zilean' if settings.ZILEAN_URL else ''}"
        )

        if len(torrents) == 0:
            return {"streams": []}

        filter_title = config["filterTitles"]
        if filter_title:
            chunk_size = 50
            chunks = [
                torrents[i : i + chunk_size]
                for i in range(0, len(torrents), chunk_size)
            ]

            tasks = []
            for chunk in chunks:
                tasks.append(filter(chunk, name, indexer_manager_type))

            filtered_total = await asyncio.gather(*tasks)

            filtered_torrents = []
            for filtered in filtered_total:
                filtered_torrents.extend(filtered)
        else:
            filtered_torrents = torrents

        logger.info(
            f"{len(torrents) - len(filtered_torrents)} filtered torrents for {log_name}"
        )

        tasks = []
        for torrent in filtered_torrents:
            tasks.append(get_torrent_hash(session, indexer_manager_type, torrent))

        torrent_hashes = await asyncio.gather(*tasks)
        torrent_hashes = list(set([hash for hash in torrent_hashes if hash]))

        logger.info(f"{len(torrent_hashes)} info hashes found for {log_name}")

        if len(torrent_hashes) == 0:
            return {"streams": []}

        files = await debrid.get_files(torrent_hashes, type, season, episode)

        ranked_files = set()
        for hash in files:
            ranked_file = rtn.rank(files[hash]["title"], hash)
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
        for hash in sorted_ranked_files:  # needed for caching
            sorted_ranked_files[hash]["data"]["title"] = files[hash]["title"]
            sorted_ranked_files[hash]["data"]["size"] = files[hash]["size"]
            sorted_ranked_files[hash]["data"]["index"] = files[hash]["index"]

        json_data = json.dumps(sorted_ranked_files).replace("'", "''")
        await database.execute(
            f"INSERT OR IGNORE INTO cache (cacheKey, results, timestamp) VALUES ('{cache_key}', '{json_data}', {time.time()})"
        )
        logger.info(f"Results have been cached for {log_name}")

        if config["debridService"] == "realdebrid":
            debrid_extension = "RD"
        elif config["debridService"] == "alldebrid":
            debrid_extension = "AD"
        elif config["debridService"] == "premiumize":
            debrid_extension = "PM"
        elif config["debridService"] == "torbox":
            debrid_extension = "TB"

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
                    results.append(
                        {
                            "name": f"[{debrid_extension}⚡] Comet {hash_data['data']['resolution'][0] if hash_data['data']['resolution'] else 'Unknown'}",
                            "title": f"{hash_data['data']['title']}\n💾 {bytes_to_size(hash_data['data']['size'])}",
                            "url": f"{request.url.scheme}://{request.url.netloc}/{b64config}/playback/{hash}/{hash_data['data']['index']}",
                        }
                    )

                    continue

        return {"streams": results}


@streams.head("/{b64config}/playback/{hash}/{index}")
async def playback(b64config: str, hash: str, index: str):
    config = config_check(b64config)
    if not config:
        return

    async with aiohttp.ClientSession() as session:
        debrid = getDebrid(session, config)
        download_link = await debrid.generate_download_link(hash, index)

        if download_link is None:
            return

    return RedirectResponse(download_link, status_code=302)


@streams.get("/{b64config}/playback/{hash}/{index}")
async def playback(request: Request, b64config: str, hash: str, index: str):
    config = config_check(b64config)
    if not config:
        return

    async with aiohttp.ClientSession() as session:
        debrid = getDebrid(session, config)
        download_link = await debrid.generate_download_link(hash, index)
        if download_link is None:
            return

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
                    self.response = None

                async def stream_content(self, headers: dict):
                    async with httpx.AsyncClient(proxy=proxy) as client:
                        async with client.stream(
                            "GET", download_link, headers=headers
                        ) as self.response:
                            async for chunk in self.response.aiter_raw():
                                yield chunk

                async def close(self):
                    if self.response is not None:
                        await self.response.aclose()

            range = None
            range_header = request.headers.get("range")
            if range_header:
                range_value = range_header.strip().split("=")[1]
                start, end = range_value.split("-")
                start = int(start)
                end = int(end) if end else ""
                range = f"bytes={start}-{end}"

            async with await session.get(
                download_link, headers={"Range": range}, proxy=proxy
            ) as response:
                if response.status == 206:
                    streamer = Streamer()

                    return StreamingResponse(
                        streamer.stream_content({"Range": range}),
                        status_code=206,
                        headers={
                            "Content-Range": response.headers["Content-Range"],
                            "Content-Length": response.headers["Content-Length"],
                            "Accept-Ranges": "bytes",
                        },
                        background=BackgroundTask(await streamer.close()),
                    )
            return

        return RedirectResponse(download_link, status_code=302)
