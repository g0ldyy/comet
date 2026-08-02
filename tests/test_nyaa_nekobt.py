import unittest

from comet.discovery.adapters.torrent.nekobt import NekoBTScraper
from comet.discovery.adapters.torrent.nyaa import extract_torrent_data


class _Response:
    status = 200

    def __init__(self, payload):
        self.payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        pass

    async def json(self):
        return self.payload


class _Session:
    def __init__(self, payload):
        self.payload = payload

    def get(self, url, **kwargs):
        return _Response(self.payload)


class _FailingSession:
    def get(self, url, **kwargs):
        raise RuntimeError("transport failed")


def _nyaa_row(title, info_hash, *, size="1.5 GiB", seeders="12"):
    return f"""
        <tr>
            <td><a href="/view/1" title="{title}">{title}</a></td>
            <td><a href="magnet:?xt=urn:btih:{info_hash}&amp;tr=udp%3A%2F%2Ftracker.test">magnet</a></td>
            <td class="text-center">{size}</td>
            <td class="text-center">{seeders}</td>
            <td class="text-center">2</td>
            <td class="text-center">3</td>
        </tr>
    """


class NyaaNekoBTTests(unittest.IsolatedAsyncioTestCase):
    def test_nyaa_rejects_a_malformed_candidate_row(self):
        malformed = f"""
            <tr>
                <td><a href="/view/2" title="Broken.Movie">Broken.Movie</a></td>
                <td><a href="magnet:?xt=urn:btih:{"b" * 40}">magnet</a></td>
            </tr>
        """
        with self.assertRaisesRegex(ValueError, "incomplete"):
            extract_torrent_data(
                _nyaa_row("First &amp; Movie", "a" * 40)
                + malformed
                + _nyaa_row("Second.Movie", "c" * 40, size="2 GiB", seeders="3")
            )

    async def test_nekobt_rejects_a_malformed_result(self):
        payload = {
            "error": False,
            "data": {
                "results": [
                    {
                        "infohash": "a" * 40,
                        "title": "First.Movie",
                        "magnet": "magnet:?xt=urn:btih:first",
                        "seeders": "12",
                        "filesize": "1000",
                    },
                    None,
                    {"infohash": "b" * 40, "title": "Broken.Movie"},
                    {
                        "infohash": "c" * 40,
                        "title": "Second.Movie",
                        "magnet": "magnet:?xt=urn:btih:second",
                        "seeders": 3,
                        "filesize": 2000,
                    },
                ],
                "recommended_media": {"id": 42},
                "similar_media": [None],
                "more": False,
            },
        }
        scraper = NekoBTScraper(None, _Session(payload))

        with self.assertRaisesRegex(ValueError, "not an object"):
            await scraper._fetch_page({})

    async def test_nekobt_propagates_transport_failures(self):
        scraper = NekoBTScraper(None, _FailingSession())

        with self.assertRaisesRegex(RuntimeError, "transport failed"):
            await scraper._fetch_page({"query": "Movie"})
