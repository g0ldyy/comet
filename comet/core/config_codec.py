"""Bounded, versioned codecs for self-contained URL configurations."""

import base64
import re
import zlib


MAX_CONFIG_SEGMENT_BYTES = 32 * 1024
MAX_CONFIG_JSON_BYTES = 24 * 1024
# This is part of the z1 wire format. Never mutate it; a new dictionary needs a
# new segment prefix so installed URLs remain decodable.
CONFIGURATION_DICTIONARY_V1 = (
    b'"nativeAccessToken":"debridStreamProxyPassword":"scrapeDebridAccountTorrents":'
    b'"maxResultsPerResolution":"remove_unknown_languages":'
    b'"allow_english_in_languages":"discoverySources":"playbackProviders":'
    b'"enabledTransports":"configurationId":"displayName":"accountId":'
    b'"schemaVersion":"accounts":"cachedOnly":"removeTrash":"resultFormat":'
    b'"maxSize":"languages":"resolutions":"options":"enabled":"kind":'
    b'"apiKey":"endpoint":"required":"allowed":"exclude":"preferred":'
    b'"bittorrent":"usenet":"realdebrid":"direct_torrent":"newznab":'
    b'"comet_native_usenet":"personal_servers":"instance_pool":"source":'
    b'"servers":"username":"password":"host":"port":"tls":'
)

_COMPRESSED_PREFIX = "z1."
_LEGACY_BASE64 = re.compile(r"^(?:[A-Za-z0-9+/]+={0,2}|[A-Za-z0-9_-]+={0,2})$")


class ConfigurationCodecError(ValueError):
    """The URL segment is malformed, oversized, or uses an unknown codec."""


def _decode_base64(value: str) -> bytes:
    if len(value) % 4 == 1 or _LEGACY_BASE64.fullmatch(value) is None:
        raise ConfigurationCodecError("invalid configuration base64")
    try:
        return base64.b64decode(
            value + "=" * (-len(value) % 4),
            altchars=b"-_",
            validate=True,
        )
    except ValueError as exc:
        raise ConfigurationCodecError("invalid configuration base64") from exc


def _decompress_bounded(value: bytes) -> bytes:
    inflater = zlib.decompressobj(
        wbits=-zlib.MAX_WBITS,
        zdict=CONFIGURATION_DICTIONARY_V1,
    )
    try:
        document = inflater.decompress(value, MAX_CONFIG_JSON_BYTES + 1)
    except zlib.error as exc:
        raise ConfigurationCodecError("invalid compressed configuration") from exc
    if (
        len(document) > MAX_CONFIG_JSON_BYTES
        or not inflater.eof
        or inflater.unconsumed_tail
        or inflater.unused_data
    ):
        raise ConfigurationCodecError("invalid compressed configuration")
    return document


def decode_configuration_segment(segment: str) -> bytes:
    """Decode current compressed and historical plain-base64 configurations."""
    if not segment or len(segment) > MAX_CONFIG_SEGMENT_BYTES:
        raise ConfigurationCodecError("oversized configuration")

    if segment.startswith(_COMPRESSED_PREFIX):
        document = _decompress_bounded(
            _decode_base64(segment.removeprefix(_COMPRESSED_PREFIX))
        )
    else:
        document = _decode_base64(segment)

    if len(document) > MAX_CONFIG_JSON_BYTES:
        raise ConfigurationCodecError("oversized configuration")
    return document


def encode_configuration_segment(document: bytes) -> str:
    """Return the shorter supported representation of a JSON document."""
    if not document or len(document) > MAX_CONFIG_JSON_BYTES:
        raise ConfigurationCodecError("oversized configuration")
    legacy = base64.urlsafe_b64encode(document).decode("ascii").rstrip("=")
    compressor = zlib.compressobj(
        level=9,
        wbits=-zlib.MAX_WBITS,
        zdict=CONFIGURATION_DICTIONARY_V1,
    )
    compressed = compressor.compress(document) + compressor.flush()
    current = _COMPRESSED_PREFIX + base64.urlsafe_b64encode(compressed).decode(
        "ascii"
    ).rstrip("=")
    segment = current if len(current) < len(legacy) else legacy
    if len(segment) > MAX_CONFIG_SEGMENT_BYTES:
        raise ConfigurationCodecError("oversized configuration")
    return segment
