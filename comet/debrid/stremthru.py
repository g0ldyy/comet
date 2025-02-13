import asyncio
from typing import Optional

import aiohttp
from RTN import parse

from comet.utils.general import is_video
from comet.utils.logger import logger


class StremThru:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        url: str,
        token: str,
        debrid_service: str,
        ip: str,
    ):
        if not self.is_supported_store(debrid_service):
            raise ValueError(f"unsupported store: {debrid_service}")

        store, token = self.parse_store_creds(debrid_service, token)
        if store == "stremthru":
            session.headers["Proxy-Authorization"] = f"Basic {token}"
        else:
            session.headers["X-StremThru-Store-Name"] = store
            session.headers["X-StremThru-Store-Authorization"] = f"Bearer {token}"

        session.headers["User-Agent"] = "comet"

        self.session = session
        self.base_url = f"{url}/v0/store"
        self.name = f"StremThru[{debrid_service}]" if debrid_service else "StremThru"
        self.client_ip = ip

    @staticmethod
    def parse_store_creds(debrid_service, token: str = ""):
        if debrid_service != "stremthru":
            return debrid_service, token
        if ":" in token:
            parts = token.split(":")
            return parts[0], parts[1]
        return debrid_service, token

    @staticmethod
    def is_supported_store(name: Optional[str]):
        return (
            name == "stremthru"
            or name == "alldebrid"
            or name == "debridlink"
            or name == "easydebrid"
            or name == "premiumize"
            or name == "realdebrid"
            or name == "torbox"
        )

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

    async def get_instant(self, magnets: list, sid: Optional[str] = None):
        try:
            url = f"{self.base_url}/magnets/check?magnet={','.join(magnets)}&client_ip={self.client_ip}"
            if sid:
                url = f"{url}&sid={sid}"
            magnet = await self.session.get(url)
            return await magnet.json()
        except Exception as e:
            logger.warning(
                f"Exception while checking hash instant availability on {self.name}: {e}"
            )

    async def get_files(
        self,
        torrent_hashes: list,
        type: str,
        season: str,
        episode: str,
        kitsu: bool,
        video_id: Optional[str] = None,
        **kwargs,
    ):
        chunk_size = 25
        chunks = [
            torrent_hashes[i : i + chunk_size]
            for i in range(0, len(torrent_hashes), chunk_size)
        ]

        tasks = []
        for chunk in chunks:
            tasks.append(self.get_instant(chunk, sid=video_id))

        responses = await asyncio.gather(*tasks)

        availability = [
            response["data"]["items"]
            for response in responses
            if response and "data" in response
        ]

        files = {}

        if type == "series":
            for magnets in availability:
                for magnet in magnets:
                    if magnet["status"] != "cached":
                        continue

                    for file in magnet["files"]:
                        filename = file["name"]

                        if not is_video(filename) or "sample" in filename:
                            continue

                        filename_parsed = parse(filename)

                        if episode not in filename_parsed.episodes:
                            continue

                        if kitsu:
                            if filename_parsed.seasons:
                                continue
                        else:
                            if season not in filename_parsed.seasons:
                                continue

                        files[magnet["hash"]] = {
                            "index": file["index"],
                            "title": filename,
                            "size": file["size"],
                        }

                        break
        else:
            for magnets in availability:
                for magnet in magnets:
                    if magnet["status"] != "cached":
                        continue

                    for file in magnet["files"]:
                        filename = file["name"]

                        if not is_video(filename) or "sample" in filename:
                            continue

                        files[magnet["hash"]] = {
                            "index": file["index"],
                            "title": filename,
                            "size": file["size"],
                        }

                        break

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
