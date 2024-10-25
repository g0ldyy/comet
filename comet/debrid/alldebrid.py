import aiohttp
import asyncio
from urllib.parse import quote_plus

from RTN import parse

from comet.utils.general import is_video
from comet.utils.logger import logger
from comet.utils.request import RequestClient


class AllDebrid:
    def __init__(self, session: aiohttp.ClientSession, debrid_api_key: str):
        session.headers["Authorization"] = f"Bearer {debrid_api_key}"
        self.client = RequestClient(session, base_url="http://api.alldebrid.com/v4")

        self.agent = "comet"

    async def check_premium(self):
        try:
            check_premium = await self.client.request(
                "get", f"/user?agent={self.agent}"
            )
            check_premium = await check_premium.text()
            if '"isPremium":true' in check_premium:
                return True
        except Exception as e:
            logger.warning(
                f"Exception while checking premium status on All-Debrid: {e}"
            )

        return False

    async def get_instant(self, chunk: list):
        try:
            get_instant = await self.client.request(
                "get",
                f"/magnet/instant?agent={self.agent}&magnets[]={'&magnets[]='.join(chunk)}",
            )
            return await get_instant.json()
        except Exception as e:
            logger.warning(
                f"Exception while checking hashes instant availability on All-Debrid: {e}"
            )

    async def get_files(
        self, torrent_hashes: list, type: str, season: str, episode: str, kitsu: bool
    ):
        chunk_size = 500
        chunks = [
            torrent_hashes[i : i + chunk_size]
            for i in range(0, len(torrent_hashes), chunk_size)
        ]

        tasks = []
        for chunk in chunks:
            tasks.append(self.get_instant(chunk))

        responses = await asyncio.gather(*tasks)

        availability = [response for response in responses if response]

        files = {}

        if type == "series":
            for result in availability:
                if "status" not in result or result["status"] != "success":
                    continue

                for magnet in result["data"]["magnets"]:
                    if not magnet["instant"]:
                        continue

                    for file in magnet["files"]:
                        filename = file["n"]
                        pack = False
                        if "e" in file:  # PACK
                            filename = file["e"][0]["n"]
                            pack = True

                        if not is_video(filename):
                            continue

                        if "sample" in filename:
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
                            "index": magnet["files"].index(file),
                            "title": filename,
                            "size": file["e"][0]["s"] if pack else file["s"],
                        }

                        break
        else:
            for result in availability:
                if "status" not in result or result["status"] != "success":
                    continue

                for magnet in result["data"]["magnets"]:
                    if not magnet["instant"]:
                        continue

                    for file in magnet["files"]:
                        filename = file["n"]

                        if not is_video(filename):
                            continue

                        if "sample" in filename:
                            continue

                        files[magnet["hash"]] = {
                            "index": magnet["files"].index(file),
                            "title": filename,
                            "size": file["s"],
                        }

                        break

        return files

    async def generate_download_link(self, hash: str, index: str):
        try:
            check_blacklisted = await self.client.request(
                "get", f"/magnet/upload?agent=comet&magnets[]={hash}"
            )
            check_blacklisted = await check_blacklisted.text()
            if "NO_SERVER" in check_blacklisted:
                if not self.client.enable_proxy():
                    logger.warning(
                        "All-Debrid blacklisted server's IP. No proxy found."
                    )
                else:
                    logger.warning(
                        f"All-Debrid blacklisted server's IP. Switching to proxy {self.client.proxy} for {hash}|{index}"
                    )

            upload_magnet = await self.client.request(
                "get",
                f"/magnet/upload?agent=comet&magnets[]={hash}",
            )
            upload_magnet = await upload_magnet.json()

            get_magnet_status = await self.client.request(
                "get",
                f"/magnet/status?agent=comet&id={upload_magnet['data']['magnets'][0]['id']}",
            )
            get_magnet_status = await get_magnet_status.json()

            unlock_link = await self.client.request(
                "get",
                f"/link/unlock?agent=comet&link={quote_plus(get_magnet_status['data']['magnets']['links'][int(index)]['link'])}",
            )
            unlock_link = await unlock_link.json()

            return unlock_link["data"]["link"]
        except Exception as e:
            logger.warning(
                f"Exception while getting download link from All-Debrid for {hash}|{index}: {e}"
            )
