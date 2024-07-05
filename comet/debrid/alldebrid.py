import aiohttp

from RTN import parse

from comet.utils.general import is_video
from comet.utils.logger import logger
from comet.utils.models import settings


class AllDebrid:
    def __init__(self, session: aiohttp.ClientSession, debrid_api_key: str):
        session.headers["Authorization"] = f"Bearer {debrid_api_key}"
        self.session = session

        self.api_url = "http://api.alldebrid.com/v4"
        self.agent = "comet"

    async def check_premium(self):
        try:
            check_premium = await self.session.get(
                f"{self.api_url}/user?agent={self.agent}"
            )
            check_premium = await check_premium.text()
            if '"isPremium": true' in check_premium:
                return True
        except Exception as e:
            logger.warning(
                f"Exception while checking premium status on All Debrid: {e}"
            )

        return False

    async def get_files(self, torrent_hashes: list, type: str, season: str, episode: str):
        try:
            get_instant = await self.session.get(
                f"{self.api_url}/magnet/instant?agent={self.agent}&magnets[]={'&magnets[]='.join(hash for hash in torrent_hashes)}"
            )
            availability = await get_instant.json()
        except Exception as e:
            logger.warning(
                f"Exception while checking hash cache on All Debrid for {hash}: {e}"
            )

            return {}

        if not "status" in availability or availability["status"] != "success":
            return {}
        
        files = {}
        for magnet in availability["data"]["magnets"]:
            if not magnet["instant"]:
                continue

            if type == "series":
                for file in magnet["files"]:
                    filename = file["n"]

                    if not is_video(filename):
                        continue

                    filename_parsed = parse(filename)
                    if (
                        season in filename_parsed.season
                        and episode in filename_parsed.episode
                    ):
                        files[magnet["hash"]] = {
                            "index": magnet["files"].index(file),
                            "title": filename,
                            "size": file["s"],
                        }

                continue

            for file in magnet["files"]:
                filename = file["n"]

                if not is_video(filename):
                    continue

                files[magnet["hash"]] = {
                    "index": magnet["files"].index(file),
                    "title": filename,
                    "size": file["s"],
                }

        return files

    async def generate_download_link(self, hash: str, index: str):
        try:
            check_blacklisted = await self.session.get(
                f"{self.api_url}/magnet/upload?agent=comet&magnets[]={hash}"
            )
            check_blacklisted = await check_blacklisted.text()
            proxy = None
            if "NO_SERVER" in check_blacklisted:
                proxy = settings.DEBRID_PROXY_URL
                if not proxy:
                    logger.warning(
                        "All-Debrid blacklisted server's IP. No proxy found."
                    )
                else:
                    logger.warning(
                        f"All-Debrid blacklisted server's IP. Switching to proxy {proxy} for {hash}|{index}"
                    )

            upload_magnet = await self.session.get(
                f"{self.api_url}/magnet/upload?agent=comet&magnets[]={hash}",
                proxy=proxy,
            )
            upload_magnet = await upload_magnet.json()

            get_magnet_status = await self.session.get(
                f"{self.api_url}/magnet/status?agent=comet&id={upload_magnet['data']['magnets'][0]['id']}",
                proxy=proxy,
            )
            get_magnet_status = await get_magnet_status.json()

            unlock_link = await self.session.get(
                f"{self.api_url}/link/unlock?agent=comet&link={get_magnet_status['data']['magnets']['links'][int(index)]['link']}",
                proxy=proxy,
            )
            unlock_link = await unlock_link.json()

            return unlock_link["data"]["link"]
        except Exception as e:
            logger.warning(
                f"Exception while getting download link from All Debrid for {hash}|{index}: {e}"
            )
            return "https://comet.fast"
