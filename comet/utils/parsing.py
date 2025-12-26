from RTN import ParsedData


def is_video(title: str):
    video_extensions = (
        ".3g2",
        ".3gp",
        ".amv",
        ".asf",
        ".avi",
        ".drc",
        ".f4a",
        ".f4b",
        ".f4p",
        ".f4v",
        ".flv",
        ".gif",
        ".gifv",
        ".m2v",
        ".m4p",
        ".m4v",
        ".mkv",
        ".mov",
        ".mp2",
        ".mp4",
        ".mpg",
        ".mpeg",
        ".mpv",
        ".mng",
        ".mpe",
        ".mxf",
        ".nsv",
        ".ogg",
        ".ogv",
        ".qt",
        ".rm",
        ".rmvb",
        ".roq",
        ".svi",
        ".webm",
        ".wmv",
        ".yuv",
    )
    return title.endswith(video_extensions)


def default_dump(obj):
    if isinstance(obj, ParsedData):
        return obj.model_dump()


def _parse_optional_int(value: str):
    if value is None or value == "":
        return None
    try:
        return int(value)
    except ValueError:
        return None


def parse_media_id(media_type: str, media_id: str):
    if "kitsu" in media_id:
        info = media_id.split(":")

        if len(info) > 2:
            return info[1], 1, _parse_optional_int(info[2])
        else:
            return info[1], 1, None

    if media_type == "series":
        info = media_id.split(":")
        series_id = info[0]
        season = _parse_optional_int(info[1]) if len(info) > 1 else None
        episode = _parse_optional_int(info[2]) if len(info) > 2 else None
        return series_id, season, episode

    return media_id, None, None


def associate_urls_credentials(urls, credentials):
    if not urls:
        return []

    if isinstance(urls, str):
        urls = [urls]

    if len(urls) == 1:
        if credentials is None:
            credential = None
        elif isinstance(credentials, str):
            credential = credentials or None
        elif isinstance(credentials, list) and len(credentials) > 0:
            credential = credentials[0]
        else:
            credential = None

        credentials_list = [credential]
    else:
        if credentials is None:
            credentials_list = [None] * len(urls)
        elif isinstance(credentials, str):
            credentials_list = [credentials or None] * len(urls)
        elif isinstance(credentials, list):
            credentials_list = []
            for i in range(len(urls)):
                if i < len(credentials):
                    cred = credentials[i] or None
                    credentials_list.append(cred)
                else:
                    credentials_list.append(None)

    return list(zip(urls, credentials_list))
