import base64
import hmac
import uuid

import msgpack
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from comet.core.capability_states import deterministic_cbor
from comet.playback.tokens import (
    PLAYBACK_INTENT_TTL_SECONDS,
    CapabilityCodec,
    CapabilityError,
)

ROOT = base64.urlsafe_b64encode(b"a" * 32).decode().rstrip("=")


def _signed_prepared_asset_token(
    partition: bytes,
    *,
    version: object = 1,
    issued_at: object = 100,
    expires_at: object = 160,
) -> str:
    payload = msgpack.packb(
        [
            version,
            issued_at,
            expires_at,
            "prepared-asset",
            partition,
            uuid.uuid4().bytes,
        ],
        use_bin_type=True,
    )
    capability_key = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"comet-capability-root-v1",
        info=b"comet-playback-capability-v1",
    ).derive(b"a" * 32)
    encoded = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    signature = (
        base64.urlsafe_b64encode(
            hmac.digest(
                capability_key,
                b"comet-cap-v1\0pa2\0" + payload,
                "sha256",
            )
        )
        .decode()
        .rstrip("=")
    )
    return f"pa2.{encoded}.{signature}"


def test_capability_accepts_short_operator_passphrases():
    codec = CapabilityCodec("x")
    partition = codec.configuration_partition(b"normalized")
    token = codec.encode(
        "pa2",
        partition=partition,
        suffix=[uuid.uuid4().bytes],
        ttl=60,
        now=100,
    )

    assert codec.decode(token, partition=partition, now=120)


def test_generated_capability_roots_keep_their_existing_derivation():
    codec = CapabilityCodec(ROOT)
    partition = codec.configuration_partition(b"normalized")
    token = _signed_prepared_asset_token(partition)

    assert codec.decode(token, partition=partition, now=120)


def test_capability_is_bound_to_prefix_partition_and_expiry():
    codec = CapabilityCodec(ROOT)
    partition = codec.configuration_partition(b"normalized")
    suffix = [
        uuid.uuid4().bytes,
        uuid.uuid4().bytes,
        [uuid.uuid4().bytes],
        [0],
        "stremio",
    ]
    token = codec.encode("pi2", partition=partition, suffix=suffix, ttl=60, now=100)

    assert codec.decode(token, partition=partition, now=120)[5:] == suffix
    with pytest.raises(CapabilityError):
        codec.decode(token, partition=b"b" * 32, now=120)
    with pytest.raises(CapabilityError):
        codec.decode(token, partition=None, now=120)
    with pytest.raises(CapabilityError):
        codec.decode(token, partition=partition, now=160)


def test_capability_rejects_invalid_issuance_time():
    codec = CapabilityCodec(ROOT)
    partition = codec.configuration_partition(b"normalized")

    for now in (-1, True, 1.5):
        with pytest.raises(ValueError):
            codec.encode(
                "pa2",
                partition=partition,
                suffix=[uuid.uuid4().bytes],
                ttl=60,
                now=now,
            )


@pytest.mark.parametrize("ttl", [True, 1.5])
def test_capability_rejects_non_integer_ttl(ttl):
    codec = CapabilityCodec(ROOT)
    partition = codec.configuration_partition(b"normalized")

    with pytest.raises(ValueError):
        codec.encode(
            "pa2",
            partition=partition,
            suffix=[uuid.uuid4().bytes],
            ttl=ttl,
        )


@pytest.mark.parametrize("now", [-1, True, 1.5])
def test_decode_rejects_invalid_current_time(now):
    codec = CapabilityCodec(ROOT)
    partition = codec.configuration_partition(b"normalized")
    token = codec.encode(
        "pa2",
        partition=partition,
        suffix=[uuid.uuid4().bytes],
        ttl=60,
        now=100,
    )

    with pytest.raises(CapabilityError):
        codec.decode(token, partition=partition, now=now)


@pytest.mark.parametrize(
    ("issued_at", "expires_at"),
    [
        (-1, 60),
        (True, 60),
        (100, 100),
        (101, 100),
        (100, False),
    ],
)
def test_decode_rejects_malformed_signed_timestamps(issued_at, expires_at):
    codec = CapabilityCodec(ROOT)
    partition = codec.configuration_partition(b"normalized")

    with pytest.raises(CapabilityError):
        codec.decode(
            _signed_prepared_asset_token(
                partition,
                issued_at=issued_at,
                expires_at=expires_at,
            ),
            partition=partition,
            now=50,
        )


def test_decode_rejects_boolean_wire_version():
    codec = CapabilityCodec(ROOT)
    partition = codec.configuration_partition(b"normalized")

    with pytest.raises(CapabilityError):
        codec.decode(
            _signed_prepared_asset_token(partition, version=True),
            partition=partition,
            now=120,
        )


def test_decode_rejects_a_non_ascii_signature_segment_as_a_capability_error():
    """A hostile URL segment must be a typed capability failure, never a bare TypeError."""
    codec = CapabilityCodec(ROOT)
    partition = codec.configuration_partition(b"normalized")
    payload = base64.urlsafe_b64encode(b"\x91\x01").decode().rstrip("=")

    with pytest.raises(CapabilityError):
        codec.decode(f"pa2.{payload}.{'é' * 43}", partition=partition)
    with pytest.raises(CapabilityError):
        codec.decode(f"pa2.{payload}.{'\u00ff' * 43}", partition=partition)


def test_decode_still_accepts_a_genuine_signature_after_the_charset_guard():
    codec = CapabilityCodec(ROOT)
    partition = codec.configuration_partition(b"normalized")
    token = codec.encode(
        "pa2", partition=partition, suffix=[uuid.uuid4().bytes], ttl=60, now=100
    )

    assert codec.decode(token, partition=partition, now=120)[3] == "prepared-asset"


def test_playback_intent_decoding_exposes_typed_identifiers():
    codec = CapabilityCodec(ROOT)
    partition = codec.configuration_partition(b"normalized")
    candidate_id = uuid.uuid4()
    provider_id = uuid.uuid4()
    locator_id = uuid.uuid4()
    token = codec.encode(
        "pi2",
        partition=partition,
        suffix=[
            candidate_id.bytes,
            provider_id.bytes,
            [locator_id.bytes],
            [1, 2, 3],
            "stremio",
        ],
        ttl=60,
        now=100,
    )

    intent = codec.decode_playback_intent(token, partition=partition, now=120)

    assert intent.candidate_id == str(candidate_id)
    assert intent.provider_configuration_id == str(provider_id)
    assert intent.locator_ids == (str(locator_id),)
    assert intent.selection_intent == (1, 2, 3)
    assert intent.client == "stremio"
    with pytest.raises(CapabilityError):
        codec.decode_playback_intent(
            token.replace("pi2.", "na1."), partition=partition, now=120
        )


def test_nzb_handoff_decoder_accepts_only_its_exact_audience():
    codec = CapabilityCodec(ROOT)
    partition = codec.configuration_partition(b"normalized")
    candidate_id, provider_id, locator_id = (uuid.uuid4() for _ in range(3))
    token = codec.encode(
        "ni2",
        partition=partition,
        suffix=[
            candidate_id.bytes,
            provider_id.bytes,
            [locator_id.bytes],
            [0],
            "stremio",
        ],
        ttl=60,
        now=100,
    )

    intent = codec.decode_nzb_handoff_intent(
        token,
        partition=partition,
        now=120,
    )

    assert intent.candidate_id == str(candidate_id)
    assert intent.provider_configuration_id == str(provider_id)
    assert intent.locator_ids == (str(locator_id),)
    with pytest.raises(CapabilityError):
        codec.decode_playback_intent(
            token,
            partition=partition,
            now=120,
        )


def test_prepared_asset_decoding_exposes_only_the_preparation_identifier():
    codec = CapabilityCodec(ROOT)
    partition = codec.configuration_partition(b"normalized")
    preparation_id = uuid.uuid4()
    token = codec.encode(
        "pa2", partition=partition, suffix=[preparation_id.bytes], ttl=60, now=100
    )

    assert codec.decode_prepared_asset(
        token, partition=partition, now=120
    ).preparation_id == str(preparation_id)


def test_capability_rejects_modified_payload():
    codec = CapabilityCodec(ROOT)
    partition = codec.configuration_partition(b"normalized")
    token = codec.encode(
        "na1", partition=partition, suffix=[uuid.uuid4().bytes], ttl=60, now=100
    )
    prefix, payload, signature = token.split(".")
    replacement = "A" if payload[-1] != "A" else "B"

    with pytest.raises(CapabilityError):
        codec.decode(
            f"{prefix}.{payload[:-1]}{replacement}.{signature}",
            partition=partition,
            now=120,
        )


def test_playback_intents_cover_a_session_but_remain_bounded():
    codec = CapabilityCodec(ROOT)
    partition = codec.configuration_partition(b"normalized")
    suffix = [
        uuid.uuid4().bytes,
        uuid.uuid4().bytes,
        [uuid.uuid4().bytes],
        [0],
        "stremio",
    ]

    token = codec.encode(
        "pi2",
        partition=partition,
        suffix=suffix,
        ttl=PLAYBACK_INTENT_TTL_SECONDS,
        now=100,
    )
    assert (
        codec.decode(
            token,
            partition=partition,
            now=100 + 4 * 60 * 60,
        )[5:]
        == suffix
    )

    with pytest.raises(ValueError):
        codec.encode(
            "pi2",
            partition=partition,
            suffix=suffix,
            ttl=PLAYBACK_INTENT_TTL_SECONDS + 1,
        )


def test_capabilities_reject_noncanonical_playback_intent_suffixes():
    codec = CapabilityCodec(ROOT)
    partition = codec.configuration_partition(b"normalized")

    with pytest.raises(ValueError):
        codec.encode(
            "pi2",
            partition=partition,
            suffix=[uuid.uuid4().bytes, uuid.uuid4().bytes, [], [0], "stremio"],
            ttl=60,
        )
    locator_id = uuid.uuid4().bytes
    with pytest.raises(ValueError):
        codec.encode(
            "pi2",
            partition=partition,
            suffix=[
                uuid.uuid4().bytes,
                uuid.uuid4().bytes,
                [locator_id, locator_id],
                [0],
                "stremio",
            ],
            ttl=60,
        )
    with pytest.raises(ValueError):
        codec.encode(
            "ni2",
            partition=partition,
            suffix=[
                uuid.uuid4().bytes,
                uuid.uuid4().bytes,
                [uuid.uuid4().bytes],
                [1, 1, 70000],
                "stremio",
            ],
            ttl=60,
        )


def test_configuration_partition_ignores_runtime_only_normalization_fields():
    codec = CapabilityCodec(ROOT)
    configured = {
        "schemaVersion": 2,
        "playbackProviders": [],
        "_debridEntries": [{"apiKey": "secret"}],
    }
    normalized = {"playbackProviders": [], "schemaVersion": 2, "_enableTorrent": True}

    assert codec.configuration_partition_for_config(
        configured
    ) == codec.configuration_partition_for_config(normalized)


def test_asset_preparation_intent_key_matches_the_normative_hmac_formula():
    codec = CapabilityCodec(ROOT)
    partition = codec.configuration_partition(b"normalized")
    candidate_id, provider_id, first_locator, second_locator = (
        str(uuid.uuid4()) for _ in range(4)
    )
    values = {
        "partition": partition,
        "candidate_id": candidate_id,
        "provider_configuration_id": provider_id,
        "ordered_locator_ids": (first_locator, second_locator),
        "selection_intent": (1, 2, 3),
        "parser_selector_plan_versions": (1, 1, 1, 1),
    }

    actual = codec.asset_preparation_intent_key(**values)
    partition_key = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"comet-capability-root-v1",
        info=b"comet-url-partition-v1",
    ).derive(b"a" * 32)
    payload = deterministic_cbor(
        [
            partition,
            candidate_id,
            provider_id,
            [first_locator, second_locator],
            [1, 2, 3],
            [1, 1, 1, 1],
        ]
    )

    assert (
        actual
        == hmac.digest(
            partition_key,
            b"comet-asset-preparation-v1\0" + payload,
            "sha256",
        ).hex()
    )
    assert actual != codec.asset_preparation_intent_key(
        **{
            **values,
            "ordered_locator_ids": (second_locator, first_locator),
        }
    )
    assert actual != codec.asset_preparation_intent_key(
        **{
            **values,
            "ordered_locator_ids": (
                first_locator,
                first_locator,
                second_locator,
            ),
        }
    )


def test_provider_account_partition_matches_the_normative_hmac_formula():
    codec = CapabilityCodec(ROOT)
    credentials = [["username", "member"], ["password", "secret"]]

    fingerprint, account_partition = codec.provider_account_partition(
        provider_kind="easynews",
        canonical_endpoint="https://members.easynews.com",
        account_identity="member",
        typed_credential_payload=credentials,
        provider_config_version=[2, "easynews-v1"],
    )
    partition_key = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"comet-capability-root-v1",
        info=b"comet-url-partition-v1",
    ).derive(b"a" * 32)
    expected_fingerprint = hmac.digest(
        partition_key,
        b"comet-credential-v1\0" + deterministic_cbor(credentials),
        "sha256",
    )
    expected_partition = hmac.digest(
        partition_key,
        b"comet-account-v1\0"
        + deterministic_cbor(
            [
                "easynews",
                "https://members.easynews.com",
                "member",
                expected_fingerprint,
                [2, "easynews-v1"],
            ]
        ),
        "sha256",
    )

    assert fingerprint == expected_fingerprint.hex()
    assert account_partition == expected_partition
