import base64
import hashlib
import json
import math
import re
import aiohttp
import bencodepy

from comet.utils.logger import logger
from comet.utils.models import settings

translation_table = {
    "ā": "a",
    "ă": "a",
    "ą": "a",
    "ć": "c",
    "č": "c",
    "ç": "c",
    "ĉ": "c",
    "ċ": "c",
    "ď": "d",
    "đ": "d",
    "è": "e",
    "é": "e",
    "ê": "e",
    "ë": "e",
    "ē": "e",
    "ĕ": "e",
    "ę": "e",
    "ě": "e",
    "ĝ": "g",
    "ğ": "g",
    "ġ": "g",
    "ģ": "g",
    "ĥ": "h",
    "î": "i",
    "ï": "i",
    "ì": "i",
    "í": "i",
    "ī": "i",
    "ĩ": "i",
    "ĭ": "i",
    "ı": "i",
    "ĵ": "j",
    "ķ": "k",
    "ĺ": "l",
    "ļ": "l",
    "ł": "l",
    "ń": "n",
    "ň": "n",
    "ñ": "n",
    "ņ": "n",
    "ŉ": "n",
    "ó": "o",
    "ô": "o",
    "õ": "o",
    "ö": "o",
    "ø": "o",
    "ō": "o",
    "ő": "o",
    "œ": "oe",
    "ŕ": "r",
    "ř": "r",
    "ŗ": "r",
    "š": "s",
    "ş": "s",
    "ś": "s",
    "ș": "s",
    "ß": "ss",
    "ť": "t",
    "ţ": "t",
    "ū": "u",
    "ŭ": "u",
    "ũ": "u",
    "û": "u",
    "ü": "u",
    "ù": "u",
    "ú": "u",
    "ų": "u",
    "ű": "u",
    "ŵ": "w",
    "ý": "y",
    "ÿ": "y",
    "ŷ": "y",
    "ž": "z",
    "ż": "z",
    "ź": "z",
    "æ": "ae",
    "ǎ": "a",
    "ǧ": "g",
    "ə": "e",
    "ƒ": "f",
    "ǐ": "i",
    "ǒ": "o",
    "ǔ": "u",
    "ǚ": "u",
    "ǜ": "u",
    "ǹ": "n",
    "ǻ": "a",
    "ǽ": "ae",
    "ǿ": "o",
}

translation_table = str.maketrans(translation_table)
info_hash_pattern = re.compile(r"\b([a-fA-F0-9]{40})\b")


def translate(title: str):
    return title.translate(translation_table)


def is_video(title: str):
    return title.endswith(
        tuple(
            [
                ".mkv",
                ".mp4",
                ".avi",
                ".mov",
                ".flv",
                ".wmv",
                ".webm",
                ".mpg",
                ".mpeg",
                ".m4v",
                ".3gp",
                ".3g2",
                ".ogv",
                ".ogg",
                ".drc",
                ".gif",
                ".gifv",
                ".mng",
                ".avi",
                ".mov",
                ".qt",
                ".wmv",
                ".yuv",
                ".rm",
                ".rmvb",
                ".asf",
                ".amv",
                ".m4p",
                ".m4v",
                ".mpg",
                ".mp2",
                ".mpeg",
                ".mpe",
                ".mpv",
                ".mpg",
                ".mpeg",
                ".m2v",
                ".m4v",
                ".svi",
                ".3gp",
                ".3g2",
                ".mxf",
                ".roq",
                ".nsv",
                ".flv",
                ".f4v",
                ".f4p",
                ".f4a",
                ".f4b",
            ]
        )
    )


def bytes_to_size(bytes: int):
    sizes = ["Bytes", "KB", "MB", "GB", "TB"]

    if bytes == 0:
        return "0 Byte"

    i = int(math.floor(math.log(bytes, 1024)))

    return f"{round(bytes / math.pow(1024, i), 2)} {sizes[i]}"


def config_check(b64config: str):
    try:
        config = json.loads(base64.b64decode(b64config).decode())

        if not isinstance(config["debridService"], str) or config[
            "debridService"
        ] not in ["realdebrid"]:
            return False
        if not isinstance(config["debridApiKey"], str):
            return False
        if not isinstance(config["indexers"], list):
            return False
        if not isinstance(config["maxResults"], int) or config["maxResults"] < 0:
            return False
        if (
            not isinstance(config["resolutions"], list)
            or len(config["resolutions"]) == 0
        ):
            return False
        if not isinstance(config["languages"], list) or len(config["languages"]) == 0:
            return False

        return config
    except:
        return False


async def get_indexer_manager(
    session: aiohttp.ClientSession,
    indexer_manager_type: str,
    indexers: list,
    query: str,
):
    try:
        indexers = [indexer.replace("_", " ") for indexer in indexers]

        timeout = aiohttp.ClientTimeout(total=settings.INDEXER_MANAGER_TIMEOUT)
        results = []

        if indexer_manager_type == "jackett":
            response = await session.get(
                f"{settings.INDEXER_MANAGER_URL}/api/v2.0/indexers/all/results?apikey={settings.INDEXER_MANAGER_API_KEY}&Query={query}&Tracker[]={'&Tracker[]='.join(indexer for indexer in indexers)}",
                timeout=timeout,
            )  # &Category[]=2000&Category[]=5000
            response = await response.json()

            for result in response["Results"]:
                results.append(result)

        if indexer_manager_type == "prowlarr":
            get_indexers = await session.get(
                f"{settings.INDEXER_MANAGER_URL}/api/v1/indexer",
                headers={"X-Api-Key": settings.INDEXER_MANAGER_API_KEY},
            )
            get_indexers = await get_indexers.json()

            indexers_id = []
            for indexer in get_indexers:
                if (
                    indexer["name"].lower() in indexers
                    or indexer["definitionName"].lower() in indexers
                ):
                    indexers_id.append(indexer["id"])

            response = await session.get(
                f"{settings.INDEXER_MANAGER_URL}/api/v1/search?query={query}&indexerIds={'&indexerIds='.join(str(indexer_id) for indexer_id in indexers_id)}&type=search",
                headers={  # &categories=2000&categories=5000
                    "X-Api-Key": settings.INDEXER_MANAGER_API_KEY
                },
            )
            response = await response.json()

            for result in response:
                results.append(result)

        return results
    except Exception as e:
        logger.warning(
            f"Exception while getting {indexer_manager_type} results for {query} with {indexers}: {e}"
        )


async def get_torrent_hash(
    session: aiohttp.ClientSession, indexer_manager_type: str, torrent: dict
):
    if "InfoHash" in torrent and torrent["InfoHash"] is not None:
        return torrent["InfoHash"]

    if "infoHash" in torrent:
        return torrent["infoHash"]

    url = (
        torrent["Link"] if indexer_manager_type == "jackett" else torrent["downloadUrl"]
    )

    try:
        timeout = aiohttp.ClientTimeout(total=settings.GET_TORRENT_TIMEOUT)
        response = await session.get(url, allow_redirects=False, timeout=timeout)
        if response.status == 200:
            torrent_data = await response.read()
            torrent_dict = bencodepy.decode(torrent_data)
            info = bencodepy.encode(torrent_dict[b"info"])
            hash = hashlib.sha1(info).hexdigest()
        else:
            location = response.headers.get("Location", "")
            if not location:
                return

            match = info_hash_pattern.search(location)
            if not match:
                return

            hash = match.group(1).upper()

        return hash
    except Exception as e:
        logger.warning(
            f"Exception while getting torrent info hash for {torrent['indexer'] if 'indexer' in torrent else (torrent['Tracker'] if 'Tracker' in torrent else '')}|{url}: {e}"
        )


async def get_balanced_hashes(hashes: dict, config: dict):
    max_results = config["maxResults"]
    config_resolutions = config["resolutions"]
    config_languages = {language.replace("_", " ").capitalize() for language in config["languages"]}
    include_all_languages = "All" in config_languages
    include_all_resolutions = "All" in config_resolutions
    include_unknown_resolution = include_all_resolutions or "Unknown" in config_resolutions

    hashes_by_resolution = {}
    for hash, hash_data in hashes.items():
        hash_info = hash_data["data"]
        if not include_all_languages and not hash_info["is_multi_audio"] and not any(lang in hash_info["language"] for lang in config_languages):
            continue

        resolution = hash_info["resolution"]
        if not resolution:
            if not include_unknown_resolution:
                continue
            resolution_key = "Unknown"
        else:
            resolution_key = resolution[0]
            if not include_all_resolutions and resolution_key not in config_resolutions:
                continue

        if resolution_key not in hashes_by_resolution:
            hashes_by_resolution[resolution_key] = []
        hashes_by_resolution[resolution_key].append(hash)

    if max_results == 0:
        return hashes_by_resolution

    total_resolutions = len(hashes_by_resolution)
    hashes_per_resolution = max_results // total_resolutions
    extra_hashes = max_results % total_resolutions

    balanced_hashes = {}
    for resolution, hash_list in hashes_by_resolution.items():
        selected_count = hashes_per_resolution + (1 if extra_hashes > 0 else 0)
        balanced_hashes[resolution] = hash_list[:selected_count]
        if extra_hashes > 0:
            extra_hashes -= 1

    selected_total = sum(len(hashes) for hashes in balanced_hashes.values())
    if selected_total < max_results:
        missing_hashes = max_results - selected_total
        for resolution, hash_list in hashes_by_resolution.items():
            if missing_hashes <= 0:
                break
            current_count = len(balanced_hashes[resolution])
            available_hashes = hash_list[current_count:current_count + missing_hashes]
            balanced_hashes[resolution].extend(available_hashes)
            missing_hashes -= len(available_hashes)

    return balanced_hashes
