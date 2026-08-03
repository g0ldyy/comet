import asyncio
from urllib.parse import quote, unquote

import aiohttp
import orjson
from RTN import normalize_title, parse, title_match

from comet.core.execution import get_executor
from comet.core.models import settings
from comet.core.provider_json import is_success_status
from comet.debrid.exceptions import DebridAuthError, DebridLinkGenerationError
from comet.debrid.file_selection import (
    is_auxiliary_video,
    select_best_availability_files,
)
from comet.debrid.link_cache import valid_download_url
from comet.metadata.episode_index import EpisodeIndexService
from comet.services.debrid_cache import schedule_cache_availability
from comet.services.filtering import exact_alias_match
from comet.services.torrent_manager import torrent_update_queue
from comet.utils.parsing import (
    ensure_multi_language,
    is_video,
    match_parsed_episode_target,
    parse_media_id,
)

_REQUEST_TIMEOUT = aiohttp.ClientTimeout(
    total=20,
    connect=5,
    sock_connect=5,
    sock_read=15,
)


def batch_parse(filenames):
    parsed_results = [parse(filename) for filename in filenames]
    for parsed in parsed_results:
        ensure_multi_language(parsed)
    return parsed_results


def _prepare_cached_torrents(responses, *, is_offcloud: bool):
    prepared = []
    filenames = []
    seen_hashes = set()

    for response in responses:
        for torrent in response["data"]["items"]:
            info_hash = torrent["hash"]
            if info_hash in seen_hashes:
                continue

            prepared_files = []
            if not is_offcloud:
                for file in torrent["files"]:
                    name = file["name"]
                    index = file["index"]
                    if name == "" and index == -1 and file.get("path") == "":
                        # StremThru can include a store-specific cache marker next
                        # to richer file metadata learned from another source.
                        continue
                    filename = name.rsplit("/", 1)[-1]
                    if not is_video(filename):
                        continue
                    prepared_files.append((file, filename))
                    filenames.append(filename)

            if not is_offcloud and not prepared_files:
                continue
            seen_hashes.add(info_hash)
            prepared.append(
                {
                    "info_hash": info_hash,
                    "files": prepared_files,
                }
            )

    return prepared, filenames


class StremThru:
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
        self.base_url = f"{settings.STREMTHRU_URL.rstrip('/')}/v0/store"
        self.store_name = store
        self.store_token = token
        self.client_ip = ip
        self.sid = video_id
        self.media_only_id = media_only_id

    @staticmethod
    def parse_store_creds(token: str) -> tuple[str, str]:
        store, separator, credential = token.partition(":")
        return (store, credential) if separator else (store, "")

    def _headers(self):
        return {
            "X-StremThru-Store-Name": self.store_name,
            "X-StremThru-Store-Authorization": f"Bearer {self.store_token}",
            "User-Agent": "comet",
            "Accept": "application/json",
            "Accept-Encoding": "identity",
        }

    @staticmethod
    def _extract_upstream_error_code(upstream_error: dict | None) -> str | None:
        upstream_error = upstream_error or {}
        return upstream_error.get("code") or upstream_error.get("error")

    def _requested_episode_scope(self) -> tuple[str | None, int | None, int | None]:
        if not self.sid or ":" not in self.sid:
            return None, None, None
        try:
            series_id, season, episode = parse_media_id("series", self.sid)
        except ValueError:
            return None, None, None
        return series_id, season, episode

    @staticmethod
    def _strict_episode_match(
        parsed,
        season: int,
        episode: int,
        target_air_date: str | None,
    ) -> bool:
        return match_parsed_episode_target(
            parsed,
            season,
            episode,
            target_air_date=target_air_date,
            reject_unknown_episode_files=True,
        )

    async def _episode_request_context(
        self,
        series_id: str | None,
        season: int | None,
        episode: int | None,
        target_air_date: str | None = None,
    ) -> tuple[bool, int | None, int | None, str | None]:
        is_episode_request = (
            series_id is not None
            and series_id.startswith("tt")
            and season is not None
            and episode is not None
        )
        if not is_episode_request:
            return False, season, episode, None

        if target_air_date is None:
            target_air_date = await EpisodeIndexService(
                self.session
            ).get_target_air_date(series_id, season, episode)
        return True, season, episode, target_air_date

    async def _request_store_json(
        self,
        method: str,
        endpoint: str,
        *,
        params: dict | None = None,
        payload: dict | None = None,
        action: str,
    ) -> tuple[int, dict]:
        request = self.session.get if method == "GET" else self.session.post
        try:
            async with request(
                f"{self.base_url}{endpoint}",
                params=params,
                **({"json": payload} if payload is not None else {}),
                headers=self._headers(),
                allow_redirects=False,
                timeout=_REQUEST_TIMEOUT,
            ) as response:
                status = response.status
                data = orjson.loads(await response.read())
        except (
            aiohttp.ClientError,
            TimeoutError,
            orjson.JSONDecodeError,
        ):
            raise DebridLinkGenerationError(
                self.store_name,
                f"{self.store_name}: Failed to {action}.",
            ) from None
        return status, data

    async def _post_store_json(
        self,
        endpoint: str,
        payload: dict,
        action: str,
        *,
        params: dict | None = None,
    ) -> dict:
        status, data = await self._request_store_json(
            "POST",
            endpoint,
            params=params,
            payload=payload,
            action=action,
        )
        error = data.get("error")
        if error:
            upstream = error.get("__upstream_cause__")
            raise DebridLinkGenerationError(
                self.store_name,
                f"{self.store_name}: Failed to {action}.",
                error_code=error.get("code"),
                upstream_error_code=self._extract_upstream_error_code(upstream),
            )
        if not 200 <= status < 300:
            raise DebridLinkGenerationError(
                self.store_name,
                f"{self.store_name}: Failed to {action}.",
            )
        return data

    async def check_premium(self):
        try:
            status, user = await self._request_store_json(
                "GET",
                "/user",
                params={"client_ip": self.client_ip},
                action="check account status",
            )
        except DebridLinkGenerationError:
            raise DebridAuthError(
                self.store_name,
                f"{self.store_name}: Failed to check account status.",
            ) from None
        if not is_success_status(status) or not user.get("data"):
            raise DebridAuthError(
                self.store_name,
                f"{self.store_name}: Invalid API key.\nPlease check your configuration.",
            )

    async def get_instant(self, magnets: list):
        status, payload = await self._request_store_json(
            "GET",
            "/magnets/check",
            params={
                "magnet": ",".join(magnets),
                "client_ip": self.client_ip,
                "sid": self.sid or "",
            },
            action="check instant availability",
        )
        if not is_success_status(status) or payload.get("error"):
            raise DebridLinkGenerationError(
                self.store_name,
                f"{self.store_name}: Failed to check instant availability.",
            )
        return payload

    async def list_magnets(self, limit: int = 500, offset: int = 0):
        status, payload = await self._request_store_json(
            "GET",
            "/magnets",
            params={
                "limit": limit,
                "offset": offset,
                "client_ip": self.client_ip,
            },
            action="list account magnets",
        )
        if not is_success_status(status) or payload.get("error"):
            raise DebridLinkGenerationError(
                self.store_name,
                f"{self.store_name}: Failed to list account magnets.",
            )
        data = payload["data"]
        return data["items"], data["total_items"]

    async def get_availability(
        self,
        torrent_hashes: list,
        seeders_map: dict,
        tracker_map: dict,
        sources_map: dict,
        target_air_date: str | None = None,
    ):
        if not torrent_hashes:
            return []
        torrent_hashes = list(dict.fromkeys(torrent_hashes))
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

        is_offcloud = self.store_name == "offcloud"
        try:
            cached_torrents, filenames_to_parse = _prepare_cached_torrents(
                responses,
                is_offcloud=is_offcloud,
            )
        except (AttributeError, KeyError, TypeError, ValueError):
            raise DebridLinkGenerationError(
                self.store_name,
                f"{self.store_name}: Invalid instant availability response.",
            ) from None
        requested_series_id, requested_season, requested_episode = (
            self._requested_episode_scope()
        )
        (
            is_episode_request,
            requested_season,
            requested_episode,
            target_air_date,
        ) = await self._episode_request_context(
            requested_series_id,
            requested_season,
            requested_episode,
            target_air_date=target_air_date,
        )

        parsed_iter = iter([])
        if filenames_to_parse:
            loop = asyncio.get_running_loop()
            parsed_results = await loop.run_in_executor(
                get_executor(), batch_parse, filenames_to_parse
            )
            parsed_iter = iter(parsed_results)

        files = []
        for torrent in cached_torrents:
            info_hash = torrent["info_hash"]
            seeders = seeders_map.get(info_hash, 0)
            tracker = tracker_map.get(info_hash, "")
            sources = sources_map.get(info_hash, [])

            if is_offcloud:
                if is_episode_request:
                    # Strict matching for episode requests: offcloud does not expose
                    # per-file metadata here, so we cannot map a specific episode.
                    continue
                files.append(
                    {
                        "info_hash": info_hash,
                        "index": None,
                        "title": None,
                        "size": None,
                        "season": None,
                        "episode": None,
                        "parsed": None,
                    }
                )
                continue

            for file, filename in torrent["files"]:
                filename_parsed = next(parsed_iter)
                if filename_parsed is None:
                    continue

                parsed_season = (
                    filename_parsed.seasons[0] if filename_parsed.seasons else None
                )
                parsed_episode = (
                    filename_parsed.episodes[0] if filename_parsed.episodes else None
                )

                if is_episode_request:
                    if not self._strict_episode_match(
                        filename_parsed,
                        requested_season,
                        requested_episode,
                        target_air_date,
                    ):
                        continue
                    season = requested_season
                    episode = requested_episode
                else:
                    season = parsed_season
                    episode = parsed_episode

                file_info = {
                    "info_hash": info_hash,
                    "index": file["index"],
                    "title": filename,
                    "size": file["size"],
                    "season": season,
                    "episode": episode,
                    "parsed": filename_parsed,
                    "seeders": seeders,
                    "tracker": tracker,
                    "sources": sources,
                }

                files.append(file_info)

        files = select_best_availability_files(files)
        for file_info in files:
            if is_auxiliary_video(file_info["parsed"]):
                continue
            await torrent_update_queue.add_torrent_info(file_info, self.media_only_id)
        return files

    async def generate_download_link(
        self,
        hash: str,
        index: str,
        name: str,
        torrent_name: str,
        season: int,
        episode: int,
        sources: list | None = None,
        aliases: dict | None = None,
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
                "/magnets",
                {"magnet": magnet_uri},
                "add torrent to store",
                params={"client_ip": self.client_ip},
            )

            debrid_files = magnet["data"]["files"]
            if not debrid_files:
                raise DebridLinkGenerationError(
                    self.store_name,
                    f"{self.store_name}: Media is not cached yet.",
                    upstream_error_code="MEDIA_NOT_CACHED_YET",
                )

            name = unquote(name)
            torrent_name = unquote(torrent_name)
            aliases = aliases or {}
            ez_aliases_normalized = frozenset(
                normalized
                for alias in aliases.get("ez", [])
                if (normalized := normalize_title(alias))
            )

            video_files = []
            filenames_to_parse = []
            for file in debrid_files:
                filename = file["name"]
                if not is_video(filename):
                    continue

                video_files.append(file)
                filenames_to_parse.append(filename)

            if not video_files:
                return

            loop = asyncio.get_running_loop()
            parsed_results = await loop.run_in_executor(
                get_executor(), batch_parse, [*filenames_to_parse, torrent_name]
            )
            release_parsed = parsed_results.pop()

            scored_files = []
            (
                is_episode_request,
                season,
                episode,
                target_air_date,
            ) = await self._episode_request_context(self.media_only_id, season, episode)

            for file, filename, parsed in zip(
                video_files,
                filenames_to_parse,
                parsed_results,
                strict=True,
            ):
                if parsed is None:
                    continue
                if is_auxiliary_video(parsed, release_parsed):
                    continue
                file_index = file["index"] if file.get("index", -1) != -1 else None
                file_size = file["size"] if file.get("size", -1) != -1 else 0
                file_link = file.get("link")

                if not file_link:
                    continue

                file_season = parsed.seasons[0] if parsed.seasons else None
                file_episode = parsed.episodes[0] if parsed.episodes else None

                if is_episode_request:
                    if not self._strict_episode_match(
                        parsed,
                        season,
                        episode,
                        target_air_date,
                    ):
                        continue
                    file_season = season
                    file_episode = episode

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
                    # Exact alias match first
                    if exact_alias_match(
                        normalize_title(parsed.parsed_title), ez_aliases_normalized
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
                if is_episode_request:
                    raise DebridLinkGenerationError(
                        self.store_name,
                        f"{self.store_name}: No file matched requested episode.",
                        upstream_error_code="EPISODE_MATCH_NOT_FOUND",
                    )
                raise DebridLinkGenerationError(
                    self.store_name,
                    f"{self.store_name}: Media is not cached yet.",
                    upstream_error_code="MEDIA_NOT_CACHED_YET",
                )

            # Sort by score descending
            scored_files.sort(key=lambda x: x["score"], reverse=True)

            # Select best file
            target_file = scored_files[0]

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
                schedule_cache_availability(self.store_name, all_files_for_cache)

            link = await self._post_store_json(
                "/link/generate",
                {"link": target_file["link"]},
                "generate download link",
                params={"client_ip": self.client_ip},
            )

            link_url = valid_download_url(link["data"]["link"])
            if link_url is None:
                raise DebridLinkGenerationError(
                    self.store_name,
                    f"{self.store_name}: Failed to generate download link.",
                )

            return link_url
        except DebridLinkGenerationError:
            raise
        except (AttributeError, KeyError, TypeError, ValueError):
            raise DebridLinkGenerationError(
                self.store_name,
                f"{self.store_name}: Failed to generate download link.",
            ) from None
