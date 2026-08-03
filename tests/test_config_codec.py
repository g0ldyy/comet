import base64
import zlib

import orjson
import pytest

from comet.core.config_codec import (
    CONFIGURATION_DICTIONARY_V1,
    MAX_CONFIG_JSON_BYTES,
    ConfigurationCodecError,
    decode_configuration_segment,
    encode_configuration_segment,
)
from comet.core.config_validation import config_check, configuration_url_segment


def _legacy(document: bytes) -> str:
    return base64.urlsafe_b64encode(document).decode().rstrip("=")


def _raw_deflate(document: bytes) -> bytes:
    compressor = zlib.compressobj(
        level=9,
        wbits=-zlib.MAX_WBITS,
        zdict=CONFIGURATION_DICTIONARY_V1,
    )
    return compressor.compress(document) + compressor.flush()


def _representative_configuration() -> bytes:
    return orjson.dumps(
        {
            "schemaVersion": 2,
            "enabledTransports": ["bittorrent", "usenet"],
            "accounts": {
                "debrid-account": {
                    "kind": "realdebrid",
                    "apiKey": "debrid-secret-credential",
                },
                "indexer-account": {
                    "kind": "indexer",
                    "apiKey": "indexer-secret-credential",
                    "endpoint": "https://indexer.example/api",
                },
            },
            "playbackProviders": [
                {
                    "configurationId": "11111111-1111-4111-8111-111111111111",
                    "displayName": "Living room",
                    "kind": "realdebrid",
                    "enabled": True,
                    "accountId": "debrid-account",
                    "options": {},
                },
                {
                    "configurationId": "22222222-2222-4222-8222-222222222222",
                    "displayName": "Direct torrent",
                    "kind": "direct_torrent",
                    "enabled": True,
                    "options": {},
                },
            ],
            "discoverySources": [
                {
                    "configurationId": "33333333-3333-4333-8333-333333333333",
                    "displayName": "My indexer",
                    "kind": "newznab",
                    "enabled": True,
                    "accountId": "indexer-account",
                    "options": {
                        "endpoint": "https://indexer.example/api",
                        "apiKey": "indexer-secret-credential",
                    },
                }
            ],
            "cachedOnly": False,
            "removeTrash": True,
            "resultFormat": ["title", "video_info", "audio_info", "size"],
            "maxResultsPerResolution": 10,
            "maxSize": 53_687_091_200,
            "languages": {
                "required": ["fr"],
                "allowed": [],
                "exclude": [],
                "preferred": ["en"],
            },
            "resolutions": {"r480p": False, "r360p": False, "r240p": False},
            "options": {
                "allow_english_in_languages": True,
                "remove_unknown_languages": False,
            },
        }
    )


def test_feat_usenet_v2_segment_round_trips_and_materially_reduces_urls():
    document = _representative_configuration()
    legacy = _legacy(document)

    encoded = encode_configuration_segment(document)

    assert encoded.startswith("z1.")
    assert decode_configuration_segment(encoded) == document
    assert len(encoded) < len(legacy) * 0.35
    assert config_check(encoded) is not None


def test_feat_usenet_v2_historical_segment_is_upgraded_for_derived_urls():
    document = _representative_configuration()
    legacy = _legacy(document)

    config = config_check(legacy)
    compact = configuration_url_segment(config, legacy)

    assert compact.startswith("z1.")
    assert len(compact) < len(legacy) * 0.35
    assert decode_configuration_segment(compact) == document


def test_development_v1_historical_segment_remains_supported():
    document = orjson.dumps(
        {
            "debridService": "realdebrid",
            "debridApiKey": "existing-development-install-key",
            "enableTorrent": True,
            "options": {
                "remove_ranks_under": -5_000,
                "allow_english_in_languages": True,
                "remove_unknown_languages": False,
            },
        }
    )
    historical = base64.b64encode(document).decode()

    config = config_check(historical)
    compact = configuration_url_segment(config, historical)

    assert config["schemaVersion"] == 1
    assert config["_debridEntries"] == [
        {
            "service": "realdebrid",
            "apiKey": "existing-development-install-key",
        }
    ]
    assert config_check(compact) == config


def test_tiny_incompressible_document_keeps_the_shorter_legacy_codec():
    assert encode_configuration_segment(b"{}") == "e30"


@pytest.mark.parametrize(
    "payload",
    [
        "z1.not.base64",
        "z1.A",
        "z2.e30",
        "é",
    ],
)
def test_invalid_segments_are_rejected(payload: str):
    with pytest.raises(ConfigurationCodecError):
        decode_configuration_segment(payload)


def test_compressed_trailing_data_is_rejected():
    encoded = "z1." + _legacy(_raw_deflate(b"{}") + b"trailing")

    with pytest.raises(ConfigurationCodecError):
        decode_configuration_segment(encoded)


def test_decompression_output_is_bounded():
    bomb = _raw_deflate(b"x" * (MAX_CONFIG_JSON_BYTES + 1))

    with pytest.raises(ConfigurationCodecError):
        decode_configuration_segment("z1." + _legacy(bomb))
