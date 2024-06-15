import aiohttp, asyncio, bencodepy, hashlib, re, base64, json, os, RTN, time, urllib.parse
from .utils.logger import logger
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from contextlib import asynccontextmanager
from databases import Database

database = Database("sqlite:///database.db")

class BestOverallRanking(RTN.BaseRankingModel):
    uhd: int = 100
    fhd: int = 90
    hd: int = 80
    sd: int = 70
    dolby_video: int = 100
    hdr: int = 80
    hdr10: int = 90
    dts_x: int = 100
    dts_hd: int = 80
    dts_hd_ma: int = 90
    atmos: int = 90
    truehd: int = 60
    ddplus: int = 40
    aac: int = 30
    ac3: int = 20
    remux: int = 150
    bluray: int = 120
    webdl: int = 90

settings = RTN.SettingsModel()
ranking_model = BestOverallRanking()
rtn = RTN.RTN(settings=settings, ranking_model=ranking_model)

infoHashPattern = re.compile(r"\b([a-fA-F0-9]{40})\b")

@asynccontextmanager
async def lifespan(app: FastAPI):
    await database.connect()
    await database.execute("CREATE TABLE IF NOT EXISTS cache (cacheKey BLOB PRIMARY KEY, timestamp INTEGER, results TEXT)")
    # await database.execute("CREATE TABLE IF NOT EXISTS debridDownloads (debridKey BLOB PRIMARY KEY, downloadLink TEXT)")
    yield
    await database.disconnect()

app = FastAPI(lifespan=lifespan, docs_url=None)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def configChecking(b64config: str):
    try:
        config = json.loads(base64.b64decode(b64config).decode())

        if not isinstance(config["debridService"], str) or config["debridService"] not in ["realdebrid"]:
            return False
        
        if not isinstance(config["debridApiKey"], str):
            return False

        if not isinstance(config["indexers"], list):
            return False

        if not isinstance(config["maxResults"], int) or config["maxResults"] < 0:
            return False
        
        if not isinstance(config["resolutions"], list) or len(config["resolutions"]) == 0:
            return False
        
        if not isinstance(config["languages"], list) or len(config["languages"]) == 0:
            return False

        return config
    except:
        return False

@app.get("/manifest.json")
@app.get("/{b64config}/manifest.json")
async def manifest(b64config: str):
    if not configChecking(b64config):
        return

    return {
        "id": "stremio.comet.fast",
        "version": "1.0.0",
        "name": "Comet",
        "description": "Stremio's fastest torrent/debrid search add-on.",
        "icon": "https://i.imgur.com/cZOiNzX.jpeg",
        "logo": "https://i.imgur.com/cZOiNzX.jpeg",
        "resources": [
            "stream"
        ],
        "types": [
            "movie",
            "series"
        ],
        "idPrefixes": [
            "tt"
        ],
        "catalogs": [],
        "behaviorHints": {
            "configurable": True
        }
    }

async def getJackett(session: aiohttp.ClientSession, indexers: list, query: str):
    timeout = aiohttp.ClientTimeout(total=int(os.getenv("JACKETT_TIMEOUT")))
    response = await session.get(f"{os.getenv('JACKETT_URL')}/api/v2.0/indexers/all/results?apikey={os.getenv('JACKETT_KEY')}&Query={query}&Tracker[]={'&Tracker[]='.join(indexer for indexer in indexers)}", timeout=timeout)
    return response

async def getTorrentHash(session: aiohttp.ClientSession, url: str):
    try:
        timeout = aiohttp.ClientTimeout(total=int(os.getenv("GET_TORRENT_TIMEOUT")))
        response = await session.get(url, allow_redirects=False, timeout=timeout)
        if response.status == 200:
            torrentData = await response.read()
            torrentDict = bencodepy.decode(torrentData)
            info = bencodepy.encode(torrentDict[b"info"])
            hash = hashlib.sha1(info).hexdigest()
        else:
            location = response.headers.get("Location", "")
            if not location:
                return

            match = infoHashPattern.search(location)
            if not match:
                return
            
            hash = match.group(1).upper()

        return hash
    except:
        pass

@app.get("/stream/{type}/{id}.json")
@app.get("/{b64config}/stream/{type}/{id}.json")
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
        toChange = {
            'ā': 'a', 'ă': 'a', 'ą': 'a', 'ć': 'c', 'č': 'c', 'ç': 'c',
            'ĉ': 'c', 'ċ': 'c', 'ď': 'd', 'đ': 'd', 'è': 'e', 'é': 'e',
            'ê': 'e', 'ë': 'e', 'ē': 'e', 'ĕ': 'e', 'ę': 'e', 'ě': 'e',
            'ĝ': 'g', 'ğ': 'g', 'ġ': 'g', 'ģ': 'g', 'ĥ': 'h', 'î': 'i',
            'ï': 'i', 'ì': 'i', 'í': 'i', 'ī': 'i', 'ĩ': 'i', 'ĭ': 'i',
            'ı': 'i', 'ĵ': 'j', 'ķ': 'k', 'ĺ': 'l', 'ļ': 'l', 'ł': 'l',
            'ń': 'n', 'ň': 'n', 'ñ': 'n', 'ņ': 'n', 'ŉ': 'n', 'ó': 'o',
            'ô': 'o', 'õ': 'o', 'ö': 'o', 'ø': 'o', 'ō': 'o', 'ő': 'o',
            'œ': 'oe', 'ŕ': 'r', 'ř': 'r', 'ŗ': 'r', 'š': 's', 'ş': 's',
            'ś': 's', 'ș': 's', 'ß': 'ss', 'ť': 't', 'ţ': 't', 'ū': 'u',
            'ŭ': 'u', 'ũ': 'u', 'û': 'u', 'ü': 'u', 'ù': 'u', 'ú': 'u',
            'ų': 'u', 'ű': 'u', 'ŵ': 'w', 'ý': 'y', 'ÿ': 'y', 'ŷ': 'y',
            'ž': 'z', 'ż': 'z', 'ź': 'z', 'æ': 'ae', 'ǎ': 'a', 'ǧ': 'g',
            'ə': 'e', 'ƒ': 'f', 'ǐ': 'i', 'ǒ': 'o', 'ǔ': 'u', 'ǚ': 'u',
            'ǜ': 'u', 'ǹ': 'n', 'ǻ': 'a', 'ǽ': 'ae', 'ǿ': 'o',
        }
        translationTable = str.maketrans(toChange)
        name = name.translate(translationTable)

        cacheKey = hashlib.md5(json.dumps({"name": name, "season": season, "episode": episode, "indexers": config["indexers"], "resolutions": config["resolutions"], "languages": config["languages"]}).encode("utf-8")).hexdigest()
        cached = await database.fetch_one(f"SELECT EXISTS (SELECT 1 FROM cache WHERE cacheKey = '{cacheKey}')")
        if cached[0] != 0:
            logger.info(f"Cache found for {name}")

            timestamp = await database.fetch_one(f"SELECT timestamp FROM cache WHERE cacheKey = '{cacheKey}'")
            if timestamp[0] + int(os.getenv("CACHE_TTL")) < time.time():
                await database.execute(f"DELETE FROM cache WHERE cacheKey = '{cacheKey}'")

                logger.info(f"Cache expired for {name}")
            else:
                sortedRankedFiles = await database.fetch_one(f"SELECT results FROM cache WHERE cacheKey = '{cacheKey}'")
                sortedRankedFiles = json.loads(sortedRankedFiles[0])
                
                results = []
                for hash in sortedRankedFiles:
                    results.append({
                        "name": f"[RD⚡] Comet {sortedRankedFiles[hash]['data']['resolution'][0] if len(sortedRankedFiles[hash]['data']['resolution']) > 0 else 'Unknown'}",
                        "title": f"{sortedRankedFiles[hash]['data']['title']}\n💾 {round(int(sortedRankedFiles[hash]['data']['size']) / 1024 / 1024 / 1024, 2)}GB",
                        "url": f"{request.url.scheme}://{request.url.netloc}/{b64config}/playback/{hash}/{sortedRankedFiles[hash]['data']['index']}"
                    })

                return {"streams": results}
        else:
            logger.info(f"No cache found for {name} with user configuration")

        logger.info(f"Start of Jackett search for {name} with indexers {config['indexers']}")

        tasks = []
        tasks.append(getJackett(session, config["indexers"], name))
        if type == "series":
            tasks.append(getJackett(session, config["indexers"], f"{name} S0{season}E0{episode}"))
        jackettSearchResponses = await asyncio.gather(*tasks)

        torrents = []
        for response in jackettSearchResponses:
            results = await response.json()
            for i in results["Results"]:
                torrents.append(i)

        logger.info(f"{len(torrents)} torrents found for {name}")

        if len(torrents) == 0:
            return {"streams": []}

        tasks = []
        for torrent in torrents:
            parsedTorrent = RTN.parse(torrent["Title"])
            if not "All" in config["resolutions"] and len(parsedTorrent.resolution) > 0 and parsedTorrent.resolution[0] not in config["resolutions"]:
                filtered += 1

                continue
            if not "All" in config["languages"] and not parsedTorrent.is_multi_audio and not any(language in parsedTorrent.language for language in config["languages"]):
                filtered += 1

                continue

            tasks.append(getTorrentHash(session, torrent["Link"]))
    
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
                        
                        if not filename.endswith(tuple([".mkv", ".mp4", ".avi", ".mov", ".flv", ".wmv", ".webm", ".mpg", ".mpeg", ".m4v", ".3gp", ".3g2", ".ogv", ".ogg", ".drc", ".gif", ".gifv", ".mng", ".avi", ".mov", ".qt", ".wmv", ".yuv", ".rm", ".rmvb", ".asf", ".amv", ".m4p", ".m4v", ".mpg", ".mp2", ".mpeg", ".mpe", ".mpv", ".mpg", ".mpeg", ".m2v", ".m4v", ".svi", ".3gp", ".3g2", ".mxf", ".roq", ".nsv", ".flv", ".f4v", ".f4p", ".f4a", ".f4b"])):
                            continue

                        filenameParsed = RTN.parse(file["filename"])
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
                    if not filename.endswith(tuple([".mkv", ".mp4", ".avi", ".mov", ".flv", ".wmv", ".webm", ".mpg", ".mpeg", ".m4v", ".3gp", ".3g2", ".ogv", ".ogg", ".drc", ".gif", ".gifv", ".mng", ".avi", ".mov", ".qt", ".wmv", ".yuv", ".rm", ".rmvb", ".asf", ".amv", ".m4p", ".m4v", ".mpg", ".mp2", ".mpeg", ".mpe", ".mpv", ".mpg", ".mpeg", ".m2v", ".m4v", ".svi", ".3gp", ".3g2", ".mxf", ".roq", ".nsv", ".flv", ".f4v", ".f4p", ".f4a", ".f4b"])):
                        continue

                    files[hash] = {
                        "index": index,
                        "title": file["filename"],
                        "size": file["filesize"]
                    }

        # await database.execute(f"INSERT INTO cache (cacheKey, results, timestamp) VALUES ('{cacheKey}', '{json.dumps(files)}', {time.time()})")
        # logger.info(f"Results have been cached for {name}")

        rankedFiles = set()
        for hash in files:
            try:
                rankedFile = rtn.rank(files[hash]["title"], hash, remove_trash=True) # , correct_title=name - removed because it's not working great
                rankedFiles.add(rankedFile)
            except:
                continue
        
        sortedRankedFiles = RTN.sort_torrents(rankedFiles)

        logger.info(f"{len(sortedRankedFiles)} cached files found on Real-Debrid for {name}")

        if len(sortedRankedFiles) == 0:
            return {"streams": []}
        
        sortedRankedFiles = {
            key: (value.model_dump() if isinstance(value, RTN.Torrent) else value)
            for key, value in sortedRankedFiles.items()
        }
        for hash in sortedRankedFiles: # needed for caching
            sortedRankedFiles[hash]["data"]["title"] = files[hash]["title"]
            sortedRankedFiles[hash]["data"]["size"] = files[hash]["size"]
            sortedRankedFiles[hash]["data"]["index"] = files[hash]["index"]
        
        await database.execute(f"INSERT INTO cache (cacheKey, results, timestamp) VALUES ('{cacheKey}', '{json.dumps(sortedRankedFiles)}', {time.time()})")
        logger.info(f"Results have been cached for {name}")
        
        results = []
        for hash in sortedRankedFiles:
            results.append({
                "name": f"[RD⚡] Comet {sortedRankedFiles[hash]['data']['resolution'][0] if len(sortedRankedFiles[hash]['data']['resolution']) > 0 else 'Unknown'}",
                "title": f"{sortedRankedFiles[hash]['data']['title']}\n💾 {round(int(sortedRankedFiles[hash]['data']['size']) / 1024 / 1024 / 1024, 2)}GB",
                "url": f"{request.url.scheme}://{request.url.netloc}/{b64config}/playback/{hash}/{sortedRankedFiles[hash]['data']['index']}"
            })

        # filesByResolution = {"Unknown": []}
        # for file in sortedFiles:
        #     if len(file.data.resolution) == 0:
        #         filesByResolution["Unknown"].append(file)
                
        #         continue

        #     if file.data.resolution[0] not in filesByResolution:
        #         filesByResolution[file.data.resolution[0]] = []

        #     filesByResolution[file.data.resolution[0]].append(file)

        # hashCount = 0
        # for quality in filesByResolution:
        #     hashCount += len(filesByResolution[quality])

        # results = []
        # if hashCount <= config["maxResults"] or config["maxResults"] == 0:
        #     for quality, files in filesByResolution.items():
        #         for file in files:
        #             for hash in file:
        #                 results.append({
        #                     "name": f"[RD⚡] Comet {quality}",
        #                     "title": f"{file[hash]['title']}\n💾 {round(int(file[hash]['size']) / 1024 / 1024 / 1024, 2)}GB",
        #                     "url": f"{request.url.scheme}://{request.url.netloc}/{b64config}/playback/{hash}/{file[hash]['index']}"
        #                 })
        # else:
        #     selectedFiles = []
        #     resolutionCount = {res: 0 for res in filesByResolution.keys()}
        #     resolutions = list(filesByResolution.keys())
            
        #     while len(selectedFiles) < config["maxResults"]:
        #         for resolution in resolutions:
        #             if len(selectedFiles) >= config["maxResults"]:
        #                 break
        #             if resolutionCount[resolution] < len(filesByResolution[resolution]):
        #                 selectedFiles.append((resolution, filesByResolution[resolution][resolutionCount[resolution]]))
        #                 resolutionCount[resolution] += 1
            
        #     balancedFiles = {res: [] for res in filesByResolution.keys()}
        #     for resolution, file in selectedFiles:
        #         balancedFiles[resolution].append(file)

        #     for quality, files in balancedFiles.items():
        #         for file in files:
        #             for hash in file:
        #                 results.append({
        #                     "name": f"[RD⚡] Comet {quality}",
        #                     "title": f"{file[hash]['title']}\n💾 {round(int(file[hash]['size']) / 1024 / 1024 / 1024, 2)}GB",
        #                     "url": f"{request.url.scheme}://{request.url.netloc}/{b64config}/playback/{hash}/{file[hash]['index']}"
        #                 })

        return {
            "streams": results
        }
    
async def generateDownloadLink(session: aiohttp.ClientSession, debridApiKey: str, hash: str, index: str):
    try:
        addMagnet = await session.post(f"https://api.real-debrid.com/rest/1.0/torrents/addMagnet", headers={
            "Authorization": f"Bearer {debridApiKey}"
        }, data={
            "magnet": f"magnet:?xt=urn:btih:{hash}"
        })
        addMagnet = await addMagnet.json()

        getMagnetInfo = await session.get(addMagnet["uri"], headers={
            "Authorization": f"Bearer {debridApiKey}"
        })
        getMagnetInfo = await getMagnetInfo.json()

        selectFile = await session.post(f"https://api.real-debrid.com/rest/1.0/torrents/selectFiles/{addMagnet['id']}", headers={
            "Authorization": f"Bearer {debridApiKey}"
        }, data={
            "files": index
        })

        getMagnetInfo = await session.get(addMagnet["uri"], headers={
            "Authorization": f"Bearer {debridApiKey}"
        })
        getMagnetInfo = await getMagnetInfo.json()

        unrestrictLink = await session.post(f"https://api.real-debrid.com/rest/1.0/unrestrict/link", headers={
            "Authorization": f"Bearer {debridApiKey}"
        }, data={
            "link": getMagnetInfo["links"][0]
        })
        unrestrictLink = await unrestrictLink.json()

        return unrestrictLink["download"]
    except:
        return "https://comet.fast"

@app.head("/{b64config}/playback/{hash}/{index}")
async def stream(b64config: str, hash: str, index: str):
    config = configChecking(b64config)
    if not config:
        return

    async with aiohttp.ClientSession() as session:
        downloadLink = await generateDownloadLink(session, config["debridApiKey"], hash, index)
        # downloadLink = await generateDownloadLink(session, config["debridApiKey"], hash, index)

        # debridKey = hashlib.md5(json.dumps({"debridApiKey": config["debridApiKey"], "hash": hash, "index": index}).encode("utf-8")).hexdigest()
        # await database.execute(f"INSERT INTO debridDownloads (debridKey, downloadLink) VALUES ('{debridKey}', '{urllib.parse.unquote(downloadLink)}')")

        return RedirectResponse(downloadLink, status_code=302)
    
@app.get("/{b64config}/playback/{hash}/{index}")
async def stream(b64config: str, hash: str, index: str):
    config = configChecking(b64config)
    if not config:
        return

    async with aiohttp.ClientSession() as session:
        downloadLink = await generateDownloadLink(session, config["debridApiKey"], hash, index)
        # debridKey = hashlib.md5(json.dumps({"debridApiKey": config["debridApiKey"], "hash": hash, "index": index}).encode("utf-8")).hexdigest()

        # downloaded = await database.fetch_one(f"SELECT EXISTS (SELECT 1 FROM debridDownloads WHERE debridKey = '{debridKey}')")
        # if downloaded[0] != 0:
        #     downloadLink = await database.fetch_one(f"SELECT downloadLink FROM debridDownloads WHERE debridKey = '{debridKey}'")
        #     downloadLink = downloadLink[0]
        #     await database.execute(f"DELETE FROM debridDownloads WHERE debridKey = '{debridKey}'")
        # else:
        #     downloadLink = await generateDownloadLink(session, config["debridApiKey"], hash, index)

        return RedirectResponse(downloadLink, status_code=302)
