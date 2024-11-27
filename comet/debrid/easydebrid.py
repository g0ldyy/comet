import aiohttp
import asyncio

from RTN import parse

from comet.utils.general import is_video
from comet.utils.logger import logger


class EasyDebrid:
    def __init__(self, session: aiohttp.ClientSession, debrid_api_key: str, ip: str):
        self.session = session
        self.ip = ip
        self.proxy = None

        self.api_url = "https://easydebrid.com/api/v1"
        self.headers = {"Authorization": f"Bearer {debrid_api_key}"}

        if ip:
            self.headers["X-Forwarded-For"] = ip

    async def check_premium(self):
        try:
            response = await self.session.get(
                f"{self.api_url}/user/details", headers=self.headers
            )
            data = await response.json()
            return bool(data["paid_until"])
        except Exception as e:
            logger.warning(f"Failed to check EasyDebrid premium status: {e}")

        return False

    async def get_instant(self, chunk):
        try:
            response = await self.session.post(
                f"{self.api_url}/link/lookup",
                json={"urls": chunk},
                headers=self.headers,
            )
            data = await response.json()

            if not data or "cached" not in data:
                return None

            return {
                "status": "success",
                "response": data["cached"],
                "filename": data.get("filenames", []),
                "filesize": [None] * len(chunk),
                "hashes": chunk,
            }
        except Exception as e:
            logger.warning(
                f"Exception while checking hash instant availability on EasyDebrid: {e}"
            )

    async def get_files(self, torrent_hashes, type, season, episode, kitsu):
        chunk_size = 100
        chunks = [
            torrent_hashes[i : i + chunk_size]
            for i in range(0, len(torrent_hashes), chunk_size)
        ]

        tasks = []
        for chunk in chunks:
            tasks.append(self.get_instant(chunk))

        responses = await asyncio.gather(*tasks)

        files = {}

        if type == "series":
            for result in responses:
                if result["status"] != "success":
                    continue

                responses = result["response"]
                filenames = result["filename"]
                hashes = result["hashes"]

                for index, (is_cached, hash) in enumerate(zip(responses, hashes)):
                    if not is_cached:
                        continue
                    
                    try:
                        hash_files = filenames[index]
                    except:
                        hash_files = filenames[str(index)]

                    for filename in hash_files:
                        if not is_video(filename):
                            continue

                        if "sample" in filename.lower():
                            continue

                        filename_parsed = parse(filename)
                        if not filename_parsed:
                            continue

                        if episode not in filename_parsed.episodes:
                            continue

                        if kitsu:
                            if filename_parsed.seasons:
                                continue
                        elif season not in filename_parsed.seasons:
                            continue

                        files[hash] = {
                            "index": f"{season}|{episode}",
                            "title": filename,
                            "size": 0,  # Size not available in lookup response
                        }
                        break  # Found matching video file
        else:
            for result in responses:
                if result["status"] != "success":
                    continue

                responses = result["response"]
                filenames = result["filename"]
                hashes = result["hashes"]

                for index, (is_cached, hash) in enumerate(zip(responses, hashes)):
                    if not is_cached:
                        continue

                    try:
                        hash_files = filenames[index]
                    except:
                        hash_files = filenames[str(index)]

                    video_files = [f for f in hash_files if is_video(f)]
                    if not video_files:
                        continue

                    # Use first valid video file found
                    files[hash] = {
                        "index": 0,
                        "title": video_files[0],
                        "size": 0,  # Size not available in lookup response
                    }

        return files

    async def generate_download_link(self, hash, index):
        try:
            response = await self.session.post(
                f"{self.api_url}/link/generate",
                headers={**self.headers, "Content-Type": "application/json"},
                json={"url": f"magnet:?xt=urn:btih:{hash}"},
            )
            data = await response.json()

            if not data or "files" not in data:
                return None

            video_files = [
                f
                for f in data["files"]
                if is_video(f["filename"]) and "sample" not in f["filename"].lower()
            ]

            if not video_files:
                return None

            if "|" in str(index):
                season, episode = map(int, index.split("|"))
                for file in video_files:
                    parsed = parse(file["filename"])
                    if (
                        parsed
                        and season in parsed.seasons
                        and episode in parsed.episodes
                    ):
                        return file["url"]

            largest_file = max(video_files, key=lambda x: x["size"])

            return largest_file["url"]
        except Exception as e:
            logger.warning(f"Error generating link for {hash}|{index}: {e}")
