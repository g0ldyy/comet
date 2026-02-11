import json
import re
import sys
from urllib import parse

import xbmc
import xbmcgui
import xbmcplugin

from .parser import parse_stream_info
from .utils import (ADDON_HANDLE, ADDON_ID, build_url,
                    convert_info_hash_to_magnet, ensure_configured, fetch_data,
                    get_base_url, get_catalog_provider_url, get_config_prefix,
                    is_elementum_installed_and_enabled, log)

CATALOG_PAGE_SIZE = 25
SUPPORTED_CATALOG_TYPES = {"movie", "series"}
SERIES_CATALOG_EXCLUDED_NAMES = {"last videos", "calendar videos"}

_YEAR_RE = re.compile(r"\d{4}")
_CATALOG_PRIORITY_MAP = {"popular": 0, "new": 1, "featured": 2}
_TAGLINE_KEYS = (
    "videoInfo",
    "audioInfo",
    "qualityInfo",
    "groupInfo",
    "seedersInfo",
    "sizeInfo",
    "trackerInfo",
    "languagesInfo",
)
_PROVIDER_CONTEXT_CACHE: tuple[str, str] | None = None


def _compose_url(base_url: str, path: str):
    return parse.urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))


def _provider_context():
    global _PROVIDER_CONTEXT_CACHE
    if _PROVIDER_CONTEXT_CACHE is not None:
        return _PROVIDER_CONTEXT_CACHE

    configured = get_catalog_provider_url()
    if configured.endswith("/manifest.json"):
        context = (configured, configured[: -len("/manifest.json")])
    elif configured.endswith(".json"):
        context = (configured, configured.rsplit("/", 1)[0])
    else:
        context = (f"{configured}/manifest.json", configured)

    _PROVIDER_CONTEXT_CACHE = context
    return context


def _provider_path(value: str):
    return parse.quote(str(value), safe="")


def _fetch_provider_manifest():
    manifest_url, _ = _provider_context()
    return fetch_data(manifest_url)


def _fetch_provider_meta(catalog_type: str, video_id: str):
    _, provider_base_url = _provider_context()
    response = fetch_data(
        _compose_url(
            provider_base_url,
            f"meta/{_provider_path(catalog_type)}/{_provider_path(video_id)}.json",
        )
    )
    return response["meta"] if response else None


def _catalog_url(catalog_type: str, catalog_id: str, extra: str):
    _, provider_base_url = _provider_context()
    return _compose_url(
        provider_base_url,
        f"catalog/{_provider_path(catalog_type)}/{_provider_path(catalog_id)}/{extra}.json",
    )


def _catalog_specs(manifest: dict, catalog_type: str):
    specs = []
    for catalog in manifest.get("catalogs", ()):
        if catalog["type"] != catalog_type:
            continue

        catalog_id = catalog.get("id")
        if not catalog_id:
            continue

        catalog_name = catalog.get("name") or catalog_id
        if (
            catalog_type == "series"
            and catalog_name.strip().lower() in SERIES_CATALOG_EXCLUDED_NAMES
        ):
            continue

        has_search = any(e.get("name") == "search" for e in catalog.get("extra", ()))
        specs.append({"id": catalog_id, "name": catalog_name, "has_search": has_search})
    return specs


def _catalog_priority(name: str):
    return _CATALOG_PRIORITY_MAP.get(name.strip().lower(), 100)


def _parse_release_year(release_info):
    if not release_info:
        return None
    match = _YEAR_RE.search(str(release_info))
    return int(match.group()) if match else None


def _upgrade_metahub_url(url: str | None):
    if url and "/poster/small/" in url:
        return url.replace("/poster/small/", "/poster/medium/")
    return url or None


def _set_ids(tags, stremio_id: str):
    if stremio_id.startswith("tt"):
        tags.setIMDBNumber(stremio_id)
        tags.setUniqueID(stremio_id, type="imdb")
    else:
        tags.setUniqueID(stremio_id, type="comet")


def _set_video_tags(tags, meta: dict, title: str):
    tags.setTitle(title)

    description = meta.get("description")
    if description:
        tags.setPlot(description)

    imdb_rating = meta.get("imdbRating")
    if imdb_rating:
        try:
            tags.setRating(float(imdb_rating))
        except (TypeError, ValueError):
            pass

    release_year = _parse_release_year(meta.get("releaseInfo"))
    if release_year:
        tags.setYear(release_year)

    genres = meta.get("genres")
    if genres:
        tags.setGenres(genres)


def _build_art(primary: str | None, poster: str | None, background: str | None):
    art = {}
    if primary:
        art["thumb"] = primary
        art["poster"] = primary
        art["icon"] = primary
        art["fanart"] = primary
        art["landscape"] = primary
        art["banner"] = primary
    if poster:
        art.setdefault("poster", poster)
        art.setdefault("icon", poster)
        art.setdefault("thumb", poster)
    if background:
        art.setdefault("fanart", background)
        art.setdefault("landscape", background)
        art.setdefault("banner", background)
    return art


def _set_art(list_item, meta: dict):
    poster = _upgrade_metahub_url(meta.get("poster"))
    background = _upgrade_metahub_url(meta.get("background")) or poster
    art = _build_art(None, poster, background)
    if art:
        list_item.setArt(art)


def _season_thumbnails(videos: list):
    thumbnails = {}
    for video in videos:
        season = video.get("season")
        thumbnail = video.get("thumbnail")
        if season is None or not thumbnail:
            continue

        episode_number = video.get("episode") or video.get("number") or 0
        current = thumbnails.get(season)
        if current is None or episode_number < current[0]:
            thumbnails[season] = (episode_number, thumbnail)

    return {season: value[1] for season, value in thumbnails.items()}


def _episode_number(video: dict):
    number = video.get("episode")
    if number is None:
        number = video.get("number")
    return number


def _set_episode_art(list_item, video: dict, meta: dict):
    episode_thumb = _upgrade_metahub_url(video.get("thumbnail"))
    poster = _upgrade_metahub_url(meta.get("poster"))
    background = _upgrade_metahub_url(meta.get("background"))
    art = _build_art(episode_thumb, poster, episode_thumb or background or poster)
    if art:
        list_item.setArt(art)


def _set_season_art(list_item, meta: dict, season_thumbnail: str | None):
    season_thumb = _upgrade_metahub_url(season_thumbnail)
    poster = _upgrade_metahub_url(meta.get("poster"))
    background = _upgrade_metahub_url(meta.get("background")) or poster
    art = _build_art(season_thumb, poster, background)
    if art:
        list_item.setArt(art)


def _stream_tagline(video_info: dict):
    parts = (video_info.get(key) for key in _TAGLINE_KEYS)
    return " | ".join(part for part in parts if part)


def _add_directory_items(items: list, total_items: int | None = None):
    if not items:
        return
    xbmcplugin.addDirectoryItems(
        ADDON_HANDLE,
        items,
        len(items) if total_items is None else total_items,
    )


def _process_catalog_items(videos: list, catalog_type: str):
    xbmcplugin.setContent(
        ADDON_HANDLE, "movies" if catalog_type == "movie" else "tvshows"
    )

    action = "list_seasons" if catalog_type == "series" else "get_streams"
    items = []

    for video in videos:
        video_id = video["id"]
        video_name = video["name"]
        list_item = xbmcgui.ListItem(label=video_name, offscreen=True)

        tags = list_item.getVideoInfoTag()
        _set_ids(tags, video_id)
        _set_video_tags(tags, video, video_name)
        _set_art(list_item, video)

        items.append(
            (
                build_url(action, catalog_type=catalog_type, video_id=video_id),
                list_item,
                True,
            )
        )

    _add_directory_items(items)


def _notify_error(message: str):
    xbmcgui.Dialog().notification("Comet", message, xbmcgui.NOTIFICATION_ERROR)


def list_root():
    if not ensure_configured():
        return

    manifest = _fetch_provider_manifest()
    if not manifest:
        xbmcplugin.endOfDirectory(ADDON_HANDLE)
        return

    movie_specs = _catalog_specs(manifest, "movie")
    series_specs = _catalog_specs(manifest, "series")

    if not movie_specs and not series_specs:
        _notify_error("No compatible catalogs found")
    else:
        items = []
        if movie_specs:
            items.append(
                (
                    build_url("list_catalog_type", catalog_type="movie"),
                    xbmcgui.ListItem(label="Movies"),
                    True,
                )
            )
        if series_specs:
            items.append(
                (
                    build_url("list_catalog_type", catalog_type="series"),
                    xbmcgui.ListItem(label="Series"),
                    True,
                )
            )
        _add_directory_items(items)

    xbmcplugin.endOfDirectory(ADDON_HANDLE)


def open_settings(_params):
    xbmc.executebuiltin(
        f"RunScript(special://home/addons/{ADDON_ID}/lib/custom_settings_window.py)"
    )


def list_catalog_type(params):
    if not ensure_configured():
        return

    catalog_type = params["catalog_type"]
    if catalog_type not in SUPPORTED_CATALOG_TYPES:
        _notify_error("Unsupported catalog type")
        return

    manifest = _fetch_provider_manifest()
    if not manifest:
        return

    specs = _catalog_specs(manifest, catalog_type)
    if not specs:
        _notify_error("No catalogs available")
        return

    specs.sort(key=lambda spec: (_catalog_priority(spec["name"]), spec["name"].lower()))
    search_catalog_id = next((spec["id"] for spec in specs if spec["has_search"]), None)

    items = []
    if search_catalog_id is not None:
        items.append(
            (
                build_url(
                    "search_catalog",
                    catalog_type=catalog_type,
                    catalog_id=search_catalog_id,
                ),
                xbmcgui.ListItem(label="Search"),
                True,
            )
        )

    seen_labels = set()
    for spec in specs:
        label = spec["name"]
        if label in seen_labels:
            label = f"{label} ({spec['id']})"
        seen_labels.add(label)

        items.append(
            (
                build_url(
                    "list_catalog",
                    catalog_type=catalog_type,
                    catalog_id=spec["id"],
                ),
                xbmcgui.ListItem(label=label),
                True,
            )
        )

    _add_directory_items(items)
    xbmcplugin.endOfDirectory(ADDON_HANDLE)


def list_catalog(params):
    if not ensure_configured():
        return

    catalog_type = params["catalog_type"]
    catalog_id = params["catalog_id"]
    skip = int(params.get("skip", "0"))

    response = fetch_data(_catalog_url(catalog_type, catalog_id, f"skip={skip}"))
    if not response:
        return

    videos = response.get("metas", ())
    if not videos:
        _notify_error("No videos available")
        return

    _process_catalog_items(videos, catalog_type)

    if len(videos) >= CATALOG_PAGE_SIZE:
        _add_directory_items(
            [
                (
                    build_url(
                        "list_catalog",
                        catalog_type=catalog_type,
                        catalog_id=catalog_id,
                        skip=skip + len(videos),
                    ),
                    xbmcgui.ListItem(label="Next Page"),
                    True,
                )
            ]
        )

    xbmcplugin.endOfDirectory(ADDON_HANDLE)


def search_catalog(params):
    if not ensure_configured():
        return

    query = xbmcgui.Dialog().input("Search", type=xbmcgui.INPUT_ALPHANUM)
    if not query:
        return

    catalog_type = params["catalog_type"]
    catalog_id = params["catalog_id"]
    response = fetch_data(
        _catalog_url(catalog_type, catalog_id, f"search={parse.quote(query, safe='')}")
    )
    if not response:
        return

    videos = response.get("metas", ())
    if not videos:
        _notify_error("No results found")
        return

    _process_catalog_items(videos, catalog_type)
    xbmcplugin.endOfDirectory(ADDON_HANDLE)


def list_seasons(params):
    if not ensure_configured():
        return

    catalog_type = params["catalog_type"]
    video_id = params["video_id"]

    meta = _fetch_provider_meta(catalog_type, video_id)
    if not meta:
        return

    videos = meta.get("videos", ())
    if not videos:
        _notify_error("No seasons available")
        return

    xbmcplugin.setContent(ADDON_HANDLE, "episodes")

    season_thumbnails = _season_thumbnails(videos)
    seasons = sorted(
        {
            season
            for video in videos
            for season in [video.get("season")]
            if season is not None
        }
    )
    if 0 in seasons:
        seasons = [season for season in seasons if season != 0] + [0]

    show_title = meta.get("name") or ""
    items = []
    for season in seasons:
        label = "Specials" if season == 0 else f"Season {season}"
        list_item = xbmcgui.ListItem(label=label, offscreen=True)
        tags = list_item.getVideoInfoTag()
        tags.setTitle(label)
        tags.setTvShowTitle(show_title)
        _set_season_art(list_item, meta, season_thumbnails.get(season))

        items.append(
            (
                build_url(
                    "list_episodes",
                    catalog_type=catalog_type,
                    video_id=video_id,
                    season=season,
                ),
                list_item,
                True,
            )
        )

    _add_directory_items(items)
    xbmcplugin.endOfDirectory(ADDON_HANDLE)


def list_episodes(params):
    if not ensure_configured():
        return

    catalog_type = params["catalog_type"]
    video_id = params["video_id"]
    selected_season = int(params["season"])

    meta = _fetch_provider_meta(catalog_type, video_id)
    if not meta:
        return

    videos = meta.get("videos", ())
    if not videos:
        _notify_error("No episodes available")
        return

    xbmcplugin.setContent(ADDON_HANDLE, "episodes")
    season_videos = sorted(
        (video for video in videos if video.get("season") == selected_season),
        key=lambda video: _episode_number(video) or 0,
    )

    show_title = meta.get("name") or ""
    meta_description = meta.get("description")
    meta_genres = meta.get("genres")
    meta_release_info = meta.get("releaseInfo")

    items = []
    for video in season_videos:
        episode_number = _episode_number(video)
        if episode_number is None:
            continue

        title = video.get("name") or video.get("title") or f"Episode {episode_number}"
        list_item = xbmcgui.ListItem(label=title, offscreen=True)
        tags = list_item.getVideoInfoTag()
        _set_ids(tags, video_id)
        tags.setTitle(title)
        tags.setTvShowTitle(show_title)
        tags.setSeason(selected_season)
        tags.setEpisode(int(episode_number))

        plot = video.get("overview") or meta_description
        if plot:
            tags.setPlot(plot)

        release_year = _parse_release_year(video.get("released") or meta_release_info)
        if release_year:
            tags.setYear(release_year)

        if meta_genres:
            tags.setGenres(meta_genres)

        _set_episode_art(list_item, video, meta)
        items.append(
            (
                build_url(
                    "get_streams",
                    catalog_type=catalog_type,
                    video_id=f"{video_id}:{selected_season}:{episode_number}",
                ),
                list_item,
                True,
            )
        )

    if not items:
        _notify_error("No episodes available")
        return

    _add_directory_items(items)
    xbmcplugin.endOfDirectory(ADDON_HANDLE)


def get_streams(params):
    if not ensure_configured():
        return

    catalog_type = params["catalog_type"]
    video_id = params["video_id"]
    stream_url = _compose_url(
        get_base_url(),
        f"{get_config_prefix()}stream/{catalog_type}/{video_id}.json?kodi=1",
    )

    response = fetch_data(stream_url)
    if not response:
        return

    streams = response.get("streams", ())
    if not streams:
        _notify_error("No streams available")
        return

    xbmcplugin.setContent(ADDON_HANDLE, "files")

    id_parts = video_id.split(":", 2)
    if len(id_parts) == 3:
        imdb_id, season, episode = id_parts
        season_number = int(season)
        episode_number = int(episode)
    else:
        imdb_id = video_id
        season = None
        episode = None
        season_number = None
        episode_number = None
    is_imdb = imdb_id.startswith("tt")

    stream_items = []
    stream_count = len(streams)
    elementum_available = None
    elementum_warning_sent = False

    for stream in streams:
        stream_name = stream["name"]
        stream_description = stream["description"]
        behavior_hints = stream.get("behaviorHints", {})
        video_info = parse_stream_info(stream_name, stream_description, behavior_hints)
        stream_tagline = _stream_tagline(video_info)

        list_item = xbmcgui.ListItem(
            label=stream_name, label2=stream_tagline, offscreen=True
        )
        tags = list_item.getVideoInfoTag()
        tags.setTitle(stream_name)
        tags.setPlot(stream_description)
        if stream_tagline:
            tags.setTagLine(stream_tagline)

        if is_imdb:
            tags.setIMDBNumber(imdb_id)
        if season is not None:
            tags.setSeason(season_number)
            tags.setEpisode(episode_number)
            tags.setMediaType("episode")
        else:
            tags.setMediaType("video")

        size = video_info["size"]
        if size:
            list_item.setProperty("size", str(size))

        tags.addVideoStream(
            xbmc.VideoStreamDetail(
                width=int(video_info["width"]),
                height=int(video_info["height"]),
                language=video_info["language"],
                codec=video_info["codec"],
                hdrtype=video_info["hdr"],
            )
        )
        list_item.setProperty("IsPlayable", "true")

        if "url" in stream:
            resolved_stream_url = stream["url"]
        elif "infoHash" in stream:
            if elementum_available is None:
                elementum_available = is_elementum_installed_and_enabled()
            if not elementum_available:
                if not elementum_warning_sent:
                    _notify_error("Elementum is required for torrent playback.")
                    elementum_warning_sent = True
                continue

            magnet_link = convert_info_hash_to_magnet(
                stream["infoHash"],
                stream.get("sources", []),
                behavior_hints.get("filename", stream_name),
            )
            resolved_stream_url = (
                "plugin://plugin.video.elementum/play?uri="
                + parse.quote_plus(magnet_link)
            )
        else:
            continue

        playback_params = {"video_url": resolved_stream_url}
        if is_imdb:
            playback_params["imdb"] = imdb_id
        if season is not None:
            playback_params["season"] = season
            playback_params["episode"] = episode

        stream_items.append(
            (build_url("play_video", **playback_params), list_item, False)
        )

    _add_directory_items(stream_items, stream_count)
    xbmcplugin.endOfDirectory(ADDON_HANDLE)


def play_video(params):
    video_url = params["video_url"]
    imdb = params.get("imdb")
    season = params.get("season")
    episode = params.get("episode")

    list_item = xbmcgui.ListItem(path=video_url)
    tags = list_item.getVideoInfoTag()

    if season and episode:
        tags.setSeason(int(season))
        tags.setEpisode(int(episode))
    if imdb:
        tags.setIMDBNumber(imdb)
        xbmcgui.Window(10000).setProperty(
            "script.trakt.ids", json.dumps({"imdb": imdb})
        )

    xbmcplugin.setResolvedUrl(ADDON_HANDLE, True, list_item)


_ACTIONS = {
    "open_settings": open_settings,
    "list_catalog_type": list_catalog_type,
    "list_catalog": list_catalog,
    "search_catalog": search_catalog,
    "list_seasons": list_seasons,
    "list_episodes": list_episodes,
    "get_streams": get_streams,
    "play_video": play_video,
}


def addon_router():
    param_string = sys.argv[2][1:]

    if param_string:
        params = dict(parse.parse_qsl(param_string))
        action = params.get("action")
        action_handler = _ACTIONS.get(action)
        if action_handler:
            action_handler(params)
            return

    log("Opening root menu", xbmc.LOGINFO)
    list_root()
