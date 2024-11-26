import aiohttp
import asyncio
import time

from RTN import parse

from comet.utils.general import is_video
from comet.utils.logger import logger


class TorBox:
    def __init__(self, session: aiohttp.ClientSession, debrid_api_key: str):
        session.headers["Authorization"] = f"Bearer {debrid_api_key}"
        self.session = session
        self.proxy = None

        self.api_url = "https://api.torbox.app/v1/api"
        self.debrid_api_key = debrid_api_key

    async def check_premium(self):
        try:
            check_premium = await retry(
                self.session.get,
                f"{self.api_url}/user/me?settings=false",
                timeout=aiohttp.ClientTimeout(total=10)
            )
            check_premium = await check_premium.text()
            if '"success":true' in check_premium and '"plan":0' not in check_premium:
                return True
        except Exception as e:
            logger.warning(f"Exception while checking premium status on TorBox: {e}")

        return False

    async def get_instant(self, chunk: list):
        try:
            response = await retry(
                self.session.get,
                f"{self.api_url}/torrents/checkcached?hash={','.join(chunk)}&format=list&list_files=true",
                timeout=aiohttp.ClientTimeout(total=10)
            )
            return await response.json()
        except Exception as e:
            logger.warning(
                f"Exception while checking hash instant availability on TorBox: {e}"
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

        availability = [response for response in responses if response is not None]

        files = {}

        if type == "series":
            for result in availability:
                if not result["success"] or not result["data"]:
                    continue

                for torrent in result["data"]:
                    torrent_files = torrent["files"]
                    for file in torrent_files:
                        filename = file["name"].split("/")[1]

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

                        files[torrent["hash"]] = {
                            "index": torrent_files.index(file),
                            "title": filename,
                            "size": file["size"],
                        }

                        break
        else:
            for result in availability:
                if not result["success"] or not result["data"]:
                    continue

                for torrent in result["data"]:
                    torrent_files = torrent["files"]
                    for file in torrent_files:
                        filename = file["name"].split("/")[1]

                        if not is_video(filename):
                            continue

                        if "sample" in filename.lower():
                            continue

                        files[torrent["hash"]] = {
                            "index": torrent_files.index(file),
                            "title": filename,
                            "size": file["size"],
                        }

                        break

        return files

    async def generate_download_link(self, hash: str, index: str):
      try:
          get_torrents = await retry(
              self.session.get,
              f"{self.api_url}/torrents/mylist?bypass_cache=true",
              timeout=aiohttp.ClientTimeout(total=10)
          )
          get_torrents = await get_torrents.json()
          exists = False
          for torrent in get_torrents["data"]:
              if torrent["hash"] == hash:
                  torrent_id = torrent["id"]
                  exists = True
                  break
          if not exists:
              create_torrent = await retry(
                  self.session.post,
                  f"{self.api_url}/torrents/createtorrent",
                  data={"magnet": f"magnet:?xt=urn:btih:{hash}"},
                  timeout=aiohttp.ClientTimeout(total=10)
              )
              create_torrent = await create_torrent.json()
              torrent_id = create_torrent["data"]["torrent_id"]

          get_download_link = await retry(
              self.session.get,
              f"{self.api_url}/torrents/requestdl?token={self.debrid_api_key}&torrent_id={torrent_id}&file_id={index}&zip=false",
              timeout=aiohttp.ClientTimeout(total=10)
          )
          get_download_link = await get_download_link.json()
          return get_download_link["data"]

      except Exception as e:
          logger.warning(
              f"Exception while getting download link from TorBox for {hash}|{index}: {e}"
          )


async def retry(async_func, *args, max_retries=3, **kwargs):
    retries = 0
    # Get the URL from args (first argument for get/post requests)
    url = args[0] if args else "unknown URL"

    while retries < max_retries:
        try:
            return await async_func(*args, **kwargs)
        except aiohttp.ClientResponseError as e:
            if e.status == 429:
                retries += 1
                if retries >= max_retries:
                    raise Exception(f"Max retries ({max_retries}) exceeded for rate limit on {url}")

                reset_time = int(e.headers.get('x-ratelimit-reset', 0))
                current_time = int(time.time())
                wait_time = max(reset_time - current_time + 3, 0)

                logger.warning(f"Rate limited on {url}. Waiting for {wait_time} seconds before retry {retries}/{max_retries}...")
                await asyncio.sleep(wait_time)
                continue
            raise
