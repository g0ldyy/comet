import base64
import hashlib
import re
import aiohttp
import bencodepy
import PTT
import asyncio
import orjson

from RTN import parse, title_match
from curl_cffi import requests
from fastapi import Request
from fuzzywuzzy import fuzz

from comet.utils.logger import logger
from comet.utils.models import settings, ConfigModel

languages_emojis = {
    "multi": "🌎",  # Dubbed
    "en": "🇬🇧",  # English
    "ja": "🇯🇵",  # Japanese
    "zh": "🇨🇳",  # Chinese
    "ru": "🇷🇺",  # Russian
    "ar": "🇸🇦",  # Arabic
    "pt": "🇵🇹",  # Portuguese
    "es": "🇪🇸",  # Spanish
    "fr": "🇫🇷",  # French
    "de": "🇩🇪",  # German
    "it": "🇮🇹",  # Italian
    "ko": "🇰🇷",  # Korean
    "hi": "🇮🇳",  # Hindi
    "bn": "🇧🇩",  # Bengali
    "pa": "🇵🇰",  # Punjabi
    "mr": "🇮🇳",  # Marathi
    "gu": "🇮🇳",  # Gujarati
    "ta": "🇮🇳",  # Tamil
    "te": "🇮🇳",  # Telugu
    "kn": "🇮🇳",  # Kannada
    "ml": "🇮🇳",  # Malayalam
    "th": "🇹🇭",  # Thai
    "vi": "🇻🇳",  # Vietnamese
    "id": "🇮🇩",  # Indonesian
    "tr": "🇹🇷",  # Turkish
    "he": "🇮🇱",  # Hebrew
    "fa": "🇮🇷",  # Persian
    "uk": "🇺🇦",  # Ukrainian
    "el": "🇬🇷",  # Greek
    "lt": "🇱🇹",  # Lithuanian
    "lv": "🇱🇻",  # Latvian
    "et": "🇪🇪",  # Estonian
    "pl": "🇵🇱",  # Polish
    "cs": "🇨🇿",  # Czech
    "sk": "🇸🇰",  # Slovak
    "hu": "🇭🇺",  # Hungarian
    "ro": "🇷🇴",  # Romanian
    "bg": "🇧🇬",  # Bulgarian
    "sr": "🇷🇸",  # Serbian
    "hr": "🇭🇷",  # Croatian
    "sl": "🇸🇮",  # Slovenian
    "nl": "🇳🇱",  # Dutch
    "da": "🇩🇰",  # Danish
    "fi": "🇫🇮",  # Finnish
    "sv": "🇸🇪",  # Swedish
    "no": "🇳🇴",  # Norwegian
    "ms": "🇲🇾",  # Malay
    "la": "💃🏻",  # Latino
}


def get_language_emoji(language: str):
    language_formatted = language.lower()
    return (
        languages_emojis[language_formatted]
        if language_formatted in languages_emojis
        else language
    )


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

    i = 0
    while bytes >= 1024 and i < len(sizes) - 1:
        bytes /= 1024
        i += 1

    return f"{round(bytes, 2)} {sizes[i]}"


def config_check(b64config: str):
    try:
        config = orjson.loads(base64.b64decode(b64config).decode())
        validated_config = ConfigModel(**config)
        return validated_config.model_dump()
    except:
        return False


def get_debrid_extension(debridService: str, debridApiKey: str = None):
    if debridApiKey == "":
        return "TORRENT"

    debrid_extensions = {
        "realdebrid": "RD",
        "alldebrid": "AD",
        "premiumize": "PM",
        "torbox": "TB",
        "debridlink": "DL",
    }

    return debrid_extensions.get(debridService, None)


async def get_indexer_manager(
    session: aiohttp.ClientSession,
    indexer_manager_type: str,
    indexers: list,
    query: str,
):
    results = []
    try:
        indexers = [indexer.replace("_", " ") for indexer in indexers]

        if indexer_manager_type == "jackett":

            async def fetch_jackett_results(
                session: aiohttp.ClientSession, indexer: str, query: str
            ):
                try:
                    async with session.get(
                        f"{settings.INDEXER_MANAGER_URL}/api/v2.0/indexers/all/results?apikey={settings.INDEXER_MANAGER_API_KEY}&Query={query}&Tracker[]={indexer}",
                        timeout=aiohttp.ClientTimeout(
                            total=settings.INDEXER_MANAGER_TIMEOUT
                        ),
                    ) as response:
                        response_json = await response.json()
                        return response_json.get("Results", [])
                except Exception as e:
                    logger.warning(
                        f"Exception while fetching Jackett results for indexer {indexer}: {e}"
                    )
                    return []

            tasks = [
                fetch_jackett_results(session, indexer, query) for indexer in indexers
            ]
            all_results = await asyncio.gather(*tasks)

            for result_set in all_results:
                results.extend(result_set)

        elif indexer_manager_type == "prowlarr":
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
                headers={"X-Api-Key": settings.INDEXER_MANAGER_API_KEY},
            )
            response = await response.json()

            for result in response:
                result["InfoHash"] = (
                    result["infoHash"] if "infoHash" in result else None
                )
                result["Title"] = result["title"]
                result["Size"] = result["size"]
                result["Link"] = (
                    result["downloadUrl"] if "downloadUrl" in result else None
                )
                result["Tracker"] = result["indexer"]

                results.append(result)
    except Exception as e:
        logger.warning(
            f"Exception while getting {indexer_manager_type} results for {query} with {indexers}: {e}"
        )
        pass

    return results


async def get_zilean(
    session: aiohttp.ClientSession, name: str, log_name: str, season: int, episode: int
):
    results = []
    try:
        show = f"&season={season}&episode={episode}"
        get_dmm = await session.get(
            f"{settings.ZILEAN_URL}/dmm/filtered?query={name}{show if season else ''}"
        )
        get_dmm = await get_dmm.json()

        if isinstance(get_dmm, list):
            take_first = get_dmm[: settings.ZILEAN_TAKE_FIRST]
            for result in take_first:
                object = {
                    "Title": result["raw_title"],
                    "InfoHash": result["info_hash"],
                    "Size": result["size"],
                    "Tracker": "DMM",
                }

                results.append(object)

        logger.info(f"{len(results)} torrents found for {log_name} with Zilean")
    except Exception as e:
        logger.warning(
            f"Exception while getting torrents for {log_name} with Zilean: {e}"
        )
        pass

    return results


async def get_torrentio(log_name: str, type: str, full_id: str):
    results = []
    try:
        try:
            get_torrentio = requests.get(
                f"https://torrentio.strem.fun/stream/{type}/{full_id}.json"
            ).json()
        except:
            get_torrentio = requests.get(
                f"https://torrentio.strem.fun/stream/{type}/{full_id}.json",
                proxies={
                    "http": settings.DEBRID_PROXY_URL,
                    "https": settings.DEBRID_PROXY_URL,
                },
            ).json()

        for torrent in get_torrentio["streams"]:
            title = torrent["title"]
            title_full = title.split("\n👤")[0]
            tracker = title.split("⚙️ ")[1].split("\n")[0]

            results.append(
                {
                    "Title": title_full,
                    "InfoHash": torrent["infoHash"],
                    "Size": None,
                    "Tracker": f"Torrentio|{tracker}",
                }
            )

        logger.info(f"{len(results)} torrents found for {log_name} with Torrentio")
    except Exception as e:
        logger.warning(
            f"Exception while getting torrents for {log_name} with Torrentio, your IP is most likely blacklisted (you should try proxying Comet): {e}"
        )
        pass

    return results


async def get_mediafusion(log_name: str, type: str, full_id: str):
    results = []
    try:
        try:
            get_mediafusion = requests.get(
                f"{settings.MEDIAFUSION_URL}/stream/{type}/{full_id}.json"
            ).json()
        except:
            get_mediafusion = requests.get(
                f"{settings.MEDIAFUSION_URL}/stream/{type}/{full_id}.json",
                proxies={
                    "http": settings.DEBRID_PROXY_URL,
                    "https": settings.DEBRID_PROXY_URL,
                },
            ).json()

        for torrent in get_mediafusion["streams"]:
            title_full = torrent["description"]
            title = title_full.split("\n")[0] if "\n" in title_full else title_full
            tracker = title_full.split("🔗 ")[1] if "🔗" in title_full else "Unknown"

            results.append(
                {
                    "Title": title,
                    "InfoHash": torrent["infoHash"],
                    "Size": torrent["behaviorHints"][
                        "videoSize"
                    ],  # not the pack size but still useful for prowlarr userss
                    "Tracker": f"MediaFusion|{tracker}",
                }
            )

        logger.info(f"{len(results)} torrents found for {log_name} with MediaFusion")

    except Exception as e:
        logger.warning(
            f"Exception while getting torrents for {log_name} with MediaFusion, your IP is most likely blacklisted (you should try proxying Comet): {e}"
        )
        pass

    return results

def match_titles(imdb_title, torrent_title, threshold=80, token_overlap_threshold=0.5):
    """
    Match movie/TV show titles using a combination of fuzzy string matching and token overlap.

    Parameters:
    imdb_title (str): The title from the IMDB data source.
    torrent_title (str): The title from the torrent data source.
    threshold (int): The minimum fuzzy match ratio to consider the titles a match.
    token_overlap_threshold (float): The minimum proportion of overlapping tokens to consider the titles a match.

    Returns:
    bool: True if the titles match, False otherwise.
    """
    # Calculate the fuzzy match ratio
    match_ratio = fuzz.token_set_ratio(imdb_title, torrent_title)

    # Calculate the proportion of overlapping tokens
    imdb_tokens = set(imdb_title.lower().split())
    torrent_tokens = set(torrent_title.lower().split())
    common_tokens = imdb_tokens.intersection(torrent_tokens)
    token_overlap_ratio = len(common_tokens) / max(len(imdb_tokens), len(torrent_tokens))

    # Check if both the fuzzy match ratio and token overlap ratio meet the thresholds
    return match_ratio >= threshold and token_overlap_ratio >= token_overlap_threshold

async def filter(torrents: list, name: str, year: int):
    results = []
    for torrent in torrents:
        index = torrent[0]
        title = torrent[1]

        if "\n" in title:  # Torrentio title parsing
            title = title.split("\n")[1]

        parsed = parse(title)

        if parsed.parsed_title and not match_titles(name, parsed.parsed_title):
            results.append((index, False))
            continue

        if year and parsed.year and year != parsed.year:
            results.append((index, False))
            continue

        results.append((index, True))

    return results


async def get_torrent_hash(session: aiohttp.ClientSession, torrent: tuple):
    index = torrent[0]
    torrent = torrent[1]
    if "InfoHash" in torrent and torrent["InfoHash"] is not None:
        return (index, torrent["InfoHash"].lower())

    url = torrent["Link"]

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
                return (index, None)

            match = info_hash_pattern.search(location)
            if not match:
                return (index, None)

            hash = match.group(1).upper()

        return (index, hash.lower())
    except Exception as e:
        logger.warning(
            f"Exception while getting torrent info hash for {torrent['indexer'] if 'indexer' in torrent else (torrent['Tracker'] if 'Tracker' in torrent else '')}|{url}: {e}"
        )

        return (index, None)


def get_balanced_hashes(hashes: dict, config: dict):
    max_results = config["maxResults"]

    max_size = config["maxSize"]
    config_resolutions = [resolution.lower() for resolution in config["resolutions"]]
    include_all_resolutions = "all" in config_resolutions

    languages = [language.lower() for language in config["languages"]]
    include_all_languages = "all" in languages
    if not include_all_languages:
        config_languages = [
            code
            for code, name in PTT.parse.LANGUAGES_TRANSLATION_TABLE.items()
            if name.lower() in languages
        ]

    hashes_by_resolution = {}
    for hash, hash_data in hashes.items():
        hash_info = hash_data["data"]

        if max_size != 0 and hash_info["size"] > max_size:
            continue

        if (
            not include_all_languages
            and not any(lang in hash_info["languages"] for lang in config_languages)
            and ("multi" not in languages if hash_info["dubbed"] else True)
        ):
            continue

        resolution = hash_info["resolution"]
        if not include_all_resolutions and resolution not in config_resolutions:
            continue

        if resolution not in hashes_by_resolution:
            hashes_by_resolution[resolution] = []
        hashes_by_resolution[resolution].append(hash)

    total_resolutions = len(hashes_by_resolution)
    if max_results == 0 or total_resolutions == 0:
        return hashes_by_resolution

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
            available_hashes = hash_list[current_count : current_count + missing_hashes]
            balanced_hashes[resolution].extend(available_hashes)
            missing_hashes -= len(available_hashes)

    return balanced_hashes


def format_metadata(data: dict):
    extras = []
    if data["quality"]:
        extras.append(data["quality"])
    if data["hdr"]:
        extras.extend(data["hdr"])
    if data["codec"]:
        extras.append(data["codec"])
    if data["audio"]:
        extras.extend(data["audio"])
    if data["channels"]:
        extras.extend(data["channels"])
    if data["bit_depth"]:
        extras.append(data["bit_depth"])
    if data["network"]:
        extras.append(data["network"])
    if data["group"]:
        extras.append(data["group"])

    return "|".join(extras)


def format_title(data: dict, config: dict):
    title = ""
    if "All" in config["resultFormat"] or "Title" in config["resultFormat"]:
        title += f"{data['title']}\n"

    if "All" in config["resultFormat"] or "Metadata" in config["resultFormat"]:
        metadata = format_metadata(data)
        if metadata != "":
            title += f"💿 {metadata}\n"

    if "All" in config["resultFormat"] or "Size" in config["resultFormat"]:
        title += f"💾 {bytes_to_size(data['size'])} "

    if "All" in config["resultFormat"] or "Tracker" in config["resultFormat"]:
        title += f"🔎 {data['tracker'] if 'tracker' in data else '?'}"

    if "All" in config["resultFormat"] or "Languages" in config["resultFormat"]:
        languages = data["languages"]
        if data["dubbed"]:
            languages.insert(0, "multi")
        formatted_languages = (
            "/".join(get_language_emoji(language) for language in languages)
            if languages
            else None
        )
        languages_str = "\n" + formatted_languages if formatted_languages else ""
        title += f"{languages_str}"

    if title == "":
        # Without this, Streamio shows SD as the result, which is confusing
        title = "Empty result format configuration"

    return title


def get_client_ip(request: Request):
    return (
        request.headers["cf-connecting-ip"]
        if "cf-connecting-ip" in request.headers
        else request.client.host
    )
