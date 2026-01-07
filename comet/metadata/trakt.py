import aiohttp


async def get_trakt_aliases(
    session: aiohttp.ClientSession, media_type: str, media_id: str
):
    try:
        async with session.get(
            f"https://api.trakt.tv/{'movies' if media_type == 'movie' else 'shows'}/{media_id}/aliases"
        ) as response:
            data = await response.json()

        seen = {}
        for alias_entry in data:
            title = alias_entry.get("title")
            if title and title not in seen:
                seen[title] = None

        if seen:
            aliases_list = list(seen.keys())
            return {"ez": aliases_list}
    except Exception:
        pass

    return {}
