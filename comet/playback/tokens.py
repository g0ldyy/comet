"""Versioned, configuration-bound playback capabilities."""

import base64
import hmac
import json
import time
import uuid
from dataclasses import dataclass

import msgpack
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from comet.core.capability_states import deterministic_cbor

MAX_NZB_HANDOFF_LOCATORS = 3
_SALT = b"comet-capability-root-v1"
_DOMAIN = b"comet-cap-v1\0"
_PREFIX_AUDIENCES = {
    "pi2": "playback-intent",
    "pa2": "prepared-asset",
    "na1": "stremio-nzb",
    "ni2": "stremio-nzb-handoff",
}
_PREFIX_MAX_TTL = {
    "pi2": 15 * 60,
    "pa2": 6 * 60 * 60,
    "na1": 6 * 60 * 60,
    "ni2": 6 * 60 * 60,
}
_CLIENT_ENUM = frozenset({"stremio", "kodi", "chilllink"})


class CapabilityError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PlaybackIntent:
    """The typed, owner-bound contents of a ``pi2`` capability."""

    candidate_id: str
    provider_configuration_id: str
    locator_ids: tuple[str, ...]
    selection_intent: tuple[object, ...]
    client: str


@dataclass(frozen=True, slots=True)
class PreparedAsset:
    preparation_id: str


_CONFIG_PARTITION_EXCLUDED_FIELDS = frozenset(
    {
        "_debridEntries",
        "_enableTorrent",
        "rtnSettings",
        "rtnRanking",
    }
)


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _b64decode(value: str) -> bytes:
    if not value or any(
        character
        not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        for character in value
    ):
        raise CapabilityError("invalid capability base64url")
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except ValueError as exc:
        raise CapabilityError("invalid capability base64url") from exc
    if _b64encode(decoded) != value:
        raise CapabilityError("non-canonical capability base64url")
    return decoded


def _is_uuid_bytes(value: object) -> bool:
    return isinstance(value, bytes) and len(value) == 16


def _is_timestamp(value: object) -> bool:
    return type(value) is int and 0 <= value < 2**63


def _is_selection_intent(value: object) -> bool:
    if not isinstance(value, list) or not value or isinstance(value[0], bool):
        return False
    kind = value[0]
    if kind == 0:
        return len(value) == 1
    if kind == 1:
        return len(value) == 3 and all(
            type(number) is int and 0 <= number <= 65535 for number in value[1:]
        )
    return (
        kind == 2
        and len(value) == 2
        and isinstance(value[1], bytes)
        and len(value[1]) == 32
    )


def _valid_suffix(prefix: str, suffix: object) -> bool:
    if not isinstance(suffix, list):
        return False
    if prefix in {"pa2", "na1"}:
        return len(suffix) == 1 and _is_uuid_bytes(suffix[0])
    if prefix == "pi2":
        return (
            len(suffix) == 5
            and _is_uuid_bytes(suffix[0])
            and _is_uuid_bytes(suffix[1])
            and isinstance(suffix[2], list)
            and 1 <= len(suffix[2]) <= 16
            and all(_is_uuid_bytes(locator) for locator in suffix[2])
            and len(set(suffix[2])) == len(suffix[2])
            and _is_selection_intent(suffix[3])
            and suffix[4] in _CLIENT_ENUM
        )
    if prefix == "ni2":
        return (
            len(suffix) == 5
            and _is_uuid_bytes(suffix[0])
            and _is_uuid_bytes(suffix[1])
            and isinstance(suffix[2], list)
            and 1 <= len(suffix[2]) <= MAX_NZB_HANDOFF_LOCATORS
            and all(_is_uuid_bytes(locator) for locator in suffix[2])
            and len(set(suffix[2])) == len(suffix[2])
            and _is_selection_intent(suffix[3])
            and suffix[4] == "stremio"
        )
    return False


class CapabilityCodec:
    def __init__(self, root_secret: str):
        if not isinstance(root_secret, str) or not root_secret:
            raise ValueError("capability root must be a non-empty string")
        try:
            raw_root = root_secret.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError("capability root must be valid UTF-8") from exc
        try:
            decoded_root = _b64decode(root_secret)
        except CapabilityError:
            pass
        else:
            # Keep existing generated roots compatible while allowing passphrases.
            if len(decoded_root) == 32:
                raw_root = decoded_root
        self._partition_key = self._derive(raw_root, b"comet-url-partition-v1")
        self._capability_key = self._derive(raw_root, b"comet-playback-capability-v1")

    @staticmethod
    def _derive(root: bytes, info: bytes) -> bytes:
        return HKDF(algorithm=hashes.SHA256(), length=32, salt=_SALT, info=info).derive(
            root
        )

    def configuration_partition(self, normalized_configuration: bytes) -> bytes:
        return hmac.digest(
            self._partition_key,
            b"comet-url-configuration-v1\0" + normalized_configuration,
            "sha256",
        )

    def configuration_partition_for_config(self, config: dict) -> bytes:
        """Partition the normalized request configuration without serializing runtime helpers."""
        portable = {
            key: value
            for key, value in config.items()
            if key not in _CONFIG_PARTITION_EXCLUDED_FIELDS and not key.startswith("_")
        }
        encoded = json.dumps(
            portable,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        return self.configuration_partition(encoded)

    def provider_credential_fingerprint(
        self,
        provider_kind: str,
        normalized_endpoint: str,
        credential_material: bytes,
    ) -> str:
        """Return a non-reversible, account-scoped binding fingerprint."""
        return hmac.digest(
            self._partition_key,
            b"comet-provider-credential-v1\0"
            + provider_kind.encode("utf-8")
            + b"\0"
            + normalized_endpoint.encode("utf-8")
            + b"\0"
            + credential_material,
            "sha256",
        ).hex()

    def provider_account_partition(
        self,
        *,
        provider_kind: str,
        canonical_endpoint: str,
        account_identity: str,
        typed_credential_payload: object,
        provider_config_version: object,
    ) -> tuple[str, bytes]:
        """Derive the normative credential fingerprint and account partition.

        The returned fingerprint is safe to persist beside provider-owned
        ledgers. The account partition is the only value used to share
        credential-sensitive evidence between equivalent URL configurations.
        """
        credential_fingerprint = hmac.digest(
            self._partition_key,
            b"comet-credential-v1\0" + deterministic_cbor(typed_credential_payload),
            "sha256",
        )
        account_partition = hmac.digest(
            self._partition_key,
            b"comet-account-v1\0"
            + deterministic_cbor(
                [
                    provider_kind,
                    canonical_endpoint,
                    account_identity,
                    credential_fingerprint,
                    provider_config_version,
                ]
            ),
            "sha256",
        )
        return credential_fingerprint.hex(), account_partition

    def capability_binding_fingerprint(
        self,
        *,
        binding_kind: str,
        schema_version: int,
        normalized_endpoint_and_behavior_options: object,
        credential_fingerprint: str,
        instance_capability_version: str,
    ) -> str:
        """Derive a validation-state key without exposing the server HMAC key."""
        from comet.core.capability_states import binding_fingerprint

        return binding_fingerprint(
            self._partition_key,
            binding_kind=binding_kind,
            schema_version=schema_version,
            normalized_endpoint_and_behavior_options=(
                normalized_endpoint_and_behavior_options
            ),
            credential_fingerprint=credential_fingerprint,
            instance_capability_version=instance_capability_version,
        )

    def provider_mutation_key(
        self,
        *,
        partition: bytes,
        provider_configuration_id: str,
        credential_fingerprint: str,
        source_discriminator: str,
        selection_json: str,
        operation: str,
        contract_version: str,
    ) -> str:
        return hmac.digest(
            self._capability_key,
            b"comet-provider-mutation-v1\0"
            + partition
            + b"\0"
            + provider_configuration_id.encode()
            + b"\0"
            + credential_fingerprint.encode()
            + b"\0"
            + source_discriminator.encode()
            + b"\0"
            + selection_json.encode()
            + b"\0"
            + operation.encode()
            + b"\0"
            + contract_version.encode(),
            "sha256",
        ).hex()

    def asset_preparation_intent_key(
        self,
        *,
        partition: bytes,
        candidate_id: str,
        provider_configuration_id: str,
        ordered_locator_ids: tuple[str, ...],
        selection_intent: tuple[object, ...],
        parser_selector_plan_versions: tuple[int, ...],
    ) -> str:
        """Derive the exact database idempotency key for one asset intent."""
        payload = deterministic_cbor(
            [
                partition,
                candidate_id,
                provider_configuration_id,
                list(ordered_locator_ids),
                list(selection_intent),
                list(parser_selector_plan_versions),
            ]
        )
        return hmac.digest(
            self._partition_key,
            b"comet-asset-preparation-v1\0" + payload,
            "sha256",
        ).hex()

    def encode(
        self,
        prefix: str,
        *,
        partition: bytes,
        suffix: list,
        ttl: int,
        now: int | None = None,
    ) -> str:
        audience = _PREFIX_AUDIENCES.get(prefix)
        if (
            audience is None
            or not isinstance(partition, bytes)
            or len(partition) != 32
            or (now is not None and not _is_timestamp(now))
            or type(ttl) is not int
            or not 1 <= ttl <= _PREFIX_MAX_TTL[prefix]
            or not _valid_suffix(prefix, suffix)
        ):
            raise ValueError("invalid capability arguments")
        issued_at = int(time.time()) if now is None else now
        payload = msgpack.packb(
            [1, issued_at, issued_at + ttl, audience, partition, *suffix],
            use_bin_type=True,
        )
        if len(payload) > 2048:
            raise ValueError("capability payload is too large")
        encoded = _b64encode(payload)
        mac = _b64encode(
            hmac.digest(
                self._capability_key,
                _DOMAIN + prefix.encode() + b"\0" + payload,
                "sha256",
            )
        )
        return f"{prefix}.{encoded}.{mac}"

    def decode(self, token: str, *, partition: bytes, now: int | None = None) -> list:
        try:
            prefix, encoded, supplied_mac = token.split(".")
        except ValueError as exc:
            raise CapabilityError("invalid capability format") from exc
        audience = _PREFIX_AUDIENCES.get(prefix)
        if (
            audience is None
            or len(token) > 4096
            or len(supplied_mac) != 43
            or not isinstance(partition, bytes)
            or len(partition) != 32
        ):
            raise CapabilityError("invalid capability")
        payload = _b64decode(encoded)
        expected_mac = _b64encode(
            hmac.digest(
                self._capability_key,
                _DOMAIN + prefix.encode() + b"\0" + payload,
                "sha256",
            )
        )
        if not hmac.compare_digest(
            expected_mac.encode("utf-8"), supplied_mac.encode("utf-8")
        ):
            raise CapabilityError("invalid capability signature")
        try:
            value = msgpack.unpackb(payload, raw=False, strict_map_key=True)
        except ValueError as exc:
            raise CapabilityError("invalid capability payload") from exc
        if not isinstance(value, list) or len(value) < 5 or value[:1] != [1]:
            raise CapabilityError("invalid capability payload")
        version, issued_at, expires_at, token_audience, token_partition = value[:5]
        current_time = int(time.time()) if now is None else now
        if (
            type(version) is not int
            or version != 1
            or not _is_timestamp(current_time)
            or not _is_timestamp(issued_at)
            or not _is_timestamp(expires_at)
            or expires_at <= issued_at
            or issued_at > current_time + 60
            or expires_at <= current_time
            or expires_at - issued_at > _PREFIX_MAX_TTL[prefix]
            or token_audience != audience
            or not isinstance(token_partition, bytes)
            or not hmac.compare_digest(token_partition, partition)
            or not _valid_suffix(prefix, value[5:])
        ):
            raise CapabilityError("expired or mismatched capability")
        return value

    def decode_playback_intent(
        self, token: str, *, partition: bytes, now: int | None = None
    ) -> PlaybackIntent:
        """Decode a ``pi2`` token without exposing its positional wire format."""
        if not token.startswith("pi2."):
            raise CapabilityError("invalid playback intent")
        return self._decode_intent(token, partition=partition, now=now)

    def decode_nzb_handoff_intent(
        self, token: str, *, partition: bytes, now: int | None = None
    ) -> PlaybackIntent:
        """Decode only the client-native ``ni2`` transform audience."""
        if not token.startswith("ni2."):
            raise CapabilityError("invalid NZB handoff intent")
        return self._decode_intent(token, partition=partition, now=now)

    def _decode_intent(
        self,
        token: str,
        *,
        partition: bytes,
        now: int | None,
    ) -> PlaybackIntent:
        value = self.decode(token, partition=partition, now=now)
        candidate_id, provider_id, locator_ids, selection_intent, client = value[5:]
        return PlaybackIntent(
            candidate_id=str(uuid.UUID(bytes=candidate_id)),
            provider_configuration_id=str(uuid.UUID(bytes=provider_id)),
            locator_ids=tuple(
                str(uuid.UUID(bytes=locator_id)) for locator_id in locator_ids
            ),
            selection_intent=tuple(selection_intent),
            client=client,
        )

    def decode_prepared_asset(
        self, token: str, *, partition: bytes, now: int | None = None
    ) -> PreparedAsset:
        """Decode a ``pa2`` reference to one owner-bound preparation."""
        if not token.startswith("pa2."):
            raise CapabilityError("invalid prepared asset")
        value = self.decode(token, partition=partition, now=now)
        return PreparedAsset(str(uuid.UUID(bytes=value[5])))
