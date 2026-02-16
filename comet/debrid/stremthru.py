import asyncio
from urllib.parse import quote, unquote

import aiohttp
from RTN import normalize_title, parse, title_match

from comet.core.execution import get_executor
from comet.core.logger import logger
from comet.core.models import settings
from comet.debrid.exceptions import DebridAuthError, DebridLinkGenerationError
from comet.services.debrid_cache import cache_availability
from comet.services.filtering import quick_alias_match
from comet.services.torrent_manager import torrent_update_queue
from comet.utils.parsing import ensure_multi_language, is_video


def batch_parse(filenames):
    parsed_results = [parse(f) for f in filenames]
    for parsed in parsed_results:
        ensure_multi_language(parsed)
    return parsed_results


class StremThru:
    _MAGNET_READY_STATUSES = {"cached", "downloaded"}
    _MAGNET_PENDING_STATUSES = {"queued", "downloading", "processing", "uploading"}
    _MAGNET_INVALID_STATUSES = {"failed", "invalid"}

    def __init__(
        self,
        session: aiohttp.ClientSession,
        video_id: str,
        media_only_id: str,
        token: str,
        ip: str,
    ):
        store, token = self.parse_store_creds(token)

        self.session = session
        self.base_url = f"{settings.STREMTHRU_URL}/v0/store"
        self.store_name = store
        self.store_token = token
        self.client_ip = ip
        self.sid = video_id
        self.media_only_id = media_only_id

    def parse_store_creds(self, token: str):
        if ":" in token:
            parts = token.split(":", 1)
            return parts[0], parts[1]

        return token, ""

    def _headers(self):
        return {
            "X-StremThru-Store-Name": self.store_name,
            "X-StremThru-Store-Authorization": f"Bearer {self.store_token}",
            "User-Agent": "comet",
        }

    @staticmethod
    def _extract_upstream_error_code(upstream_error: dict | None) -> str | None:
        if not isinstance(upstream_error, dict):
            return None
        return upstream_error.get("code") or upstream_error.get("error")

    async def _post_store_json(self, endpoint: str, payload: dict, action: str) -> dict:
        response = await self.session.post(
            f"{self.base_url}{endpoint}",
            json=payload,
            headers=self._headers(),
        )

        try:
            data = await response.json(content_type=None)
        except Exception as exc:
            raise DebridLinkGenerationError(
                self.store_name,
                f"{self.store_name}: Failed to {action}.",
                payload={
                    "status_code": response.status,
                    "raw": await response.text(),
                },
            ) from exc

        if isinstance(data, dict):
            error = data.get("error")
            if isinstance(error, dict):
                upstream = error.get("__upstream_cause__")
                message = error.get("message")
                if not message and isinstance(upstream, dict):
                    message = upstream.get("detail") or upstream.get("message")

                raise DebridLinkGenerationError(
                    self.store_name,
                    message or f"{self.store_name}: Failed to {action}.",
                    error_code=error.get("code"),
                    upstream_error_code=self._extract_upstream_error_code(upstream),
                    payload=data,
                )

            if response.status < 400:
                return data

        raise DebridLinkGenerationError(
            self.store_name,
            f"{self.store_name}: Failed to {action}.",
            payload={"response": data, "status_code": response.status},
        )

    async def check_premium(self):
        try:
            response = await self.session.get(
                f"{self.base_url}/user?client_ip={self.client_ip}",
                headers=self._headers(),
            )
            user = await response.json()

            if "data" not in user:
                raise DebridAuthError(
                    self.store_name,
                    f"{self.store_name}: Invalid API key.\nPlease check your configuration.",
                )

            if user["data"]["subscription_status"] != "premium":
                raise DebridAuthError(
                    self.store_name,
                    f"{self.store_name}: No active subscription.\nPlease renew your debrid account.",
                )
        except DebridAuthError:
            raise
        except Exception as e:
            raise DebridAuthError(
                self.store_name,
                f"{self.store_name}: Failed to check account status.\n{e}",
            )

    async def get_instant(self, magnets: list):
        try:
            url = f"{self.base_url}/magnets/check?magnet={','.join(magnets)}&client_ip={self.client_ip}&sid={self.sid}"
            magnet = await self.session.get(url, headers=self._headers())
            return await magnet.json()
        except Exception as e:
            logger.warning(
                f"Exception while checking hash instant availability on {self.store_name}: {e}"
            )

    async def list_magnets(self, limit: int = 500, offset: int = 0):
        try:
            response = await self.session.get(
                f"{self.base_url}/magnets?limit={limit}&offset={offset}&client_ip={self.client_ip}",
                headers=self._headers(),
            )
            payload = await response.json()
            data = payload["data"]
            return data["items"], int(data["total_items"])
        except Exception as e:
            logger.warning(
                f"Exception while listing account magnets on {self.store_name}: {e}"
            )
            return None, 0

    async def get_availability(
        self,
        torrent_hashes: list,
        seeders_map: dict,
        tracker_map: dict,
        sources_map: dict,
    ):
        await self.check_premium()

        chunk_size = 500
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

        is_offcloud = self.store_name == "offcloud"

        filenames_to_parse = []
        if not is_offcloud:
            for result in availability:
                for torrent in result:
                    if torrent["status"] != "cached":
                        continue
                    for file in torrent["files"]:
                        filename = file["name"].split("/")[-1]
                        if not is_video(filename) or "sample" in filename.lower():
                            continue
                        filenames_to_parse.append(filename)

        parsed_iter = iter([])
        if filenames_to_parse:
            loop = asyncio.get_running_loop()
            parsed_results = await loop.run_in_executor(
                get_executor(), batch_parse, filenames_to_parse
            )
            parsed_iter = iter(parsed_results)

        files = []
        cached_count = 0
        for result in availability:
            for torrent in result:
                if torrent["status"] != "cached":
                    continue

                cached_count += 1
                hash = torrent["hash"]
                seeders = seeders_map.get(hash, 0)
                tracker = tracker_map.get(hash, "")
                sources = sources_map.get(hash, [])

                if is_offcloud:
                    file_info = {
                        "info_hash": hash,
                        "index": None,
                        "title": None,
                        "size": None,
                        "season": None,
                        "episode": None,
                        "parsed": None,
                    }

                    files.append(file_info)
                else:
                    for file in torrent["files"]:
                        filename = file["name"].split("/")[-1]

                        if not is_video(filename) or "sample" in filename.lower():
                            continue

                        filename_parsed = next(parsed_iter)

                        season = (
                            filename_parsed.seasons[0]
                            if filename_parsed.seasons
                            else None
                        )
                        episode = (
                            filename_parsed.episodes[0]
                            if filename_parsed.episodes
                            else None
                        )
                        if ":" in self.sid and (season is None or episode is None):
                            continue

                        index = file["index"] if file["index"] != -1 else None
                        size = file["size"] if file["size"] != -1 else None

                        file_info = {
                            "info_hash": hash,
                            "index": index,
                            "title": filename,
                            "size": size,
                            "season": season,
                            "episode": episode,
                            "parsed": filename_parsed,
                            "seeders": seeders,
                            "tracker": tracker,
                            "sources": sources,
                        }

                        files.append(file_info)
                        await torrent_update_queue.add_torrent_info(
                            file_info, self.media_only_id
                        )

        logger.log(
            "SCRAPER",
            f"{self.store_name}: Found {cached_count} cached torrents with {len(files)} valid files",
        )
        return files

    async def generate_download_link(
        self,
        hash: str,
        index: str,
        name: str,
        torrent_name: str,
        season: int,
        episode: int,
        sources: list = None,
        aliases: dict = None,
    ):
        """
        Smart file selection algorithm with scoring system.

        Priority order (highest to lowest):
        1. Exact season + episode match with single episode file (+1000)
        2. Exact season + episode match with multi-episode file (+500)
        3. Episode match without season info (+200)
        4. Exact filename match with requested torrent_name (+100)
        5. Title alias match (+50)
        6. Index match from original selection (+25)
        7. Fallback: largest video file (+file_size as tiebreaker)
        """
        try:
            magnet_uri = f"magnet:?xt=urn:btih:{hash}&dn={quote(torrent_name)}"

            if sources:
                for source in sources:
                    magnet_uri += f"&tr={quote(source, safe='')}"

            magnet = await self._post_store_json(
                f"/magnets?client_ip={self.client_ip}",
                {"magnet": magnet_uri},
                "add torrent to store",
            )

            magnet_data = magnet.get("data", {})
            magnet_status = magnet_data.get("status", "")

            if magnet_status in self._MAGNET_PENDING_STATUSES:
                raise DebridLinkGenerationError(
                    self.store_name,
                    f"{self.store_name}: Media is not cached yet (status: {magnet_status}).",
                    upstream_error_code="MEDIA_NOT_CACHED_YET",
                    payload={"status": magnet_status, "data": magnet_data},
                )
            if magnet_status in self._MAGNET_INVALID_STATUSES:
                raise DebridLinkGenerationError(
                    self.store_name,
                    f"{self.store_name}: Torrent cannot be processed (status: {magnet_status}).",
                    upstream_error_code="STORE_MAGNET_INVALID",
                    payload={"status": magnet_status, "data": magnet_data},
                )
            if magnet_status not in self._MAGNET_READY_STATUSES:
                return

            name = unquote(name)
            torrent_name = unquote(torrent_name)

            ez_aliases = aliases.get("ez", [])
            if ez_aliases:
                ez_aliases_normalized = [normalize_title(a) for a in ez_aliases]

            debrid_files = magnet.get("data", {}).get("files", [])

            # Filter to video files only, excluding samples
            video_files = []
            filenames_to_parse = []
            for file in debrid_files:
                filename = file["name"]
                filename_lower = filename.lower()

                if "sample" in filename_lower:
                    continue
                if not is_video(filename):
                    continue

                video_files.append(file)
                filenames_to_parse.append(filename)

            if not video_files:
                logger.warning(f"No video files found in torrent {hash}")
                return

            loop = asyncio.get_running_loop()
            parsed_results = await loop.run_in_executor(
                get_executor(), batch_parse, filenames_to_parse
            )

            scored_files = []

            for file, filename, parsed in zip(
                video_files, filenames_to_parse, parsed_results
            ):
                file_index = file["index"] if file.get("index", -1) != -1 else None
                file_size = file["size"] if file.get("size", -1) != -1 else 0
                file_link = file.get("link")

                if not file_link:
                    continue

                file_season = parsed.seasons[0] if parsed.seasons else None
                file_episode = parsed.episodes[0] if parsed.episodes else None

                # Calculate score
                score = 0
                match_reason = []

                # Season + Episode matching (highest priority)
                if season is not None and episode is not None:
                    season_matches = (not parsed.seasons) or (season in parsed.seasons)
                    episode_matches = parsed.episodes and episode in parsed.episodes

                    if season_matches and episode_matches:
                        if len(parsed.episodes) == 1:
                            score += 1000  # Perfect single episode match
                            match_reason.append("exact_episode")
                        else:
                            score += 500  # Multi-episode file containing our episode
                            match_reason.append("multi_episode")
                    elif episode_matches:
                        score += 200  # Episode matches but season doesn't
                        match_reason.append("episode_only")

                # Exact filename match
                if filename == torrent_name:
                    score += 100
                    match_reason.append("exact_name")

                # Title/alias matching
                if parsed.parsed_title:
                    # Quick alias match first
                    if ez_aliases and quick_alias_match(
                        normalize_title(filename), ez_aliases_normalized
                    ):
                        score += 50
                        match_reason.append("alias")
                    elif title_match(name, parsed.parsed_title, aliases=aliases):
                        score += 50
                        match_reason.append("title")

                # Index match from original selection
                if file_index is not None and str(file_index) == str(index):
                    score += 25
                    match_reason.append("index")

                # Use file size as tiebreaker (larger files preferred)
                # Normalize to 0-10 range to not overwhelm other scores
                size_score = min(
                    file_size / (10 * 1024 * 1024 * 1024), 10
                )  # Cap at 10GB
                score += size_score

                enriched_file = {
                    "index": file_index,
                    "title": filename,
                    "size": file_size if file_size > 0 else None,
                    "season": file_season,
                    "episode": file_episode,
                    "link": file_link,
                    "parsed": parsed,
                    "score": score,
                    "match_reason": match_reason,
                }

                scored_files.append(enriched_file)

            if not scored_files:
                logger.log(
                    "PLAYBACK",
                    f"No valid video files with links found in torrent {hash}",
                )
                return

            # Sort by score descending
            scored_files.sort(key=lambda x: x["score"], reverse=True)

            # Select best file
            target_file = scored_files[0]

            logger.log(
                "PLAYBACK",
                f"File selection for {hash}: selected '{target_file['title']}' "
                f"(score={target_file['score']:.1f}, reasons={target_file['match_reason']}) "
                f"from {len(scored_files)} candidates",
            )

            all_files_for_cache = []

            for f in scored_files:
                if f["season"] is not None or f["episode"] is not None:
                    all_files_for_cache.append(
                        {
                            "info_hash": hash,
                            "index": f["index"],
                            "title": f["title"],
                            "size": f["size"],
                            "season": f["season"]
                            if f["season"] is not None
                            else season,
                            "episode": f["episode"],
                            "parsed": f["parsed"],
                        }
                    )

            # Also ensure the selected file is cached with the REQUESTED season/episode
            # This handles cases where filename doesn't contain S/E info but user requested it
            if season is not None or episode is not None:
                all_files_for_cache.append(
                    {
                        "info_hash": hash,
                        "index": target_file["index"],
                        "title": target_file["title"],
                        "size": target_file["size"],
                        "season": season,
                        "episode": episode,
                        "parsed": target_file["parsed"],
                    }
                )

            if all_files_for_cache:
                asyncio.create_task(
                    cache_availability(self.store_name, all_files_for_cache)
                )

            link = await self._post_store_json(
                f"/link/generate?client_ip={self.client_ip}",
                {"link": target_file["link"]},
                "generate download link",
            )

            return link.get("data", {}).get("link")
        except DebridLinkGenerationError:
            raise
        except Exception as e:
            logger.warning(f"Exception while getting download link for {hash}: {e}")
