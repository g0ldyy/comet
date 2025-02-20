import aiohttp
import asyncio
import time
import orjson

from RTN import parse, title_match

from comet.utils.models import settings, database
from comet.utils.general import is_video, default_dump
from comet.utils.logger import logger
from comet.utils.torrent import torrent_update_queue


class StremThru:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        video_id: str,
        token: str,
        ip: str,
    ):
        store, token = self.parse_store_creds(token)
        session.headers["X-StremThru-Store-Name"] = store
        session.headers["X-StremThru-Store-Authorization"] = f"Bearer {token}"

        session.headers["User-Agent"] = "comet"

        self.session = session
        self.base_url = f"{settings.STREMTHRU_URL}/v0/store"
        self.name = f"StremThru-{store}"
        self.real_debrid_name = store
        self.client_ip = ip
        self.sid = video_id

    def parse_store_creds(self, token: str):
        if ":" in token:
            parts = token.split(":", 1)
            return parts[0], parts[1]

        return token, ""

    async def check_premium(self):
        try:
            user = await self.session.get(
                f"{self.base_url}/user?client_ip={self.client_ip}"
            )
            user = await user.json()
            return user["data"]["subscription_status"] == "premium"
        except Exception as e:
            logger.warning(
                f"Exception while checking premium status on {self.name}: {e}"
            )

        return False

    async def get_instant(self, magnets: list):
        try:
            url = f"{self.base_url}/magnets/check?magnet={','.join(magnets)}&client_ip={self.client_ip}&sid={self.sid}"
            magnet = await self.session.get(url)
            return await magnet.json()
        except Exception as e:
            logger.warning(
                f"Exception while checking hash instant availability on {self.name}: {e}"
            )

    async def get_availability(
        self,
        torrent_hashes: list,
        seeders_map: dict,
        tracker_map: dict,
        sources_map: dict,
    ):
        if not await self.check_premium():
            return []

        media_id = self.sid.split(":")[0] if ":" in self.sid else self.sid

        chunk_size = 50
        chunks = [
            torrent_hashes[i : i + chunk_size]
            for i in range(0, len(torrent_hashes), chunk_size)
        ]

        tasks = []
        for chunk in chunks:
            tasks.append(self.get_instant(chunk))

        responses = await asyncio.gather(*tasks)

        availability = [
            response["data"]["items"]
            for response in responses
            if response and "data" in response
        ]

        files = []
        cached_count = 0
        for result in availability:
            for torrent in result:
                if torrent["status"] != "cached":
                    continue

                cached_count += 1
                hash = torrent["hash"]
                seeders = seeders_map[hash]
                tracker = tracker_map[hash]
                sources = sources_map[hash]

                for file in torrent["files"]:
                    filename = file["name"].split("/")[-1]

                    if not is_video(filename) or "sample" in filename.lower():
                        continue

                    filename_parsed = parse(filename)

                    season = (
                        filename_parsed.seasons[0] if filename_parsed.seasons else None
                    )
                    episode = (
                        filename_parsed.episodes[0]
                        if filename_parsed.episodes
                        else None
                    )
                    if ":" in self.sid and (season is None or episode is None):
                        continue

                    index = file["index"]
                    size = file["size"]

                    file_info = {
                        "info_hash": hash,
                        "index": index,
                        "title": filename,
                        "size": size,
                        "season": season,
                        "episode": episode,
                        "parsed": filename_parsed,
                        "seeders": seeders,
                        "tracker": tracker,
                        "sources": sources,
                    }

                    files.append(file_info)
                    await torrent_update_queue.add_torrent_info(file_info, media_id)

        logger.log(
            "SCRAPER",
            f"{self.name}: Found {cached_count} cached torrents with {len(files)} valid files",
        )
        return files

    async def generate_download_link(
        self, hash: str, index: str, name: str, season: int, episode: int
    ):
        try:
            magnet = await self.session.post(
                f"{self.base_url}/magnets?client_ip={self.client_ip}",
                json={"magnet": f"magnet:?xt=urn:btih:{hash}"},
            )
            magnet = await magnet.json()

            if magnet["data"]["status"] != "downloaded":
                return

            name_parsed = parse(name)
            target_file = None

            for file in magnet["data"]["files"]:
                filename = file["name"]
                file_parsed = parse(filename)

                file_season = file_parsed.seasons[0] if file_parsed.seasons else None
                file_episode = file_parsed.episodes[0] if file_parsed.episodes else None

                if str(file["index"]) == index:
                    target_file = file
                    break

                if not is_video(filename) or not title_match(
                    name_parsed.parsed_title, file_parsed.parsed_title
                ):
                    continue

                if season == file_season and episode == file_episode:
                    target_file = file
                    break

            if not target_file:
                return

            await database.execute(
                f"""
                INSERT {"OR IGNORE " if settings.DATABASE_TYPE == "sqlite" else ""}
                INTO debrid_availability (debrid_service, info_hash, file_index, title, season, episode, size, parsed, timestamp)
                VALUES (:debrid_service, :info_hash, :file_index, :title, :season, :episode, :size, :parsed, :timestamp)
                {" ON CONFLICT DO NOTHING" if settings.DATABASE_TYPE == "postgresql" else ""}
                """,
                {
                    "debrid_service": self.real_debrid_name,
                    "info_hash": hash,
                    "file_index": str(target_file["index"]),
                    "title": target_file["name"],
                    "season": season,
                    "episode": episode,
                    "size": target_file["size"],
                    "parsed": orjson.dumps(file_parsed, default=default_dump).decode(
                        "utf-8"
                    ),
                    "timestamp": time.time(),
                },
            )
            # await file_index_update_queue.add_update(
            #     hash,
            #     season,
            #     episode,
            #     target_file["index"],
            #     target_file["name"],
            #     target_file["size"],
            #     parsed,
            # )

            link = await self.session.post(
                f"{self.base_url}/link/generate?client_ip={self.client_ip}",
                json={"link": target_file["link"]},
            )
            link = await link.json()

            return link["data"]["link"]
        except Exception as e:
            logger.warning(f"Exception while getting download link for {hash}: {e}")
