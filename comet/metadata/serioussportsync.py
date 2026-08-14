import re
from dataclasses import dataclass
from urllib.parse import quote, urlsplit, urlunsplit

from comet.core.models import settings

_EVENT_ID = re.compile(r"[a-z0-9][a-z0-9_-]*:[A-Za-z0-9][A-Za-z0-9._-]*")
_FIXTURE_SEPARATOR = re.compile(
    r"\s+(?:v(?:s)?\.?|versus|at)\s+|\s*@\s*", re.IGNORECASE
)
_SPLIT_EVENT_DAY_SUFFIX = re.compile(
    r"\s+(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)$",
    re.IGNORECASE,
)


class SeriousSportSyncResolverError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SeriousSportSyncEvent:
    title: str
    search_titles: tuple[str, ...]
    date: str | None
    year: int | None
    country: str | None


def is_serioussportsync_event_id(media_type: str, media_id: str) -> bool:
    return (
        media_type == "movie"
        and media_id != "kitsu"
        and _EVENT_ID.fullmatch(media_id) is not None
        and not media_id.startswith("kitsu:")
    )


def build_event_search_titles(title: str, aliases: list[str]) -> tuple[str, ...]:
    titles = []
    seen = set()

    def append(candidate: object):
        if not isinstance(candidate, str):
            return
        candidate = " ".join(candidate.split())
        identity = candidate.casefold()
        if not candidate or identity in seen:
            return
        seen.add(identity)
        titles.append(candidate)

    for candidate in (title, *aliases):
        append(candidate)
        if isinstance(candidate, str):
            append(_FIXTURE_SEPARATOR.sub(" ", candidate))
            # Some indexers use strict full-text matching and return no results for
            # split-day labels such as "WWE SummerSlam Sunday", even though the
            # release is indexed under the shared event name "WWE SummerSlam".
            # Keep the precise forms first, then add the safe base-event form.
            base_event = _SPLIT_EVENT_DAY_SUFFIX.sub("", candidate)
            if base_event != candidate:
                append(base_event)
                append(_FIXTURE_SEPARATOR.sub(" ", base_event))
    return tuple(titles[:24])


def _allowed_manifest_url(manifest_url: str):
    parsed = urlsplit(manifest_url)
    allowed_hosts = {
        host.strip().casefold()
        for host in (settings.SERIOUSSPORTSYNC_ALLOWED_HOSTS or "").split(",")
        if host.strip()
    }
    if parsed.netloc.casefold() not in allowed_hosts:
        raise SeriousSportSyncResolverError(
            "SeriousSportSync manifest host is not allowed by this Comet instance"
        )
    return parsed


async def resolve_serioussportsync_event(
    session,
    manifest_url: str,
    media_type: str,
    media_id: str,
) -> SeriousSportSyncEvent:
    parsed = _allowed_manifest_url(manifest_url)
    context_path = parsed.path.removesuffix("manifest.json") + (
        f"search-context/{quote(media_type, safe='')}/{quote(media_id, safe='')}.json"
    )
    context_url = urlunsplit((parsed.scheme, parsed.netloc, context_path, "", ""))

    try:
        async with session.get(context_url) as response:
            if response.status != 200:
                raise SeriousSportSyncResolverError(
                    f"SeriousSportSync returned HTTP {response.status}"
                )
            payload = await response.json()
    except SeriousSportSyncResolverError:
        raise
    except Exception as error:
        raise SeriousSportSyncResolverError(
            f"Unable to resolve SeriousSportSync event: {error}"
        ) from error

    if not isinstance(payload, dict):
        raise SeriousSportSyncResolverError("Invalid SeriousSportSync response")
    title = payload.get("title")
    aliases = payload.get("searchTitles", [])
    date = payload.get("date")
    year = payload.get("year")
    country = payload.get("country")
    if not isinstance(title, str) or not title.strip():
        raise SeriousSportSyncResolverError("SeriousSportSync response has no title")
    if not isinstance(aliases, list):
        aliases = []
    aliases = [alias for alias in aliases if isinstance(alias, str) and alias.strip()]
    if date is not None and not isinstance(date, str):
        date = None
    if not isinstance(year, int):
        year = None
    if country is not None and not isinstance(country, str):
        country = None

    return SeriousSportSyncEvent(
        title=title.strip(),
        search_titles=build_event_search_titles(title, aliases),
        date=date,
        year=year,
        country=country,
    )
