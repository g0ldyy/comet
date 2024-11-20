import aiohttp
import asyncio

from RTN import parse

from comet.utils.general import is_video
from comet.utils.logger import logger
from comet.utils.models import settings


class RealDebrid:
    def __init__(self, session: aiohttp.ClientSession, debrid_api_key: str, ip: str):
        session.headers["Authorization"] = f"Bearer {debrid_api_key}"
        self.session = session
        self.ip = ip
        self.proxy = None

        self.api_url = "https://api.real-debrid.com/rest/1.0"

    async def check_premium(self):
        try:
            check_premium = await self.session.get(f"{self.api_url}/user")
            check_premium = await check_premium.text()
            if '"type": "premium"' in check_premium:
                return True
        except Exception as e:
            logger.warning(
                f"Exception while checking premium status on Real-Debrid: {e}"
            )

        return False

    async def get_instant(self, chunk: list):
        try:
            response = await self.session.get(
                f"{self.api_url}/torrents/instantAvailability/{'/'.join(chunk)}"
            )
            response_json = await response.json()
            if response.status != 200:
                logger.warning(
                    f"Request failed with status {response.status}: {response_json}"
                )
                return
            return response_json
        except Exception as e:
            logger.warning(
                f"Exception while checking hash instant availability on Real-Debrid: {e}"
            )

    async def get_files(
        self, torrent_hashes: list, type: str, season: str, episode: str, kitsu: bool
    ):
        chunk_size = 50
        chunks = [
            torrent_hashes[i : i + chunk_size]
            for i in range(0, len(torrent_hashes), chunk_size)
        ]

        tasks = []
        for chunk in chunks:
            tasks.append(self.get_instant(chunk))

        responses = await asyncio.gather(*tasks)

        availability = {}
        for response in responses:
            if response is not None:
                availability.update(response)

        files = {}

        if type == "series":
            for hash, details in availability.items():
                if "rd" not in details:
                    continue

                for variants in details["rd"]:
                    for index, file in variants.items():
                        filename = file["filename"]

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
                            "index": index,
                            "title": filename,
                            "size": file["filesize"],
                        }

                        break
        else:
            for hash, details in availability.items():
                if "rd" not in details:
                    continue

                for variants in details["rd"]:
                    for index, file in variants.items():
                        filename = file["filename"]

                        if not is_video(filename):
                            continue

                        if "sample" in filename.lower():
                            continue

                        files[hash] = {
                            "index": index,
                            "title": filename,
                            "size": file["filesize"],
                        }

                        break

        return files

    async def generate_download_link(self, hash: str, index: str):
        try:
            check_blacklisted = await self.session.get("https://real-debrid.com/vpn")
            check_blacklisted = await check_blacklisted.text()
            if (
                "Your ISP or VPN provider IP address is currently blocked on our website"
                in check_blacklisted
            ):
                self.proxy = settings.DEBRID_PROXY_URL
                if not self.proxy:
                    logger.warning(
                        "Real-Debrid blacklisted server's IP. No proxy found."
                    )
                else:
                    logger.warning(
                        f"Real-Debrid blacklisted server's IP. Switching to proxy {self.proxy} for {hash}|{index}"
                    )

            add_magnet = await self.session.post(
                f"{self.api_url}/torrents/addMagnet",
                data={"magnet": f"magnet:?xt=urn:btih:{hash}", "ip": self.ip},
                proxy=self.proxy,
            )
            add_magnet = await add_magnet.json()

            get_magnet_info = await self.session.get(
                add_magnet["uri"], proxy=self.proxy
            )
            get_magnet_info = await get_magnet_info.json()

            await self.session.post(
                f"{self.api_url}/torrents/selectFiles/{add_magnet['id']}",
                data={
                    "files": ",".join(
                        str(file["id"])
                        for file in get_magnet_info["files"]
                        if is_video(file["path"])
                    ),
                    "ip": self.ip,
                },
                proxy=self.proxy,
            )

            get_magnet_info = await self.session.get(
                add_magnet["uri"], proxy=self.proxy
            )
            get_magnet_info = await get_magnet_info.json()

            index = int(index)
            realIndex = index
            for file in get_magnet_info["files"]:
                if file["id"] == realIndex:
                    break

                if file["selected"] != 1:
                    index -= 1

            unrestrict_link = await self.session.post(
                f"{self.api_url}/unrestrict/link",
                data={"link": get_magnet_info["links"][index - 1], "ip": self.ip},
                proxy=self.proxy,
            )
            unrestrict_link = await unrestrict_link.json()

            return unrestrict_link["download"]
        except Exception as e:
            logger.warning(
                f"Exception while getting download link from Real-Debrid for {hash}|{index}: {e}"
            )
