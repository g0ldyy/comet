import aiohttp
import asyncio
import orjson
import time

from RTN import parse, title_match, ParsedData

from comet.utils.models import settings, database

from .zilean import get_zilean
from .torrentio import get_torrentio
from .mediafusion import get_mediafusion


class TorrentScraper:
    def __init__(
        self,
        media_type: str,
        media_id: str,
        title: str,
        year: int,
        year_end: int,
        season: int,
        episode: int,
        aliases: dict,
        remove_adult_content: bool,
    ):
        self.media_type = media_type
        self.media_id = media_id
        self.title = title
        self.year = year
        self.year_end = year_end
        self.season = season
        self.episode = episode
        self.aliases = aliases
        self.remove_adult_content = remove_adult_content

        self.seenHashes = set()
        self.torrents = []

    async def scrape_torrents(
        self,
        session: aiohttp.ClientSession,
    ):
        tasks = []
        if settings.SCRAPE_TORRENTIO:
            tasks.append(get_torrentio(self, self.media_type, self.media_id))

        if settings.SCRAPE_MEDIAFUSION:
            tasks.append(get_mediafusion(self, self.media_type, self.media_id))

        if settings.SCRAPE_ZILEAN:
            tasks.append(
                get_zilean(self, session, self.title, self.season, self.episode)
            )

        await asyncio.gather(*tasks)

        await self.cache_torrents()

    async def get_cached_torrents(self):
        rows = await database.fetch_all(
            """
                SELECT data
                FROM torrents_cache
                WHERE media_id = :media_id
                AND ((cast(:season as INTEGER) IS NULL AND season IS NULL) OR season = cast(:season as INTEGER))
                AND ((cast(:episode as INTEGER) IS NULL AND episode IS NULL) OR episode = cast(:episode as INTEGER))
                AND timestamp + :cache_ttl >= :current_time
            """,
            {
                "media_id": self.media_id,
                "season": self.season,
                "episode": self.episode,
                "cache_ttl": settings.CACHE_TTL,
                "current_time": time.time(),
            },
        )
        for row in rows:
            data = orjson.loads(row["data"])

            data["parsed"] = ParsedData(**data["parsed"])

            self.torrents.append(data)

    async def cache_torrents(self):
        def default(obj):
            if isinstance(obj, ParsedData):
                return obj.model_dump()

        current_time = time.time()
        values = [
            {
                "info_hash": torrent["infoHash"],
                "media_id": self.media_id,
                "season": self.season,
                "episode": self.episode,
                "data": orjson.dumps(torrent, default),
                "timestamp": current_time,
            }
            for torrent in self.torrents
        ]

        query = f"""
            INSERT {'OR IGNORE ' if settings.DATABASE_TYPE == 'sqlite' else ''}
            INTO torrents_cache (info_hash, media_id, season, episode, data, timestamp)
            VALUES (:info_hash, :media_id, :season, :episode, :data, :timestamp)
            {' ON CONFLICT DO NOTHING' if settings.DATABASE_TYPE == 'postgresql' else ''}
        """

        await database.execute_many(query, values)

    async def filter(self, torrents: list):
        title = self.title
        year = self.year
        year_end = self.year_end
        aliases = self.aliases
        remove_adult_content = self.remove_adult_content

        for torrent in torrents:
            parsed = parse(torrent["title"])

            if remove_adult_content and parsed.adult:
                continue

            if parsed.parsed_title and not title_match(
                title, parsed.parsed_title, aliases=aliases
            ):
                continue

            if year and parsed.year:
                if year_end is not None:
                    if not (year <= parsed.year <= year_end):
                        continue
                else:
                    if year < (parsed.year - 1) or year > (parsed.year + 1):
                        continue

            torrent["parsed"] = parsed

            self.torrents.append(torrent)

    async def filter_manager(self, torrents: list):
        new_torrents = [
            torrent
            for torrent in torrents
            if torrent["infoHash"] not in self.seenHashes
        ]
        self.seenHashes.update(torrent["infoHash"] for torrent in new_torrents)

        chunk_size = 50
        tasks = []
        for i in range(0, len(new_torrents), chunk_size):
            chunk = new_torrents[i : i + chunk_size]
            tasks.append(self.filter(chunk))

        await asyncio.gather(*tasks)
