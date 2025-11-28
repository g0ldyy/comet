from RTN import Torrent, check_fetch, get_rank, sort_torrents


def rank_worker(
    torrents,
    debrid_service,
    rtn_settings,
    rtn_ranking,
    max_results_per_resolution,
    max_size,
    cached_only,
    remove_trash,
):
    ranked_torrents = set()
    for info_hash, torrent in torrents.items():
        if cached_only and debrid_service != "torrent" and not torrent["cached"]:
            continue

        if max_size != 0 and torrent["size"] > max_size:
            continue

        parsed = torrent["parsed"]
        raw_title = torrent["title"]

        is_fetchable, failed_keys = check_fetch(parsed, rtn_settings)
        rank = get_rank(parsed, rtn_settings, rtn_ranking)

        if remove_trash:
            if not is_fetchable or rank < rtn_settings.options["remove_ranks_under"]:
                continue

        try:
            ranked_torrents.add(
                Torrent(
                    infohash=info_hash,
                    raw_title=raw_title,
                    data=parsed,
                    fetch=is_fetchable,
                    rank=rank,
                    lev_ratio=0.0,
                )
            )
        except Exception:
            pass

    return sort_torrents(ranked_torrents, max_results_per_resolution)
