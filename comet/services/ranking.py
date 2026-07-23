from RTN import Torrent, check_fetch_and_rank_many, sort_torrents
from RTN.exceptions import GarbageTorrent


def rank_worker(
    torrents,
    rtn_settings,
    rtn_ranking,
    max_results_per_resolution,
    max_size,
    remove_trash,
):
    ranked_torrents = set()
    eligible_torrents = []
    for info_hash, torrent in torrents.items():
        if max_size != 0:
            torrent_size = torrent["size"]
            if torrent_size is not None and torrent_size > max_size:
                continue

        eligible_torrents.append((info_hash, torrent))

    rank_results = check_fetch_and_rank_many(
        (torrent["parsed"] for _, torrent in eligible_torrents),
        rtn_settings,
        rtn_ranking,
    )

    for (info_hash, torrent), (is_fetchable, _, rank) in zip(
        eligible_torrents, rank_results, strict=True
    ):
        parsed = torrent["parsed"]
        raw_title = torrent["title"]

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
        except GarbageTorrent:
            pass

    return sort_torrents(ranked_torrents, max_results_per_resolution)
