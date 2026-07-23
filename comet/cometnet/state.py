import math
from typing import Any


GOSSIP_STAT_KEYS = {
    "torrents_received",
    "torrents_propagated",
    "torrents_repropagated",
    "messages_sent",
    "messages_received",
    "invalid_messages",
    "duplicates_ignored",
    "validation_skipped_exists",
    "torrents_filtered_untrusted",
    "torrents_filtered_blacklisted",
    "torrents_skipped_mode",
}


def _object(value: Any, name: str) -> dict:
    if type(value) is not dict:
        raise ValueError(f"{name} must be an object")
    return value


def _list(value: Any, name: str) -> list:
    if type(value) is not list:
        raise ValueError(f"{name} must be a list")
    return value


def _string(value: Any, name: str, *, allow_empty: bool = False) -> str:
    if type(value) is not str or (not allow_empty and not value):
        raise ValueError(f"{name} must be a string")
    return value


def _number(value: Any, name: str) -> int | float:
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    return value


def _integer(value: Any, name: str) -> int:
    if type(value) is not int:
        raise ValueError(f"{name} must be an integer")
    return value


def _exact_object(value: Any, name: str, fields: set[str]) -> dict:
    obj = _object(value, name)
    if set(obj) != fields:
        raise ValueError(f"{name} does not match the current schema")
    return obj


def validate_state(value: Any) -> dict:
    """Validate the complete schema emitted by the current state writer."""
    state = _object(value, "state")
    required_state_fields = {
        "saved_at",
        "node_id",
        "reputation",
        "keystore",
        "discovery",
        "gossip",
    }
    if set(state) not in (
        required_state_fields,
        required_state_fields | {"integrity_signature"},
    ):
        raise ValueError("state does not match the current schema")
    if _number(state["saved_at"], "saved_at") < 0:
        raise ValueError("saved_at must be non-negative")
    if state["node_id"] is not None:
        _string(state["node_id"], "node_id")
    if "integrity_signature" in state:
        _string(state["integrity_signature"], "integrity_signature")

    reputation = _exact_object(
        state["reputation"], "reputation", {"peers", "blacklist"}
    )
    peers = _object(reputation["peers"], "reputation.peers")
    for node_id, peer_value in peers.items():
        _string(node_id, "reputation peer ID")
        peer = _exact_object(
            peer_value,
            f"reputation.peers.{node_id}",
            {
                "reputation",
                "first_seen",
                "last_seen",
                "valid_contributions",
                "invalid_contributions",
                "is_blacklisted",
            },
        )
        _number(peer["reputation"], f"reputation.peers.{node_id}.reputation")
        _number(peer["first_seen"], f"reputation.peers.{node_id}.first_seen")
        _number(peer["last_seen"], f"reputation.peers.{node_id}.last_seen")
        if (
            _integer(
                peer["valid_contributions"],
                f"reputation.peers.{node_id}.valid_contributions",
            )
            < 0
        ):
            raise ValueError("valid_contributions must be non-negative")
        if (
            _integer(
                peer["invalid_contributions"],
                f"reputation.peers.{node_id}.invalid_contributions",
            )
            < 0
        ):
            raise ValueError("invalid_contributions must be non-negative")
        if type(peer["is_blacklisted"]) is not bool:
            raise ValueError(
                f"reputation.peers.{node_id}.is_blacklisted must be a boolean"
            )
    for node_id in _list(reputation["blacklist"], "reputation.blacklist"):
        _string(node_id, "blacklisted peer ID")

    keystore = _exact_object(state["keystore"], "keystore", {"keys"})
    keys = _object(keystore["keys"], "keystore.keys")
    for node_id, key_value in keys.items():
        _string(node_id, "keystore peer ID")
        key = _exact_object(
            key_value,
            f"keystore.keys.{node_id}",
            {"public_key_hex", "first_seen", "last_seen", "verified"},
        )
        _string(key["public_key_hex"], f"keystore.keys.{node_id}.public_key_hex")
        _number(key["first_seen"], f"keystore.keys.{node_id}.first_seen")
        _number(key["last_seen"], f"keystore.keys.{node_id}.last_seen")
        if type(key["verified"]) is not bool:
            raise ValueError(f"keystore.keys.{node_id}.verified must be a boolean")

    discovery = _exact_object(state["discovery"], "discovery", {"known_peers"})
    for index, peer_value in enumerate(
        _list(discovery["known_peers"], "discovery.known_peers")
    ):
        peer = _exact_object(
            peer_value,
            f"discovery.known_peers[{index}]",
            {"address", "node_id", "source", "last_seen"},
        )
        _string(peer["address"], f"discovery.known_peers[{index}].address")
        _string(
            peer["node_id"],
            f"discovery.known_peers[{index}].node_id",
            allow_empty=True,
        )
        source = _string(peer["source"], f"discovery.known_peers[{index}].source")
        if source not in {"manual", "bootstrap", "pex"}:
            raise ValueError("persisted discovery source is invalid")
        _number(peer["last_seen"], f"discovery.known_peers[{index}].last_seen")

    gossip = _exact_object(state["gossip"], "gossip", {"stats"})
    stats = _object(gossip["stats"], "gossip.stats")
    if stats.keys() != GOSSIP_STAT_KEYS:
        raise ValueError("gossip.stats does not match the current schema")
    for key, stat_value in stats.items():
        if _integer(stat_value, f"gossip.stats.{key}") < 0:
            raise ValueError(f"gossip.stats.{key} must be non-negative")

    return state
