"""Canonical credential-free serialization for release locators."""

import re
import uuid
from collections.abc import Mapping

import orjson

from comet.core.sources import (
    LOCATOR_PROVIDER_KINDS,
    MAX_REMOTE_GUID_LENGTH,
    MAX_SIGNED_BIGINT,
    EasynewsHttpRef,
    Locator,
    LocatorKind,
    LocatorPolicy,
    NzbArtifactRef,
    RealNzbRef,
    TorrentLocator,
)

_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f]")
_PROVIDER_KIND = re.compile(r"[0-9A-Za-z_-]+")


def locator_json(locator: Locator) -> str:
    payload: dict[str, object]
    expected_kind: LocatorKind
    if isinstance(locator, TorrentLocator):
        expected_kind = LocatorKind.TORRENT
        payload = {"info_hash": _lower_hex(locator.info_hash, "info_hash", 40)}
        selection_values = (
            locator.file_index,
            locator.selection_title,
            locator.selection_size,
            locator.selection_parsed_json,
        )
        has_selection = (
            any(value is not None for value in selection_values)
            or locator.season_norm != -1
            or locator.episode_norm != -1
        )
        if has_selection:
            if locator.selection_title is None or locator.selection_parsed_json is None:
                raise ValueError("torrent selection fields are incomplete")
            if locator.file_index is not None and (
                isinstance(locator.file_index, bool)
                or not isinstance(locator.file_index, int)
                or locator.file_index < 0
            ):
                raise ValueError("torrent file index is invalid")
            if (
                isinstance(locator.season_norm, bool)
                or not isinstance(locator.season_norm, int)
                or locator.season_norm < -1
                or isinstance(locator.episode_norm, bool)
                or not isinstance(locator.episode_norm, int)
                or locator.episode_norm < -1
            ):
                raise ValueError("torrent selection scope is invalid")
            parsed_payload = _text(
                locator.selection_parsed_json,
                "selection_parsed_json",
                32_768,
            )
            try:
                parsed_value = orjson.loads(parsed_payload)
            except (orjson.JSONDecodeError, TypeError) as exc:
                raise ValueError("torrent selection metadata is invalid") from exc
            if not isinstance(parsed_value, dict):
                raise ValueError("torrent selection metadata is invalid")
            payload.update(
                {
                    "file_index": locator.file_index,
                    "season_norm": locator.season_norm,
                    "episode_norm": locator.episode_norm,
                    "selection_title": _text(
                        locator.selection_title,
                        "selection_title",
                        1_024,
                    ),
                    "selection_size": _optional_positive_size(
                        locator.selection_size,
                        "selection_size",
                    ),
                    "selection_parsed_json": parsed_payload,
                }
            )
    elif isinstance(locator, RealNzbRef):
        expected_kind = LocatorKind.REAL_NZB
        payload = {
            "adapter_configuration_id": _configuration_id(
                locator.adapter_configuration_id,
                "adapter_configuration_id",
            ),
            "remote_guid": _text(
                locator.remote_guid,
                "remote_guid",
                MAX_REMOTE_GUID_LENGTH,
            ),
        }
    elif isinstance(locator, NzbArtifactRef):
        expected_kind = LocatorKind.NZB_ARTIFACT
        payload = {
            "artifact_sha256": _lower_hex(
                locator.artifact_sha256,
                "artifact_sha256",
                64,
            ),
            "manifest_identity": _identity(
                locator.manifest_identity,
                "manifest_identity",
                "nm1",
                64,
            ),
        }
        if (
            locator.selection_hint_name is not None
            or locator.selection_hint_size is not None
        ):
            payload.update(
                {
                    "selection_hint_name": _optional_text(
                        locator.selection_hint_name,
                        "selection_hint_name",
                        512,
                    ),
                    "selection_hint_size": _optional_positive_size(
                        locator.selection_hint_size,
                        "selection_hint_size",
                    ),
                }
            )
            if None in (
                payload["selection_hint_name"],
                payload["selection_hint_size"],
            ):
                raise ValueError("incomplete NZB artifact selection hint")
    elif isinstance(locator, EasynewsHttpRef):
        expected_kind = LocatorKind.EASYNEWS_HTTP
        payload = {
            "account_configuration_id": _configuration_id(
                locator.account_configuration_id,
                "account_configuration_id",
            ),
            "file_identifier": _text(
                locator.file_identifier,
                "file_identifier",
            ),
            "dlFarm": _text(locator.download_farm, "download_farm", 128),
            "dlPort": _text(locator.download_port, "download_port", 128),
            "hash": _text(locator.content_hash, "content_hash", 256),
            "id": _empty_or_text(
                locator.item_identifier,
                "item_identifier",
                256,
            ),
            "filename": _text(locator.filename, "filename", 512),
            "extension": _text(locator.extension, "extension", 32),
            "signature": _optional_text(
                locator.signature,
                "signature",
                512,
            ),
            "byte_size": _optional_positive_size(
                locator.byte_size,
                "byte_size",
            ),
        }
    else:
        raise ValueError("unsupported locator type")
    if locator.kind != expected_kind:
        raise ValueError("locator kind does not match locator type")
    return _canonical_json(payload, 65_536, "locator")


def policy_json(locator: Locator) -> str:
    policy = locator.policy
    if not isinstance(policy, LocatorPolicy) or not isinstance(
        locator.kind, LocatorKind
    ):
        raise ValueError("invalid locator policy")
    if not isinstance(policy.allowed_provider_kinds, frozenset):
        raise ValueError("invalid locator provider policy")
    provider_kinds = policy.allowed_provider_kinds
    if (
        not provider_kinds
        or len(provider_kinds) > 32
        or not provider_kinds <= LOCATOR_PROVIDER_KINDS[locator.kind]
        or any(
            not isinstance(kind, str)
            or not 1 <= len(kind) <= 64
            or not _PROVIDER_KIND.fullmatch(kind)
            for kind in provider_kinds
        )
    ):
        raise ValueError("invalid locator provider policy")
    provider_kinds = sorted(provider_kinds)
    exact_configuration_id = policy.exact_provider_configuration_id
    if exact_configuration_id is not None:
        exact_configuration_id = _configuration_id(
            exact_configuration_id,
            "exact_provider_configuration_id",
        )
    owner_partition = policy.owner_configuration_partition
    if owner_partition is not None and (
        not isinstance(owner_partition, bytes) or len(owner_partition) != 32
    ):
        raise ValueError("invalid locator owner partition")
    expires_at = policy.expires_at
    if expires_at is not None and (
        isinstance(expires_at, bool)
        or not isinstance(expires_at, int)
        or not 0 <= expires_at <= (2**63 - 1) // 1_000
    ):
        raise ValueError("invalid locator expiry")
    return _canonical_json(
        {
            "allowed_provider_kinds": provider_kinds,
            "exact_provider_configuration_id": exact_configuration_id,
            "expires_at": expires_at,
            "owner_configuration_partition": (
                owner_partition.hex() if owner_partition is not None else None
            ),
        },
        16_384,
        "locator policy",
    )


def parsed_json(parsed: object | None) -> str:
    if parsed is None:
        return "{}"
    model_dump = getattr(parsed, "model_dump", None)
    if not callable(model_dump):
        raise ValueError("invalid parsed release")
    return _canonical_json(model_dump(), 65_536, "parsed release")


def locator_from_json(
    locator_id: str,
    kind: str,
    locator_payload: str,
    policy_payload: str,
) -> Locator:
    try:
        locator_kind = LocatorKind(kind)
        payload = orjson.loads(locator_payload)
        raw_policy = orjson.loads(policy_payload)
    except (orjson.JSONDecodeError, TypeError, ValueError) as exc:
        raise ValueError("invalid persisted locator") from exc
    if not isinstance(payload, dict) or not isinstance(raw_policy, dict):
        raise ValueError("invalid persisted locator")
    policy = _policy_from_mapping(raw_policy, locator_kind)
    common = {
        "locator_id": _text(locator_id, "locator_id", 128),
        "kind": locator_kind,
        "policy": policy,
    }
    if locator_kind is LocatorKind.TORRENT:
        base_fields = {"info_hash"}
        selection_fields = {
            "file_index",
            "season_norm",
            "episode_norm",
            "selection_title",
            "selection_size",
            "selection_parsed_json",
        }
        if payload.keys() not in (base_fields, base_fields | selection_fields):
            raise ValueError("invalid persisted locator fields")
        locator = TorrentLocator(
            **common,
            info_hash=_lower_hex(payload["info_hash"], "info_hash", 40),
            file_index=_optional_nonnegative_integer(
                payload.get("file_index"),
                "file_index",
            ),
            season_norm=_scope_integer(payload.get("season_norm", -1)),
            episode_norm=_scope_integer(payload.get("episode_norm", -1)),
            selection_title=_optional_text(
                payload.get("selection_title"),
                "selection_title",
                1_024,
            ),
            selection_size=_optional_positive_size(
                payload.get("selection_size"),
                "selection_size",
            ),
            selection_parsed_json=_optional_json_object_text(
                payload.get("selection_parsed_json"),
                "selection_parsed_json",
                32_768,
            ),
        )
        if selection_fields <= payload.keys() and (
            locator.selection_title is None or locator.selection_parsed_json is None
        ):
            raise ValueError("invalid persisted locator fields")
    elif locator_kind is LocatorKind.REAL_NZB:
        if payload.keys() != {"adapter_configuration_id", "remote_guid"}:
            raise ValueError("invalid persisted locator fields")
        locator = RealNzbRef(
            **common,
            adapter_configuration_id=_configuration_id(
                payload["adapter_configuration_id"],
                "adapter_configuration_id",
            ),
            remote_guid=_text(
                payload["remote_guid"],
                "remote_guid",
                MAX_REMOTE_GUID_LENGTH,
            ),
        )
    elif locator_kind is LocatorKind.NZB_ARTIFACT:
        base_fields = {"artifact_sha256", "manifest_identity"}
        hint_fields = {"selection_hint_name", "selection_hint_size"}
        if payload.keys() not in (base_fields, base_fields | hint_fields):
            raise ValueError("invalid persisted locator fields")
        has_selection_hint = bool(hint_fields & payload.keys())
        locator = NzbArtifactRef(
            **common,
            artifact_sha256=_lower_hex(
                payload["artifact_sha256"],
                "artifact_sha256",
                64,
            ),
            manifest_identity=_identity(
                payload["manifest_identity"],
                "manifest_identity",
                "nm1",
                64,
            ),
            selection_hint_name=_optional_text(
                payload.get("selection_hint_name"),
                "selection_hint_name",
                512,
            ),
            selection_hint_size=_optional_positive_size(
                payload.get("selection_hint_size"),
                "selection_hint_size",
            ),
        )
        if has_selection_hint and (
            locator.selection_hint_name is None or locator.selection_hint_size is None
        ):
            raise ValueError("invalid persisted locator fields")
    else:
        easynews_fields = {
            "account_configuration_id",
            "file_identifier",
            "dlFarm",
            "dlPort",
            "hash",
            "id",
            "filename",
            "extension",
            "signature",
            "byte_size",
        }
        if payload.keys() != easynews_fields:
            raise ValueError("invalid persisted locator fields")
        locator = EasynewsHttpRef(
            **common,
            account_configuration_id=_configuration_id(
                payload["account_configuration_id"],
                "account_configuration_id",
            ),
            file_identifier=_text(
                payload["file_identifier"],
                "file_identifier",
            ),
            download_farm=_text(payload["dlFarm"], "download_farm", 128),
            download_port=_text(payload["dlPort"], "download_port", 128),
            content_hash=_text(payload["hash"], "content_hash", 256),
            item_identifier=_empty_or_text(
                payload["id"],
                "item_identifier",
                256,
            ),
            filename=_text(payload["filename"], "filename", 512),
            extension=_text(payload["extension"], "extension", 32),
            signature=_optional_text(
                payload.get("signature"),
                "signature",
                512,
            ),
            byte_size=_optional_positive_size(
                payload.get("byte_size"),
                "byte_size",
            ),
        )
    # Re-serialization applies the same limits and canonical form to decoded data.
    locator_json(locator)
    policy_json(locator)
    return locator


def _policy_from_mapping(
    payload: Mapping[object, object],
    locator_kind: LocatorKind,
) -> LocatorPolicy:
    if payload.keys() != {
        "allowed_provider_kinds",
        "exact_provider_configuration_id",
        "expires_at",
        "owner_configuration_partition",
    }:
        raise ValueError("invalid persisted locator fields")
    raw_kinds = payload["allowed_provider_kinds"]
    if (
        not isinstance(raw_kinds, list)
        or any(not isinstance(kind, str) for kind in raw_kinds)
        or raw_kinds != sorted(set(raw_kinds))
    ):
        raise ValueError("invalid persisted locator policy")
    owner_partition = payload["owner_configuration_partition"]
    if owner_partition is not None:
        if (
            not isinstance(owner_partition, str)
            or len(owner_partition) != 64
            or owner_partition.lower() != owner_partition
        ):
            raise ValueError("invalid persisted locator policy")
        try:
            owner_partition = bytes.fromhex(owner_partition)
        except ValueError as exc:
            raise ValueError("invalid persisted locator policy") from exc
    try:
        policy = LocatorPolicy(
            frozenset(raw_kinds),
            owner_partition,
            payload["exact_provider_configuration_id"],
            payload["expires_at"],
        )
    except TypeError as exc:
        raise ValueError("invalid persisted locator policy") from exc
    # Validate through the one canonical policy encoder.
    policy_json(
        Locator(
            locator_id="validation",
            kind=locator_kind,
            policy=policy,
        )
    )
    return policy


def _canonical_json(value: object, maximum: int, field: str) -> str:
    try:
        payload = orjson.dumps(value, option=orjson.OPT_SORT_KEYS).decode()
    except (orjson.JSONEncodeError, TypeError) as exc:
        raise ValueError(f"invalid {field} JSON") from exc
    if len(payload.encode()) > maximum:
        raise ValueError(f"{field} JSON is too large")
    return payload


def _text(value: object, field: str, maximum: int = 1024) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or _CONTROL_CHARACTERS.search(value)
    ):
        raise ValueError(f"invalid locator {field}")
    return value


def _optional_text(
    value: object,
    field: str,
    maximum: int = 1024,
) -> str | None:
    if value is None:
        return None
    return _text(value, field, maximum)


def _empty_or_text(value: object, field: str, maximum: int) -> str:
    if value == "":
        return ""
    return _text(value, field, maximum)


def _lower_hex(value: object, field: str, length: int) -> str:
    text = _text(value, field, length)
    if len(text) != length or any(
        character not in "0123456789abcdef" for character in text
    ):
        raise ValueError(f"invalid locator {field}")
    return text


def _identity(
    value: object,
    field: str,
    scheme: str,
    digest_length: int,
) -> str:
    text = _text(value, field, len(scheme) + 1 + digest_length)
    prefix = f"{scheme}:"
    if not text.startswith(prefix):
        raise ValueError(f"invalid locator {field}")
    _lower_hex(text[len(prefix) :], field, digest_length)
    return text


def _configuration_id(value: object, field: str) -> str:
    text = _text(value, field, 36)
    try:
        canonical = str(uuid.UUID(text))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(f"invalid locator {field}") from exc
    if canonical != text:
        raise ValueError(f"invalid locator {field}")
    return text


def _optional_positive_size(value: object, field: str) -> int | None:
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= MAX_SIGNED_BIGINT
    ):
        raise ValueError(f"invalid locator {field}")
    return value


def _optional_nonnegative_integer(value: object, field: str) -> int | None:
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= 2**31 - 1
    ):
        raise ValueError(f"invalid locator {field}")
    return value


def _scope_integer(value: object) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not -1 <= value <= 100_000
    ):
        raise ValueError("invalid torrent selection scope")
    return value


def _optional_json_object_text(
    value: object,
    field: str,
    maximum: int,
) -> str | None:
    if value is None:
        return None
    payload = _text(value, field, maximum)
    try:
        decoded = orjson.loads(payload)
    except (orjson.JSONDecodeError, TypeError) as exc:
        raise ValueError(f"invalid locator {field}") from exc
    if not isinstance(decoded, dict):
        raise ValueError(f"invalid locator {field}")
    return payload
