import aiohttp
import asyncio

from RTN import parse, title_match

from comet.utils.models import settings
from comet.utils.general import is_video
from comet.utils.logger import logger
from comet.utils.torrent import file_index_update_queue


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

    async def get_availability(self, torrent_hashes: list):
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

        files = []
        for result in availability:
            for torrent in result:
                if torrent["status"] != "cached":
                    continue

                for file in torrent["files"]:
                    filename = file["name"].split("/")[-1]

                    if not is_video(filename) or "sample" in filename.lower():
                        continue

                    filename_parsed = parse(filename)

                    hash = torrent["hash"]

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
                        "file_data": filename_parsed,
                    }

                    files.append(file_info)
                    await file_index_update_queue.add_update(hash, season, episode, index, size)

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
                if str(file["index"]) == index:
                    target_file = file
                    break

                filename = file["name"]
                file_parsed = parse(filename)

                if not is_video(filename) or not title_match(
                    name_parsed.parsed_title, file_parsed.parsed_title
                ):
                    continue

                file_season = file_parsed.seasons[0] if file_parsed.seasons else None
                file_episode = file_parsed.episodes[0] if file_parsed.episodes else None
                season = int(season) if season != "n" else None
                episode = int(episode) if episode != "n" else None

                if season == file_season and episode == file_episode:
                    target_file = file
                    break

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
