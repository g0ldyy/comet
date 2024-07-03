import aiohttp

from comet.utils.logger import logger
from comet.utils.models import settings


class RealDebrid:
    def __init__(self, session: aiohttp.ClientSession, debrid_api_key: str):
        session.headers["Authorization"] = f"Bearer {debrid_api_key}"
        self.session = session

    async def check_premium(self):
        try:
            check_premium = await self.session.get(
                "https://api.real-debrid.com/rest/1.0/user"
            )
            check_premium = await check_premium.text()
            if '"type": "premium"' not in check_premium:
                return False

            return True
        except Exception as e:
            logger.warning(
                f"Exception while checking premium status on Real Debrid: {e}"
            )
            return False

    async def check_hash_cache(self, hash: str):
        try:
            response = await self.session.get(
                f"https://api.real-debrid.com/rest/1.0/torrents/instantAvailability/{hash}"
            )
            return response
        except Exception as e:
            logger.warning(
                f"Exception while checking hash cache on Real Debrid for {hash}: {e}"
            )
            return

    async def generate_download_link(self, hash: str, index: str):
        try:
            check_blacklisted = await self.session.get("https://real-debrid.com/vpn")
            check_blacklisted = await check_blacklisted.text()
            proxy = None
            if (
                "Your ISP or VPN provider IP address is currently blocked on our website"
                in check_blacklisted
            ):
                proxy = settings.DEBRID_PROXY_URL
                if not proxy:
                    logger.warning(
                        "Real-Debrid blacklisted server's IP. No proxy found."
                    )
                    return "https://comet.fast"
                else:
                    logger.warning(
                        f"Real-Debrid blacklisted server's IP. Switching to proxy {proxy} for {hash}|{index}"
                    )

            add_magnet = await self.session.post(
                "https://api.real-debrid.com/rest/1.0/torrents/addMagnet",
                data={"magnet": f"magnet:?xt=urn:btih:{hash}"},
                proxy=proxy,
            )
            add_magnet = await add_magnet.json()

            get_magnet_info = await self.session.get(add_magnet["uri"], proxy=proxy)
            get_magnet_info = await get_magnet_info.json()

            await self.session.post(
                f"https://api.real-debrid.com/rest/1.0/torrents/selectFiles/{add_magnet['id']}",
                data={"files": index},
                proxy=proxy,
            )

            get_magnet_info = await self.session.get(add_magnet["uri"], proxy=proxy)
            get_magnet_info = await get_magnet_info.json()

            unrestrict_link = await self.session.post(
                "https://api.real-debrid.com/rest/1.0/unrestrict/link",
                data={"link": get_magnet_info["links"][0]},
                proxy=proxy,
            )
            unrestrict_link = await unrestrict_link.json()

            return unrestrict_link["download"]
        except Exception as e:
            logger.warning(
                f"Exception while getting download link from Real Debrid for {hash}|{index}: {e}"
            )
            return "https://comet.fast"
