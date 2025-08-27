from comet.utils.general import (
    log_scraper_error,
    fetch_with_proxy_fallback,
)
from comet.utils.mediafusion import mediafusion_config, encode_mediafusion_api_password


async def get_mediafusion(manager, url: str, api_password: str | None):
    torrents = []
    try:
        if api_password is not None:
            encoded_user_data = encode_mediafusion_api_password(api_password)
            headers = {"encoded_user_data": encoded_user_data}
        else:
            headers = mediafusion_config.headers

        get_mediafusion = await fetch_with_proxy_fallback(
            f"{url}/stream/{manager.media_type}/{manager.media_id}.json",
            headers=headers,
        )

        for torrent in get_mediafusion["streams"]:
            title_full = torrent["description"]
            lines = title_full.split("\n")

            title = lines[0].replace("📂 ", "").replace("/", "")

            seeders = None
            if "👤" in lines[1]:
                seeders = int(lines[1].split("👤 ")[1].split("\n")[0])

            tracker = lines[-1].split("🔗 ")[1]

            torrents.append(
                {
                    "title": title,
                    "infoHash": torrent["infoHash"].lower(),
                    "fileIndex": torrent["fileIdx"] if "fileIdx" in torrent else None,
                    "seeders": seeders,
                    "size": torrent["behaviorHints"][
                        "videoSize"
                    ],  # not the pack size but still useful for prowlarr userss
                    "tracker": f"MediaFusion|{tracker}",
                    "sources": torrent["sources"] if "sources" in torrent else [],
                }
            )
    except Exception as e:
        log_scraper_error("MediaFusion", url, manager.media_id, e)
        pass

    await manager.filter_manager(torrents)
