import asyncio
import hashlib
import json
import time
import aiohttp

from typing import Dict
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from RTN import ParsedData, Torrent, parse, sort_torrents

from comet.utils.general import (bytesToSize, configChecking,
                                 generateDownloadLink, getIndexerManager,
                                 getTorrentHash, isVideo, translate)
from comet.utils.logger import logger
from comet.utils.models import database, rtn, settings

streams = APIRouter()

@streams.get("/stream/{type}/{id}.json")
@streams.get("/{b64config}/stream/{type}/{id}.json")
async def stream(request: Request, b64config: str, type: str, id: str):
    config = configChecking(b64config)
    if not config:
            return {
                "streams": [
                    {
                        "name": "[⚠️] Comet", 
                        "title": "Invalid Comet config.",
                        "url": "https://comet.fast"
                    }
                ]
            }
    
    async with aiohttp.ClientSession() as session:
        checkDebrid = await session.get("https://api.real-debrid.com/rest/1.0/user", headers={
            "Authorization": f"Bearer {config['debridApiKey']}"
        })
        checkDebrid = await checkDebrid.text()
        if not '"type": "premium"' in checkDebrid:
            return {
                "streams": [
                    {
                        "name": "[⚠️] Comet", 
                        "title": "Invalid Real-Debrid account.",
                        "url": "https://comet.fast"
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

        getMetadata = await session.get(f"https://v3.sg.media-imdb.com/suggestion/a/{id}.json")
        metadata = await getMetadata.json()

        name = metadata["d"][0]["l"]
        name = translate(name)

        cacheKey = hashlib.md5(json.dumps({"debridService": config["debridService"], "name": name, "season": season, "episode": episode, "indexers": config["indexers"], "resolutions": config["resolutions"], "languages": config["languages"]}).encode("utf-8")).hexdigest()
        cached = await database.fetch_one(f"SELECT EXISTS (SELECT 1 FROM cache WHERE cacheKey = '{cacheKey}')")
        if cached[0] != 0:
            logger.info(f"Cache found for {name}")

            timestamp = await database.fetch_one(f"SELECT timestamp FROM cache WHERE cacheKey = '{cacheKey}'")
            if timestamp[0] + settings.CACHE_TTL < time.time():
                await database.execute(f"DELETE FROM cache WHERE cacheKey = '{cacheKey}'")

                logger.info(f"Cache expired for {name}")
            else:
                sortedRankedFiles = await database.fetch_one(f"SELECT results FROM cache WHERE cacheKey = '{cacheKey}'")
                sortedRankedFiles = json.loads(sortedRankedFiles[0])
                
                results = []
                for hash in sortedRankedFiles:
                    results.append({
                        "name": f"[RD⚡] Comet {sortedRankedFiles[hash]['data']['resolution'][0] if len(sortedRankedFiles[hash]['data']['resolution']) > 0 else 'Unknown'}",
                        "title": f"{sortedRankedFiles[hash]['data']['title']}\n💾 {bytesToSize(sortedRankedFiles[hash]['data']['size'])}",
                        "url": f"{request.url.scheme}://{request.url.netloc}/{b64config}/playback/{hash}/{sortedRankedFiles[hash]['data']['index']}"
                    })

                return {"streams": results}
        else:
            logger.info(f"No cache found for {name} with user configuration")

        indexerManagerType = settings.INDEXER_MANAGER_TYPE

        logger.info(f"Start of {indexerManagerType} search for {name} with indexers {config['indexers']}")

        tasks = []
        tasks.append(getIndexerManager(session, indexerManagerType, config["indexers"], name))
        if type == "series":
            tasks.append(getIndexerManager(session, indexerManagerType, config["indexers"], f"{name} S0{season}E0{episode}"))
        searchResponses = await asyncio.gather(*tasks)

        torrents = []
        for results in searchResponses:
            if results == None:
                continue

            for result in results:
                torrents.append(result)

        logger.info(f"{len(torrents)} torrents found for {name}")

        if len(torrents) == 0:
            return {"streams": []}

        tasks = []
        filtered = 0
        for torrent in torrents:
            parsedTorrent: ParsedData = parse(torrent["Title"]) if indexerManagerType == "jackett" else parse(torrent["title"])
            if not "All" in config["resolutions"] and len(parsedTorrent.resolution) > 0 and parsedTorrent.resolution[0] not in config["resolutions"]:
                filtered += 1

                continue
            if not "All" in config["languages"] and not parsedTorrent.is_multi_audio and not any(language in parsedTorrent.language for language in config["languages"]):
                filtered += 1

                continue

            tasks.append(getTorrentHash(session, indexerManagerType, torrent))
    
        torrentHashes = await asyncio.gather(*tasks)
        torrentHashes = list(set([hash for hash in torrentHashes if hash]))

        logger.info(f"{len(torrentHashes)} info hashes found for {name}")
        
        if len(torrentHashes) == 0:
            return {"streams": []}

        getAvailability = await session.get(f"https://api.real-debrid.com/rest/1.0/torrents/instantAvailability/{'/'.join(torrentHashes)}", headers={
            "Authorization": f"Bearer {config['debridApiKey']}"
        })

        files = {}

        availability = await getAvailability.json()
        for hash, details in availability.items():
            if not "rd" in details:
                continue

            if type == "series":
                for variants in details["rd"]:
                    for index, file in variants.items():
                        filename = file["filename"].lower()
                        
                        if not isVideo(filename):
                            continue

                        filenameParsed = parse(file["filename"])
                        if season in filenameParsed.season and episode in filenameParsed.episode:
                            files[hash] = {
                                "index": index,
                                "title": file["filename"],
                                "size": file["filesize"]
                            }

                continue

            for variants in details["rd"]:
                for index, file in variants.items():
                    filename = file["filename"].lower()

                    if not isVideo(filename):
                        continue

                    files[hash] = {
                        "index": index,
                        "title": file["filename"],
                        "size": file["filesize"]
                    }

        rankedFiles = set()
        for hash in files:
            # try:
            rankedFile = rtn.rank(files[hash]["title"], hash) # , remove_trash=True, correct_title=name - removed because it's not working great
            rankedFiles.add(rankedFile)
            # except:
            #     continue
        
        sortedRankedFiles: Dict[str, Torrent] = sort_torrents(rankedFiles)

        logger.info(f"{len(sortedRankedFiles)} cached files found on Real-Debrid for {name}")

        if len(sortedRankedFiles) == 0:
            return {"streams": []}
        
        sortedRankedFiles = {
            key: (value.model_dump() if isinstance(value, Torrent) else value)
            for key, value in sortedRankedFiles.items()
        }
        for hash in sortedRankedFiles: # needed for caching
            sortedRankedFiles[hash]["data"]["title"] = files[hash]["title"]
            sortedRankedFiles[hash]["data"]["size"] = files[hash]["size"]
            sortedRankedFiles[hash]["data"]["index"] = files[hash]["index"]
        
        jsonData = json.dumps(sortedRankedFiles).replace("'", "''")
        await database.execute(f"INSERT INTO cache (cacheKey, results, timestamp) VALUES ('{cacheKey}', '{jsonData}', {time.time()})")
        logger.info(f"Results have been cached for {name}")
        
        results = []
        for hash in sortedRankedFiles:
            results.append({
                "name": f"[RD⚡] Comet {sortedRankedFiles[hash]['data']['resolution'][0] if len(sortedRankedFiles[hash]['data']['resolution']) > 0 else 'Unknown'}",
                "title": f"{sortedRankedFiles[hash]['data']['title']}\n💾 {bytesToSize(sortedRankedFiles[hash]['data']['size'])}",
                "url": f"{request.url.scheme}://{request.url.netloc}/{b64config}/playback/{hash}/{sortedRankedFiles[hash]['data']['index']}"
            })

        return {
            "streams": results
        }

@streams.route("/{b64config}/playback/{hash}/{index}", methods=["HEAD", "GET"])
async def stream(b64config: str, hash: str, index: str):
    config = configChecking(b64config)
    if not config:
        return

    downloadLink = await generateDownloadLink(config["debridApiKey"], hash, index)

    return RedirectResponse(downloadLink, status_code=302)
