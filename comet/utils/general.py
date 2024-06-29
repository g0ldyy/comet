import base64
import hashlib
import json
import math
import os
import re

import aiohttp
import bencodepy
from RTN.patterns import language_code_mapping

from comet.utils.logger import logger
from comet.utils.models import settings

translationTable = {
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

translationTable = str.maketrans(translationTable)
infoHashPattern = re.compile(r"\b([a-fA-F0-9]{40})\b")
lang_code_map = [indexer.replace(" ", "_") for indexer in language_code_mapping.keys()]

def translate(title: str):
    return title.translate(translationTable)


def isVideo(title: str):
    return title.endswith(tuple([".mkv", ".mp4", ".avi", ".mov", ".flv", ".wmv", ".webm", ".mpg", ".mpeg", ".m4v", ".3gp", ".3g2", ".ogv", ".ogg", ".drc", ".gif", ".gifv", ".mng", ".avi", ".mov", ".qt", ".wmv", ".yuv", ".rm", ".rmvb", ".asf", ".amv", ".m4p", ".m4v", ".mpg", ".mp2", ".mpeg", ".mpe", ".mpv", ".mpg", ".mpeg", ".m2v", ".m4v", ".svi", ".3gp", ".3g2", ".mxf", ".roq", ".nsv", ".flv", ".f4v", ".f4p", ".f4a", ".f4b"]))


def bytesToSize(bytes: int):
    sizes = ["Bytes", "KB", "MB", "GB", "TB"]

    if bytes == 0:
        return "0 Byte"
    
    i = int(math.floor(math.log(bytes, 1024)))

    return f"{round(bytes / math.pow(1024, i), 2)} {sizes[i]}"


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


async def getIndexerManager(session: aiohttp.ClientSession, indexerManagerType: str, indexers: list, query: str):
    try:
        timeout = aiohttp.ClientTimeout(total=settings.INDEXER_MANAGER_TIMEOUT)
        results = []

        if indexerManagerType == "jackett":
            response = await session.get(f"{settings.INDEXER_MANAGER_URL}/api/v2.0/indexers/all/results?apikey={settings.INDEXER_MANAGER_API_KEY}&Query={query}&Tracker[]={'&Tracker[]='.join(indexer for indexer in indexers)}", timeout=timeout)
            response = await response.json()

            for result in response["Results"]:
                results.append(result)
        
        if indexerManagerType == "prowlarr":
            getIndexers = await session.get(f"{settings.INDEXER_MANAGER_URL}/api/v1/indexer", headers={
                "X-Api-Key": settings.INDEXER_MANAGER_API_KEY
            })
            getIndexers = await getIndexers.json()

            indexersId = []
            for indexer in getIndexers:
                if indexer["definitionName"] in indexers:
                    indexersId.append(indexer["id"])

            response = await session.get(f"{settings.INDEXER_MANAGER_URL}/api/v1/search?query={query}&indexerIds={'&indexerIds='.join(str(indexerId) for indexerId in indexersId)}&type=search", headers={
                "X-Api-Key": settings.INDEXER_MANAGER_API_KEY            
            })
            response = await response.json()

            for result in response:
                results.append(result)

        return results
    except Exception as e:
        logger.warning(f"Exception while getting {indexerManagerType} results for {query} with {indexers}: {e}")


async def getTorrentHash(session: aiohttp.ClientSession, indexerManagerType: str, torrent: dict):
    if "InfoHash" in torrent and torrent["InfoHash"] != None:
        return torrent["InfoHash"]
    
    if "infoHash" in torrent:
        return torrent["infoHash"]

    url = torrent["Link"] if indexerManagerType == "jackett" else torrent["downloadUrl"]

    try:
        timeout = aiohttp.ClientTimeout(total=settings.GET_TORRENT_TIMEOUT)
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
    except Exception as e:
        logger.warning(f"Exception while getting torrent info hash for {torrent['indexer'] if 'indexer' in torrent else (torrent['Tracker'] if 'Tracker' in torrent else '')}|{url}: {e}")
        # logger.warning(f"Exception while getting torrent info hash for {jackettIndexerPattern.findall(url)[0]}|{jackettNamePattern.search(url)[0]}: {e}")


async def generateDownloadLink(debridApiKey: str, hash: str, index: str):
    try:
        async with aiohttp.ClientSession() as session:
            checkBlacklisted = await session.get("https://real-debrid.com/vpn")
            checkBlacklisted = await checkBlacklisted.text()

            proxy = None
            if "Your ISP or VPN provider IP address is currently blocked on our website" in checkBlacklisted:
                proxy = settings.DEBRID_PROXY_URL
                if not proxy:
                    logger.warning(f"Real-Debrid blacklisted server's IP. No proxy found.")
                    return "https://comet.fast" # TODO: This needs to be handled better
                else:
                    logger.warning(f"Real-Debrid blacklisted server's IP. Switching to proxy {proxy} for {hash}|{index}")

            addMagnet = await session.post(f"https://api.real-debrid.com/rest/1.0/torrents/addMagnet", headers={
                "Authorization": f"Bearer {debridApiKey}"
            }, data={
                "magnet": f"magnet:?xt=urn:btih:{hash}"
            }, proxy=proxy)
            addMagnet = await addMagnet.json()

            getMagnetInfo = await session.get(addMagnet["uri"], headers={
                "Authorization": f"Bearer {debridApiKey}"
            }, proxy=proxy)
            getMagnetInfo = await getMagnetInfo.json()

            selectFile = await session.post(f"https://api.real-debrid.com/rest/1.0/torrents/selectFiles/{addMagnet['id']}", headers={
                "Authorization": f"Bearer {debridApiKey}"
            }, data={
                "files": index
            }, proxy=proxy)

            getMagnetInfo = await session.get(addMagnet["uri"], headers={
                "Authorization": f"Bearer {debridApiKey}"
            }, proxy=proxy)
            getMagnetInfo = await getMagnetInfo.json()

            unrestrictLink = await session.post(f"https://api.real-debrid.com/rest/1.0/unrestrict/link", headers={
                "Authorization": f"Bearer {debridApiKey}"
            }, data={
                "link": getMagnetInfo["links"][0]
            }, proxy=proxy)
            unrestrictLink = await unrestrictLink.json()

            return unrestrictLink["download"]
    except Exception as e:
        logger.warning(f"Exception while getting download link from Real Debrid for {hash}|{index}: {e}")

        return "https://comet.fast"
