import importlib
import sys
import types
import unittest
from pathlib import Path

PLUGIN_ROOT = Path(__file__).parents[1] / "kodi" / "plugin.video.comet"


def _load_router():
    xbmc = types.ModuleType("xbmc")
    xbmc.LOGINFO = 1
    xbmc.LOGERROR = 2

    xbmcaddon = types.ModuleType("xbmcaddon")

    class Addon:
        def getAddonInfo(self, key):
            return "plugin.video.comet" if key == "id" else ""

        def getSetting(self, key):
            del key
            return ""

    xbmcaddon.Addon = Addon

    xbmcgui = types.ModuleType("xbmcgui")
    xbmcgui.NOTIFICATION_ERROR = 1
    xbmcplugin = types.ModuleType("xbmcplugin")
    requests = types.ModuleType("requests")

    class Session:
        pass

    requests.Session = Session
    requests.RequestException = Exception

    sys.modules.update(
        {
            "requests": requests,
            "xbmc": xbmc,
            "xbmcaddon": xbmcaddon,
            "xbmcgui": xbmcgui,
            "xbmcplugin": xbmcplugin,
        }
    )
    sys.path.insert(0, str(PLUGIN_ROOT))
    original_argv = sys.argv
    sys.argv = ["plugin://plugin.video.comet", "1", ""]
    try:
        return importlib.import_module("lib.router")
    finally:
        sys.argv = original_argv
        sys.path.remove(str(PLUGIN_ROOT))


router = _load_router()
settings_window = importlib.import_module("lib.custom_settings_window")
utils = importlib.import_module("lib.utils")


class KodiRouterTests(unittest.TestCase):
    def test_setup_code_response_requires_current_shape(self):
        valid = {
            "code": "1234abcd",
            "configure_url": "https://comet.test/configure?kodi_code=1234abcd",
            "expires_in": 300,
            "stremio_api_prefix": "api/",
        }
        self.assertEqual(
            settings_window._parse_setup_code_response(valid),
            ("1234abcd", valid["configure_url"], 300, "api"),
        )
        for response in (
            None,
            [],
            {**valid, "code": None},
            {**valid, "code": "1234ABCd"},
            {**valid, "code": "1234abc"},
            {**valid, "configure_url": "javascript:alert(1)"},
            {
                **valid,
                "configure_url": "https://member:secret@comet.test/configure",
            },
            {**valid, "expires_in": True},
            {**valid, "expires_in": 0},
            {**valid, "expires_in": settings_window.MAX_SETUP_POLL_SECONDS + 1},
            {**valid, "stremio_api_prefix": []},
            {**valid, "stremio_api_prefix": "https://attacker.test"},
        ):
            with self.subTest(response=response):
                with self.assertRaisesRegex(ValueError, "Invalid response"):
                    settings_window._parse_setup_code_response(response)

    def test_manifest_response_requires_current_string_fields(self):
        self.assertEqual(
            settings_window._parse_manifest_response(
                {"secret_string": "config", "stremio_api_prefix": "api/"}
            ),
            ("config", "api"),
        )
        for response in (
            None,
            [],
            {},
            {"secret_string": []},
            {"secret_string": "x", "stremio_api_prefix": None},
            {"secret_string": "x", "stremio_api_prefix": "../escape"},
        ):
            with self.subTest(response=response):
                with self.assertRaisesRegex(ValueError, "Invalid response"):
                    settings_window._parse_manifest_response(response)

    def test_current_stream_parser_isolates_malformed_entries(self):
        valid_url = {
            "name": "Direct",
            "description": "Ready",
            "url": "https://example.test/video",
            "behaviorHints": {},
        }
        valid_hash = {
            "name": "Torrent",
            "description": "Ready",
            "infoHash": "a" * 40,
            "sources": ["tracker:https://tracker.test/announce"],
        }

        self.assertEqual(
            router._parse_current_stream(valid_url)["url"], valid_url["url"]
        )
        self.assertEqual(
            router._parse_current_stream(valid_hash)["info_hash"], "a" * 40
        )
        for stream in (
            None,
            [],
            {"name": "Missing description", "url": "https://example.test"},
            {"name": "Bad hints", "description": "x", "url": "x", "behaviorHints": []},
            {"name": "Bad URL", "description": "x", "url": []},
            {"name": "Bad hash", "description": "x", "infoHash": "not-a-hash"},
            {
                "name": "Bad sources",
                "description": "x",
                "infoHash": "b" * 40,
                "sources": [None],
            },
            {
                "name": "Bad scheme",
                "description": "x",
                "url": "file:///tmp/video",
            },
        ):
            with self.subTest(stream=stream):
                self.assertIsNone(router._parse_current_stream(stream))

    def test_fetch_failure_logs_only_category_not_configuration_url(self):
        sensitive = "private-configuration-value"

        class Session:
            def get(self, _url, **_kwargs):
                raise RuntimeError(f"https://comet.test/{sensitive}/stream")

        notifications = []
        logs = []

        class Dialog:
            def notification(self, *args):
                notifications.append(args)

        original_session = utils.HTTP_SESSION
        missing = object()
        original_log = getattr(utils.xbmc, "log", missing)
        original_dialog = getattr(utils.xbmcgui, "Dialog", missing)
        try:
            utils.HTTP_SESSION = Session()
            utils.xbmc.log = lambda message, level: logs.append((message, level))
            utils.xbmcgui.Dialog = Dialog
            self.assertIsNone(
                utils.fetch_data(
                    f"https://comet.test/{sensitive}/stream/movie/tt1234567.json"
                )
            )
        finally:
            utils.HTTP_SESSION = original_session
            if original_log is missing:
                del utils.xbmc.log
            else:
                utils.xbmc.log = original_log
            if original_dialog is missing:
                del utils.xbmcgui.Dialog
            else:
                utils.xbmcgui.Dialog = original_dialog

        rendered = repr((logs, notifications))
        self.assertNotIn(sensitive, rendered)
        self.assertIn("comet.test", rendered)

    def test_browser_open_failure_does_not_log_configuration_url(self):
        sensitive = "private-configuration-value"
        logs = []
        missing = object()
        original_visibility = getattr(
            settings_window.xbmc,
            "getCondVisibility",
            missing,
        )
        original_log = getattr(settings_window.xbmc, "log", missing)
        original_run = settings_window.subprocess.run
        try:
            settings_window.xbmc.getCondVisibility = lambda condition: (
                condition == "system.platform.linux"
            )
            settings_window.xbmc.log = lambda message, level: logs.append(
                (message, level)
            )
            settings_window.subprocess.run = lambda args, **kwargs: (
                _ for _ in ()
            ).throw(RuntimeError(repr((args, kwargs))))
            settings_window.open_configuration_page(
                f"https://comet.test/{sensitive}/configure"
            )
        finally:
            if original_visibility is missing:
                del settings_window.xbmc.getCondVisibility
            else:
                settings_window.xbmc.getCondVisibility = original_visibility
            if original_log is missing:
                del settings_window.xbmc.log
            else:
                settings_window.xbmc.log = original_log
            settings_window.subprocess.run = original_run

        self.assertNotIn(sensitive, repr(logs))
        self.assertIn("RuntimeError", repr(logs))

    def test_catalog_specs_isolate_malformed_current_records(self):
        manifest = {
            "catalogs": [
                None,
                "catalog",
                {"type": "movie"},
                {"type": "movie", "id": 3, "name": "Bad"},
                {"type": "movie", "id": "bad-name", "name": 3},
                {"type": "movie", "id": "popular", "extra": [None, "search"]},
                {
                    "type": "movie",
                    "id": "new",
                    "name": "New",
                    "extra": [None, {"name": "search"}],
                },
                {"type": "series", "id": "series"},
            ]
        }

        self.assertEqual(
            router._catalog_specs(manifest, "movie"),
            [
                {"id": "popular", "name": "popular", "has_search": False},
                {"id": "new", "name": "New", "has_search": True},
            ],
        )

    def test_catalog_specs_reject_invalid_root_shapes(self):
        for manifest in (None, [], {"catalogs": None}, {"catalogs": {}}):
            with self.subTest(manifest=manifest):
                self.assertEqual(router._catalog_specs(manifest, "movie"), [])

    def test_provider_meta_requires_current_object_shape(self):
        original = router.fetch_data
        try:
            for response in (None, [], {"meta": []}, {"meta": None}):
                with self.subTest(response=response):
                    router.fetch_data = lambda url, value=response: value
                    self.assertIsNone(router._fetch_provider_meta("movie", "tt123"))

            expected = {"id": "tt123", "name": "Title"}
            router.fetch_data = lambda url: {"meta": expected}
            self.assertIs(router._fetch_provider_meta("movie", "tt123"), expected)
        finally:
            router.fetch_data = original
