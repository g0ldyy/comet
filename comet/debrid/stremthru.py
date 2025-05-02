import aiohttp
import asyncio

from RTN import parse, title_match
from urllib.parse import unquote

from comet.utils.models import settings
from comet.utils.general import is_video
from comet.utils.debrid import cache_availability
from comet.utils.logger import logger
from comet.utils.torrent import torrent_update_queue


class StremThru:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        video_id: str,
        media_only_id: str,
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
        self.media_only_id = media_only_id

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

        is_offcloud = self.real_debrid_name == "offcloud"

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

                if is_offcloud:
                    file_info = {
                        "info_hash": hash,
                        "index": None,
                        "title": None,
                        "size": None,
                        "season": None,
                        "episode": None,
                        "parsed": None,
                    }

                    files.append(file_info)
                else:
                    for file in torrent["files"]:
                        filename = file["name"].split("/")[-1]

                        if not is_video(filename) or "sample" in filename.lower():
                            continue

                        filename_parsed = parse(filename)

                        season = (
                            filename_parsed.seasons[0]
                            if filename_parsed.seasons
                            else None
                        )
                        episode = (
                            filename_parsed.episodes[0]
                            if filename_parsed.episodes
                            else None
                        )
                        if ":" in self.sid and (season is None or episode is None):
                            continue

                        index = file["index"] if file["index"] != -1 else None
                        size = file["size"] if file["size"] != -1 else None

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
                        await torrent_update_queue.add_torrent_info(
                            file_info, self.media_only_id
                        )

        logger.log(
            "SCRAPER",
            f"{self.name}: Found {cached_count} cached torrents with {len(files)} valid files",
        )
        return files

    async def generate_download_link(
        self,
        hash: str,
        index: str,
        name: str,
        torrent_name: str,
        season: int,
        episode: int,
    ):
        try:
            magnet = await self.session.post(
                f"{self.base_url}/magnets?client_ip={self.client_ip}",
                json={"magnet": f"magnet:?xt=urn:btih:{hash}"},
            )
            magnet = await magnet.json()

            if magnet["data"]["status"] != "downloaded":
                return
            
            name = unquote(name)
            torrent_name = unquote(torrent_name)

            name_parsed = parse(name)
            target_file = None

            debrid_files = magnet["data"]["files"]
            debrid_files_parsed = []
            files = []
            for file in debrid_files:
                filename = file["name"]

                if "sample" in filename.lower():
                    continue

                if not is_video(filename):
                    continue

                filename_parsed = parse(filename)

                file_season = (
                    filename_parsed.seasons[0] if filename_parsed.seasons else None
                )
                file_episode = (
                    filename_parsed.episodes[0] if filename_parsed.episodes else None
                )
                file_index = file["index"] if file["index"] != -1 else None
                file_size = file["size"] if file["size"] != -1 else None

                file = {
                    "index": file_index,
                    "title": filename,
                    "size": file_size,
                    "season": file_season,
                    "episode": file_episode,
                    "link": file["link"] if "link" in file else None,
                }

                debrid_files_parsed.append(file)

                if not title_match(
                    name_parsed.parsed_title, filename_parsed.parsed_title
                ):
                    continue

                file["info_hash"] = hash
                file["parsed"] = filename_parsed
                files.append(file)

            for file in debrid_files_parsed:
                if file["title"] == torrent_name:
                    target_file = file
                    break

                if season == file["season"] and episode == file["episode"]:
                    target_file = file
                    break

            if not target_file:
                for file in files:
                    if str(file["index"]) == index:
                        target_file = file
                        break

            if len(files) > 0:
                asyncio.create_task(cache_availability(self.real_debrid_name, files))

            if not target_file and len(debrid_files) > 0:
                files_with_link = [
                    file for file in debrid_files if "link" in file and file["link"]
                ]
                if len(files_with_link) > 0:
                    target_file = max(files_with_link, key=lambda x: x["size"])

            if not target_file:
                return

            link = await self.session.post(
                f"{self.base_url}/link/generate?client_ip={self.client_ip}",
                json={"link": target_file["link"]},
            )
            link = await link.json()

            return link["data"]["link"]
        except Exception as e:
            logger.warning(f"Exception while getting download link for {hash}: {e}")
