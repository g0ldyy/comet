import importlib.util
import unittest
from pathlib import Path


PARSER_PATH = (
    Path(__file__).parents[1] / "kodi" / "plugin.video.comet" / "lib" / "parser.py"
)
SPEC = importlib.util.spec_from_file_location("comet_kodi_parser", PARSER_PATH)
parser = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(parser)


class KodiParserTests(unittest.TestCase):
    def test_malformed_hints_degrade_to_current_empty_metadata(self):
        for hints in (None, [], "metadata"):
            with self.subTest(hints=hints):
                result = parser.parse_stream_info("name", "description", hints)
                self.assertEqual(result["size"], 0)
                self.assertEqual(result["languages"], [])

    def test_malformed_kodi_fields_are_isolated(self):
        result = parser.parse_stream_info(
            "name",
            "description",
            {
                "videoSize": True,
                parser.KODI_META_KEY: {
                    "width": True,
                    "height": -1080,
                    "codec": ["hevc"],
                    "qualityInfo": "WEB-DL",
                    "languages": ["en", None, 3, "fr"],
                },
            },
        )

        self.assertEqual(result["size"], 0)
        self.assertEqual(result["width"], 0)
        self.assertEqual(result["height"], 0)
        self.assertEqual(result["codec"], "")
        self.assertEqual(result["qualityInfo"], "WEB-DL")
        self.assertEqual(result["languages"], ["en", "fr"])

    def test_valid_current_metadata_is_preserved(self):
        result = parser.parse_stream_info(
            "name",
            "description",
            {
                "videoSize": 1234,
                parser.KODI_META_KEY: {
                    "width": "1920",
                    "height": 1080,
                    "codec": "h264",
                    "languages": ["en"],
                },
            },
        )

        self.assertEqual(result["size"], 1234)
        self.assertEqual(result["width"], 1920)
        self.assertEqual(result["height"], 1080)
        self.assertEqual(result["codec"], "h264")
        self.assertEqual(result["languages"], ["en"])
