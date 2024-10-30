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
    ):
        if not self.is_supported_store(debrid_service):
            raise ValueError(f"unsupported store: {debrid_service}")

        if debrid_service == "stremthru":
            session.headers["Proxy-Authorization"] = f"Basic {token}"
        else:
            session.headers["X-StremThru-Store-Name"] = debrid_service
            session.headers["X-StremThru-Store-Authorization"] = f"Bearer {token}"

        session.headers["User-Agent"] = "comet"

        self.session = session
        self.base_url = f"{url}/v0/store"
        self.name = f"StremThru[{debrid_service}]" if debrid_service else "StremThru"

    @staticmethod
    def is_supported_store(name: Optional[str]):
        return (
            name == "stremthru"
            or name == "alldebrid"
            or name == "debridlink"
            or name == "premiumize"
            or name == "realdebrid"
            or name == "torbox"
        )

    async def check_premium(self):
        try:
            user = await self.session.get(f"{self.base_url}/user")
            user = await user.json()
            return user["data"]["subscription_status"] == "premium"
        except Exception as e:
            logger.warning(
                f"Exception while checking premium status on {self.name}: {e}"
            )

        return False

    async def get_instant(self, magnets: list):
        try:
            magnet = await self.session.get(
                f"{self.base_url}/magnets/check?magnet={','.join(magnets)}"
            )
            return await magnet.json()
        except Exception as e:
            logger.warning(
                f"Exception while checking hash instant availability on {self.name}: {e}"
            )

    async def get_files(
        self, torrent_hashes: list, type: str, season: str, episode: str, kitsu: bool
    ):
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
                f"{self.base_url}/magnets",
                json={"magnet": f"magnet:?xt=urn:btih:{hash}"},
            )
            magnet = await magnet.json()

            file = next(
                (
                    file
                    for file in magnet["data"]["files"]
                    if file["index"] == int(index)
                ),
                None,
            )

            if not file:
                return

            link = await self.session.post(
                f"{self.base_url}/link/generate",
                json={"link": file["link"]},
            )
            link = await link.json()

            return link["data"]["link"]
        except Exception as e:
            logger.warning(
                f"Exception while getting download link from {self.name} for {hash}|{index}: {e}"
            )
