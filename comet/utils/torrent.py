import hashlib
import re
import bencodepy
import aiohttp
from urllib.parse import parse_qs, urlparse

from comet.utils.logger import logger
from comet.utils.models import settings
from comet.utils.general import is_video

info_hash_pattern = re.compile(r"btih:([a-fA-F0-9]{40})")
episode_pattern = re.compile(
    r"(?:s(\d{1,2})e(\d{1,2})|(\d{1,2})x(\d{1,2}))", re.IGNORECASE
)


def extract_trackers_from_magnet(magnet_uri: str):
    try:
        parsed = urlparse(magnet_uri)
        params = parse_qs(parsed.query)
        return params.get("tr", [])
    except:
        return []


def normalize_name(name: str):
    name = name.lower()
    name = re.sub(r"[\s._-]+", ".", name)
    name = re.sub(r"\[.*?\]|\(.*?\)|\{.*?\}", "", name)
    name = re.sub(r"[^a-z0-9.]", "", name)
    name = re.sub(r"\.+", ".", name)
    name = name.strip(".")
    return name


def find_best_video_file(files: list, torrent_name: str, reference_title: str = None):
    best_size = 0
    best_index = 0
    best_score = -1

    ref_season = ref_episode = None
    if reference_title:
        match = episode_pattern.search(reference_title)
        if match:
            groups = match.groups()
            if groups[0] is not None:
                ref_season, ref_episode = int(groups[0]), int(groups[1])
            elif groups[2] is not None:
                ref_season, ref_episode = int(groups[2]), int(groups[3])

    ref_title_norm = normalize_name(reference_title) if reference_title else None
    torrent_name_norm = normalize_name(torrent_name) if torrent_name else None

    for idx, file in enumerate(files):
        try:
            if b"path" in file:
                path_parts = [part.decode() for part in file[b"path"]]
                path = "/".join(path_parts)
            else:
                path = file[b"name"].decode() if b"name" in file else ""

            if not path:
                continue

            if not is_video(path):
                continue

            path_norm = normalize_name(path)
            size = file[b"length"]
            score = size

            if ref_season is not None and ref_episode is not None:
                match = episode_pattern.search(path)
                if match:
                    groups = match.groups()
                    if groups[0] is not None:
                        season, episode = int(groups[0]), int(groups[1])
                    elif groups[2] is not None:
                        season, episode = int(groups[2]), int(groups[3])
                    else:
                        season = episode = None

                    if season == ref_season and episode == ref_episode:
                        score *= 3

            if ref_title_norm:
                clean_path = episode_pattern.sub("", path_norm)
                clean_ref = episode_pattern.sub("", ref_title_norm)

                if clean_ref in clean_path:
                    score *= 2

            if torrent_name_norm and torrent_name_norm in path_norm:
                score *= 1.5

            if "/" not in path:
                score *= 1.1

            score *= 1 + (size / (1024 * 1024 * 1024))

            if score > best_score:
                best_score = score
                best_size = size
                best_index = idx

        except Exception as e:
            logger.warning(f"Failed to find best video file: {e}")
            continue

    return best_index, best_size


async def download_torrent(session: aiohttp.ClientSession, url: str):
    try:
        timeout = aiohttp.ClientTimeout(total=settings.GET_TORRENT_TIMEOUT)
        async with session.get(url, allow_redirects=False, timeout=timeout) as response:
            if response.status == 200:
                return (await response.read(), None, None)

            location = response.headers.get("Location", "")
            if location:
                match = info_hash_pattern.search(location)
                if match:
                    return (None, match.group(1), location)
            return (None, None, None)
    except Exception as e:
        logger.warning(f"Failed to download torrent from {url}: {e}")
        return (None, None, None)


def extract_torrent_metadata(content: bytes, reference_title: str = None):
    try:
        torrent_data = bencodepy.decode(content)
        info = torrent_data[b"info"]
        info_encoded = bencodepy.encode(info)
        m = hashlib.sha1()
        m.update(info_encoded)
        info_hash = m.hexdigest()

        torrent_name = info.get(b"name", b"").decode()
        if not torrent_name:
            return {}

        if b"files" in info:
            files = info[b"files"]
            best_index, total_size = find_best_video_file(
                files, torrent_name, reference_title
            )
        else:
            total_size = info[b"length"]
            best_index = 0

            name = info[b"name"].decode()
            if not is_video(name):
                return {}

        announce_list = [
            tracker[0].decode() for tracker in torrent_data.get(b"announce-list", [])
        ]

        metadata = {
            "info_hash": info_hash.lower(),
            "announce_list": announce_list,
            "total_size": total_size,
            "torrent_name": torrent_name,
            "file_index": best_index,
            "torrent_file": content,
        }
        return metadata

    except Exception as e:
        logger.warning(f"Failed to extract torrent metadata: {e}")
        return {}
