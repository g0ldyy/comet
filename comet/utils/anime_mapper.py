import aiohttp
import orjson

from comet.utils.logger import logger


class AnimeMapper:
    def __init__(self):
        self.kitsu_to_imdb = {}
        self.imdb_to_kitsu = {}
        self.anime_imdb_ids = set()
        self.loaded = False

    async def load_anime_mapping(self, session: aiohttp.ClientSession):
        try:
            url = "https://raw.githubusercontent.com/Fribb/anime-lists/refs/heads/master/anime-list-full.json"
            response = await session.get(url)

            if response.status != 200:
                logger.error(f"Failed to load anime mapping: HTTP {response.status}")
                return False

            text = await response.text()
            data = orjson.loads(text)

            kitsu_count = 0
            imdb_count = 0

            for entry in data:
                kitsu_id = entry.get("kitsu_id")
                imdb_id = entry.get("imdb_id")

                if kitsu_id and imdb_id:
                    self.kitsu_to_imdb[kitsu_id] = imdb_id
                    self.imdb_to_kitsu[imdb_id] = kitsu_id
                    self.anime_imdb_ids.add(imdb_id)
                    imdb_count += 1

                if kitsu_id:
                    kitsu_count += 1

            self.loaded = True
            logger.log(
                "COMET",
                f"✅ Anime mapping loaded: {kitsu_count} Kitsu entries, {imdb_count} with IMDB IDs",
            )
            return True
        except Exception as e:
            logger.error(f"Exception while loading anime mapping: {e}")
            return False

    def get_imdb_from_kitsu(self, kitsu_id: int):
        return self.kitsu_to_imdb.get(kitsu_id)

    def get_kitsu_from_imdb(self, imdb_id: str):
        return self.imdb_to_kitsu.get(imdb_id)

    def is_anime(self, imdb_id: str):
        return imdb_id in self.anime_imdb_ids

    def is_loaded(self):
        return self.loaded


anime_mapper = AnimeMapper()
