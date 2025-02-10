import aiohttp
import asyncio

from RTN import parse

from comet.utils.general import is_video
from comet.utils.logger import logger


class StremThru:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        video_id: str,
        url: str,
        token: str,
        ip: str,
    ):
        store, token = self.parse_store_creds(token)
        session.headers["X-StremThru-Store-Name"] = store
        session.headers["X-StremThru-Store-Authorization"] = f"Bearer {token}"

        session.headers["User-Agent"] = "comet"

        self.session = session
        self.base_url = f"{url}/v0/store"
        self.name = f"StremThru[{store}]"
        self.client_ip = ip
        self.sid = video_id

    def parse_store_creds(self, token: str):
        if ":" in token:
            parts = token.split(":")
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

        chunk_size = 25
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
                    filename = file["name"]

                    if not is_video(filename) or "sample" in filename:
                        continue

                    filename_parsed = parse(filename)

                    files.append(
                        {
                            "info_hash": torrent["hash"],
                            "index": file["index"],
                            "title": filename,
                            "size": file["size"],
                            "season": filename_parsed.seasons[0]
                            if len(filename_parsed.seasons) != 0
                            else None,
                            "episode": filename_parsed.episodes[0]
                            if len(filename_parsed.episodes) != 0
                            else None,
                            "file_data": filename_parsed,
                        }
                    )

        return files

    async def generate_download_link(self, hash: str, index: str):
        try:
            magnet = await self.session.post(
                f"{self.base_url}/magnets?client_ip={self.client_ip}",
                json={"magnet": f"magnet:?xt=urn:btih:{hash}"},
            )
            magnet = await magnet.json()

            if magnet["data"]["status"] != "downloaded":
                return

            file = next(
                (
                    file
                    for file in magnet["data"]["files"]
                    if str(file["index"]) == index or file["name"] == index
                ),
                None,
            )

            if not file:
                return

            link = await self.session.post(
                f"{self.base_url}/link/generate?client_ip={self.client_ip}",
                json={"link": file["link"]},
            )
            link = await link.json()

            return link["data"]["link"]
        except Exception as e:
            logger.warning(
                f"Exception while getting download link from {self.name} for {hash}|{index}: {e}"
            )
