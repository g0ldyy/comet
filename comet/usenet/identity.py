"""Credential-free identities for verified Usenet posting sets."""

import hashlib
import re

_SHA256_HEX_RE = re.compile(r"\A[0-9a-f]{64}\Z")


def is_sha256_hex(value: object) -> bool:
    """Return True if value is a 64-character lowercase hex string."""
    return isinstance(value, str) and _SHA256_HEX_RE.fullmatch(value) is not None


def partition_hex(partition: bytes) -> str:
    """Return the hex encoding of a 32-byte owner configuration partition."""
    if not isinstance(partition, bytes) or len(partition) != 32:
        raise ValueError("owner configuration partition must contain 32 bytes")
    return partition.hex()


def archive_member_id(set_identity: str, relative_path: str, exact_size: int) -> bytes:
    """Compute the canonical identity of one member inside a verified archive set."""
    encoded = relative_path.encode("utf-8")
    digest = hashlib.sha256()
    digest.update(b"comet-archive-member-v1\0")
    digest.update(set_identity.encode("ascii"))
    digest.update(len(encoded).to_bytes(4, "big"))
    digest.update(encoded)
    digest.update(exact_size.to_bytes(8, "big"))
    return digest.digest()


def archive_member_identity(
    set_identity: str, relative_path: str, exact_size: int
) -> str:
    """Return the hex encoding the engine reports for the same member identity."""
    return archive_member_id(set_identity, relative_path, exact_size).hex()
