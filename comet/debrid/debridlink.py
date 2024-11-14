import aiohttp
import asyncio

from RTN import parse

from comet.utils.general import is_video
from comet.utils.logger import logger


class DebridLink:
    def __init__(self, session: aiohttp.ClientSession, debrid_api_key: str):
        session.headers["Authorization"] = f"Bearer {debrid_api_key}"
        self.session = session
        self.proxy = None

        self.api_url = "https://debrid-link.com/api/v2"

    async def check_premium(self):
        try:
            check_premium = await self.session.get(f"{self.api_url}/account/infos")
            check_premium = await check_premium.text()
            if '"accountType":1' in check_premium:
                return True
        except Exception as e:
            logger.warning(
                f"Exception while checking premium status on Debrid-Link: {e}"
            )

        return False

    async def get_instant(self, chunk: list):
        try:
            get_instant = await self.session.get(
                f"{self.api_url}/seedbox/cached?url={','.join(chunk)}"
            )
            return await get_instant.json()
        except Exception as e:
            logger.warning(
                f"Exception while checking hashes instant availability on Debrid-Link: {e}"
            )

    async def get_files(
        self, torrent_hashes: list, type: str, season: str, episode: str, kitsu: bool
    ):
        chunk_size = 250
        chunks = [
            torrent_hashes[i : i + chunk_size]
            for i in range(0, len(torrent_hashes), chunk_size)
        ]

        tasks = []
        for chunk in chunks:
            tasks.append(self.get_instant(chunk))

        responses = await asyncio.gather(*tasks)

        availability = [
            response for response in responses if response and response.get("success")
        ]

        files = {}

        if type == "series":
            for result in availability:
                for hash, torrent_data in result["value"].items():
                    for file in torrent_data["files"]:
                        filename = file["name"]

                        if not is_video(filename):
                            continue

                        if "sample" in filename.lower():
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

                        files[hash] = {
                            "index": torrent_data["files"].index(file),
                            "title": filename,
                            "size": file["size"],
                        }

                        break
        else:
            for result in availability:
                for hash, torrent_data in result["value"].items():
                    for file in torrent_data["files"]:
                        filename = file["name"]

                        if not is_video(filename):
                            continue

                        if "sample" in filename.lower():
                            continue

                        files[hash] = {
                            "index": torrent_data["files"].index(file),
                            "title": filename,
                            "size": file["size"],
                        }

                        break

        return files

    async def generate_download_link(self, hash: str, index: str):
        try:
            add_torrent = await self.session.post(
                f"{self.api_url}/seedbox/add", data={"url": hash, "async": True}
            )
            add_torrent = await add_torrent.json()

            return add_torrent["value"]["files"][int(index)]["downloadUrl"]
        except Exception as e:
            logger.warning(
                f"Exception while getting download link from Debrid-Link for {hash}|{index}: {e}"
            )
