import aiohttp
import asyncio

from RTN import parse

from comet.utils.general import is_video
from comet.utils.logger import logger


class Premiumize:
    def __init__(self, session: aiohttp.ClientSession, debrid_api_key: str):
        self.session = session
        self.proxy = None

        self.api_url = "https://premiumize.me/api"
        self.debrid_api_key = debrid_api_key

    async def check_premium(self):
        try:
            check_premium = await self.session.get(
                f"{self.api_url}/account/info?apikey={self.debrid_api_key}"
            )
            check_premium = await check_premium.text()
            if (
                '"status":"success"' in check_premium
                and '"premium_until":null' not in check_premium
            ):
                return True
        except Exception as e:
            logger.warning(
                f"Exception while checking premium status on Premiumize: {e}"
            )

        return False

    async def get_instant(self, chunk: list):
        try:
            response = await self.session.get(
                f"{self.api_url}/cache/check?apikey={self.debrid_api_key}&items[]={'&items[]='.join(chunk)}"
            )

            response = await response.json()
            response["hashes"] = chunk

            return response
        except Exception as e:
            logger.warning(
                f"Exception while checking hash instant availability on Premiumize: {e}"
            )

    async def get_files(
        self, torrent_hashes: list, type: str, season: str, episode: str, kitsu: bool
    ):
        chunk_size = 100
        chunks = [
            torrent_hashes[i : i + chunk_size]
            for i in range(0, len(torrent_hashes), chunk_size)
        ]

        tasks = []
        for chunk in chunks:
            tasks.append(self.get_instant(chunk))

        responses = await asyncio.gather(*tasks)

        availability = []
        for response in responses:
            if not response:
                continue

            availability.append(response)

        files = {}

        if type == "series":
            for result in availability:
                if result["status"] != "success":
                    continue

                responses = result["response"]
                filenames = result["filename"]
                filesizes = result["filesize"]
                hashes = result["hashes"]
                for index, response in enumerate(responses):
                    if not response:
                        continue

                    if not filesizes[index]:
                        continue

                    filename = filenames[index]

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

                    files[hashes[index]] = {
                        "index": f"{season}|{episode}",
                        "title": filename,
                        "size": int(filesizes[index]),
                    }
        else:
            for result in availability:
                if result["status"] != "success":
                    continue

                responses = result["response"]
                filenames = result["filename"]
                filesizes = result["filesize"]
                hashes = result["hashes"]
                for index, response in enumerate(responses):
                    if response is False:
                        continue

                    if not filesizes[index]:
                        continue

                    filename = filenames[index]

                    if "sample" in filename.lower():
                        continue

                    files[hashes[index]] = {
                        "index": 0,
                        "title": filename,
                        "size": int(filesizes[index]),
                    }

        return files

    async def generate_download_link(self, hash: str, index: str):
        try:
            add_magnet = await self.session.post(
                f"{self.api_url}/transfer/directdl?apikey={self.debrid_api_key}&src=magnet:?xt=urn:btih:{hash}",
            )
            add_magnet = await add_magnet.json()

            season = None
            if "|" in index:
                index = index.split("|")
                season = int(index[0])
                episode = int(index[1])

            content = add_magnet["content"]
            for file in content:
                filename = file["path"]
                if "/" in filename:
                    filename = filename.split("/")[1]

                if not is_video(filename):
                    content.remove(file)
                    continue

                if season is not None:
                    filename_parsed = parse(filename)
                    if (
                        season in filename_parsed.seasons
                        and episode in filename_parsed.episodes
                    ):
                        return file["link"]

            max_size_item = max(content, key=lambda x: x["size"])
            return max_size_item["link"]
        except Exception as e:
            logger.warning(
                f"Exception while getting download link from Premiumize for {hash}|{index}: {e}"
            )
