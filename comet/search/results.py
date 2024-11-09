import aiohttp
import bencodepy
import hashlib
import re

from RTN import parse, title_match
from comet.utils.logger import logger
from comet.utils.models import settings

info_hash_pattern = re.compile(r"\b([a-fA-F0-9]{40})\b")

class SearchResult:
    def __init__(self, title: str, info_hash: str, tracker: str, size: str = None,  link: str = None):
        self.title = title
        if "\n" in title:
          # Torrentio title parsing
          self.title = title.split("\n")[1]
        self.parsed = parse(self.title)
        self.info_hash = info_hash.lower() if info_hash else None
        self.tracker = tracker
        self.size = size
        self.link = link

    def matches_title(self,
                      title: str,
                      year: int,
                      year_end: int,
                      aliases: dict,
                      remove_adult_content: bool):
        if self.parsed.parsed_title and not title_match(title, self.parsed.parsed_title, aliases=aliases):
            return False
        if remove_adult_content and self.parsed.adult:
            return False
        if year and self.parsed.year:
            if year_end:
                if not year <= self.parsed.year <= year_end:
                    return False
            else:
                if not (self.parsed.year - 1) <= year <= (self.parsed.year + 1):
                    return False
        return True

    async def fetch_hash(self, session: aiohttp.ClientSession):
        if self.info_hash is not None:
            return

        try:
            timeout = aiohttp.ClientTimeout(total=settings.GET_TORRENT_TIMEOUT)
            response = await session.get(self.link, allow_redirects=False, timeout=timeout)
            if response.status == 200:
                torrent_data = await response.read()
                torrent_dict = bencodepy.decode(torrent_data)
                info = bencodepy.encode(torrent_dict[b"info"])
                hash = hashlib.sha1(info).hexdigest()
            else:
                location = response.headers.get("Location", "")
                if not location:
                    return None

                match = info_hash_pattern.search(location)
                if not match:
                    return None

                hash = match.group(1).upper()

            self.info_hash = hash.lower()
        except Exception as e:
            logger.warning(
                f"Exception while getting torrent info hash for {self.tracker}|<<url>>: {e}"
            )
            return
