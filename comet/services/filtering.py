from RTN import parse, title_match


def filter_worker(torrents, title, year, year_end, aliases, remove_adult_content):
    # todo: log reason for each filtered torrent
    results = []
    for torrent in torrents:
        torrent_title = torrent["title"]
        if "sample" in torrent_title.lower() or torrent_title == "":
            continue

        parsed = parse(torrent_title)

        if remove_adult_content and parsed.adult:
            continue

        if not parsed.parsed_title or not title_match(
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
        results.append(torrent)
    return results
