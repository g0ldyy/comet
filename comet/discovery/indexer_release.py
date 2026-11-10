import asyncio

from comet.discovery.torrent_models import ScrapeResult
from comet.services.torrent_manager import (
    add_torrent_queue,
    download_torrent,
    extract_torrent_metadata,
    extract_trackers_from_magnet,
)
from comet.usenet.outbound import configured_http_origin


async def resolve_indexer_release(
    indexer_url: str,
    *,
    title: str,
    seeders: int | str | None,
    size: int | None,
    tracker: str,
    is_private: bool,
    info_hash: str | None,
    magnet_url: str | None,
    download_url: str | None,
    grab_known_hash: bool,
    media_id: str,
    season: int | None,
) -> list[ScrapeResult]:
    """Turn a Jackett/Prowlarr release into scrape results.

    The .torrent yields per-file results but costs a grab through the indexer
    manager; ``grab_known_hash`` decides whether that is worth it when the
    release already exposes its info hash (Prowlarr rate-limits every grab).
    """

    seeders = int(seeders) if seeders is not None else None
    base: ScrapeResult = {
        "title": title,
        "infoHash": None,
        "fileIndex": None,
        "seeders": seeders,
        "size": size,
        "tracker": tracker,
        "sources": [],
        "isPrivate": is_private,
    }

    if download_url and (grab_known_hash or not info_hash):
        content, grabbed_hash, grabbed_magnet = await download_torrent(
            download_url,
            allowed_private_origins=frozenset({configured_http_origin(indexer_url)}),
        )
        if content and (
            metadata := await asyncio.to_thread(extract_torrent_metadata, content)
        ):
            info_hash = metadata["info_hash"].lower()
            return [
                {
                    **base,
                    "title": file["title"],
                    "infoHash": info_hash,
                    "fileIndex": file["index"],
                    "size": file["size"],
                    "sources": metadata["sources"],
                    "isPrivate": is_private or metadata["is_private"],
                }
                for file in metadata["files"]
            ]
        if grabbed_hash:
            info_hash, magnet_url = grabbed_hash, grabbed_magnet

    if not info_hash:
        return []

    base["infoHash"] = info_hash = info_hash.lower()
    if magnet_url:
        base["sources"] = extract_trackers_from_magnet(magnet_url)
        await add_torrent_queue.add_torrent(
            magnet_url,
            seeders,
            tracker,
            media_id,
            season,
            info_hash,
            is_private=is_private,
        )
    return [base]
