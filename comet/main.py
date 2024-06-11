import asyncio
import base64
import hashlib
import json
import os
import re
from typing import Optional

import aiohttp
import bencodepy
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from pydantic import ValidationError
from RTN import parse

from comet.logger import logger
from comet.models import ConfigModel, settings, video_extensions

downloadLinks = {} # temporary before sqlite cache db is implemented

app = FastAPI(docs_url=None)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def configChecking(config_str: str) -> Optional[ConfigModel]:
    try:
        config_data = json.loads(config_str)
        config = ConfigModel(**config_data)
        return config
    except (json.JSONDecodeError, ValidationError):
        return None

@app.get("/manifest.json")
@app.get("/{config_str}/manifest.json")
async def manifest(request: Request, config_str: str = None):
    if config_str and not configChecking(config_str):
        raise HTTPException(status_code=400, detail="Invalid configuration")

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
    response = await session.get(f"{settings.JACKETT_URL}/api/v2.0/indexers/all/results?apikey={settings.JACKETT_KEY}&Query={query}&Tracker[]={'&Tracker[]='.join(indexer for indexer in indexers)}")
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

            match = re.search("btih:([a-zA-Z0-9]+)", location)
            if not match:
                return
            
            hash = match.group(1).upper()

        return hash
    except:
        pass

@app.get("/stream/{type}/{id}.json")
@app.get("/{config_str}/stream/{type}/{id}.json")
async def stream(request: Request, type: str, id: str, config_str: str = None):
    if config_str:
        config = configChecking(config_str)
        if not config:
            raise HTTPException(status_code=400, detail="Invalid configuration")
    else:
        config = None  # Handle the case where config_str is not provided
    
    async with aiohttp.ClientSession() as session:
        if config and not await is_premium_account(session, config.debridApiKey):
            return invalid_account_response()

        if type == "series":
            id, season, episode = parse_series_id(id)

        name = await get_metadata_name(session, id)
        name = translate_name(name)

        logger.debug(f"Start of Jackett search for {name}")

        if type == "series":
            torrents = await search_torrents(session, config.indexers if config else [], name, type, season, episode)
        else:
            torrents = await search_torrents(session, config.indexers if config else [], name, type)
        logger.debug(f"{len(torrents)} torrents found for {name}")

        if not torrents:
            return {"streams": []}

        torrentHashes = await get_torrent_hashes(session, torrents)
        logger.debug(f"{len(torrentHashes)} info hashes found for {name}")

        if not torrentHashes:
            return {"streams": []}

        files = await get_available_files(session, config.debridApiKey if config else "", torrentHashes, type, season, episode)
        logger.debug(f"{len(files)} cached files found on Real-Debrid for {name}")

        if not files:
            return {"streams": []}

        results = generate_stream_results(files, config.maxResults if config else 0, request.url, config_str)
        return {"streams": results}

async def is_premium_account(session, debridApiKey: str) -> bool:
    checkDebrid = await session.get("https://api.real-debrid.com/rest/1.0/user", headers={
        "Authorization": f"Bearer {debridApiKey}"
    })
    checkDebrid = await checkDebrid.text()
    return '"type": "premium"' in checkDebrid

def invalid_account_response():
    return {
        "streams": [
            {
                "name": f"[⚠️] Comet", 
                "title": f"Invalid Real-Debrid account.",
                "url": f"https://comet.fast"
            }
        ]
    }

def parse_series_id(id: str):
    info = id.split(":")
    return info[0], info[1].zfill(2), info[2].zfill(2)

async def get_metadata_name(session, id: str) -> str:
    getMetadata = await session.get(f"https://v3.sg.media-imdb.com/suggestion/a/{id}.json")
    metadata = await getMetadata.json()
    return metadata["d"][0]["l"]

def translate_name(name: str) -> str:
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
    return name.translate(translationTable)

async def search_torrents(session, indexers: list, name: str, type: str, season: str = None, episode: str = None):
    tasks = [getJackett(session, indexers, name)]
    if type == "series":
        tasks.append(getJackett(session, indexers, f"{name} S{season}E{episode}"))
    jackettSearchResponses = await asyncio.gather(*tasks)

    torrents = []
    for response in jackettSearchResponses:
        results = await response.json()
        for i in results["Results"]:
            torrents.append(i)
    return torrents

async def get_torrent_hashes(session, torrents: list):
    tasks = [getTorrentHash(session, torrent["Link"]) for torrent in torrents]
    torrentHashes = await asyncio.gather(*tasks)
    return list(set([hash for hash in torrentHashes if hash]))

async def get_available_files(session, debridApiKey: str, torrentHashes: list, type: str, season: str = None, episode: str = None):
    getAvailability = await session.get(f"https://api.real-debrid.com/rest/1.0/torrents/instantAvailability/{'/'.join(torrentHashes)}", headers={
        "Authorization": f"Bearer {debridApiKey}"
    })

    files = {}
    strictFiles = {}

    availability = await getAvailability.json()
    for hash, details in availability.items():
        if not "rd" in details:
            continue

        if type == "series":
            for variants in details["rd"]:
                for index, file in variants.items():
                    filename = file["filename"].lower()
                    
                    if not filename.endswith(video_extensions):
                        continue

                    if season in filename and episode in filename:
                        files[hash] = {
                            "index": index,
                            "title": file["filename"],
                            "size": file["filesize"]
                        }

                    if f"s{season}" in filename and f"e{episode}" in filename:
                        strictFiles[hash] = {
                            "index": index,
                            "title": file["filename"],
                            "size": file["filesize"]
                        }

            continue

        for variants in details["rd"]:
            for index, file in variants.items():
                filename = file["filename"].lower()
                if not filename.endswith(video_extensions):
                    continue

                files[hash] = {
                    "index": index,
                    "title": file["filename"],
                    "size": file["filesize"]
                }

    if len(strictFiles) > 0:
        files = strictFiles

    return files

def generate_stream_results(files, maxResults, url, config_str):
    files = dict(sorted(files.items(), key=lambda item: item[1]["size"], reverse=True))

    filesByResolution = {"4k": [], "1080p": [], "720p": [], "480p": [], "Unknown": []}
    for key, value in files.items():
        qualityPattern = (
            (r"\b(2160P|UHD|4K)\b", "4k"),
            (r"\b(1080P|FHD|FULLHD|HD|HIGHDEFINITION)\b", "1080p"),
            (r"\b(720P|HD|HIGHDEFINITION)\b", "720p"),
            (r"\b(480P|SD|STANDARDDEFINITION)\b", "480p"),
        )

        resolution = "Unknown"
        for pattern, quality in qualityPattern:
            if re.search(pattern, value["title"], re.IGNORECASE):
                resolution = quality
        
        filesByResolution[resolution].append({
            key: value
        })

    hashCount = 0
    for quality in filesByResolution:
        hashCount += len(filesByResolution[quality])

    results = []
    if hashCount <= maxResults or maxResults == 0:
        for quality, files in filesByResolution.items():
            for file in files:
                for hash in file:
                    results.append({
                        "name": f"[RD⚡] Comet {quality}",
                        "title": f"{file[hash]['title']}\n💾 {round(int(file[hash]['size']) / 1024 / 1024 / 1024, 2)}GB",
                        "url": f"{url.scheme}://{url.netloc}/{config_str}/playback/{hash}/{file[hash]['index']}" if config_str else f"{url.scheme}://{url.netloc}/playback/{hash}/{file[hash]['index']}"
                    })
    else:
        selectedFiles = []
        resolutionCount = {res: 0 for res in filesByResolution.keys()}
        resolutions = list(filesByResolution.keys())
        
        while len(selectedFiles) < maxResults:
            for resolution in resolutions:
                if len(selectedFiles) >= maxResults:
                    break
                if resolutionCount[resolution] < len(filesByResolution[resolution]):
                    selectedFiles.append((resolution, filesByResolution[resolution][resolutionCount[resolution]]))
                    resolutionCount[resolution] += 1
        
        balancedFiles = {res: [] for res in filesByResolution.keys()}
        for resolution, file in selectedFiles:
            balancedFiles[resolution].append(file)

        for quality, files in balancedFiles.items():
            for file in files:
                for hash in file:
                    results.append({
                        "name": f"[RD⚡] Comet {quality}",
                        "title": f"{file[hash]['title']}\n💾 {round(int(file[hash]['size']) / 1024 / 1024 / 1024, 2)}GB",
                        "url": f"{url.scheme}://{url.netloc}/{config_str}/playback/{hash}/{file[hash]['index']}" if config_str else f"{url.scheme}://{url.netloc}/playback/{hash}/{file[hash]['index']}"
                    })

    return results

async def generateDownloadLink(session, debridApiKey: str, hash: str, index: str):
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

@app.head("/{b64config}/playback/{hash}/{index}")
async def stream(b64config: str, hash: str, index: str):
    config = configChecking(b64config)
    if not config:
        return

    async with aiohttp.ClientSession() as session:
        downloadLink = await generateDownloadLink(session, config["debridApiKey"], hash, index)
        downloadLinks[(hash, index)] = downloadLink

        return RedirectResponse(downloadLink, status_code=302)
    
@app.get("/{b64config}/playback/{hash}/{index}")
async def stream(b64config: str, hash: str, index: str):
    config = configChecking(b64config)
    if not config:
        return

    async with aiohttp.ClientSession() as session:
        if (hash, index) in downloadLinks:
            downloadLink = downloadLinks[(hash, index)]
        else:
            downloadLink = await generateDownloadLink(session, config["debridApiKey"], hash, index)
            downloadLinks[(hash, index)] = downloadLink
        
        return RedirectResponse(downloadLink, status_code=302)