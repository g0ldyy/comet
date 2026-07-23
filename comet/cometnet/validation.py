import time
from typing import Optional

from cryptography.hazmat.primitives.asymmetric.ec import EllipticCurvePublicKey

from comet.cometnet.crypto import NodeIdentity
from comet.cometnet.keystore import PublicKeyStore
from comet.cometnet.protocol import BaseMessage, TorrentMetadata
from comet.cometnet.reputation import ReputationStore
from comet.cometnet.utils import run_in_executor
from comet.core.logger import logger
from comet.core.models import settings


def verify_message_signature_sync(
    message: BaseMessage, signature_bytes: bytes, public_key: EllipticCurvePublicKey
) -> bool:
    """
    Verify message signature synchronously.
    Intended to be run in an executor.
    """
    try:
        data = message.to_signable_bytes()
        return NodeIdentity.verify_with_key(data, signature_bytes, public_key)
    except Exception as e:
        logger.debug(f"Signature verification error: {e}")
        return False


async def validate_message_security(
    message: BaseMessage,
    sender_id: str,
    keystore: Optional[PublicKeyStore],
    reputation: Optional[ReputationStore],
) -> bool:
    """
    Validate message security: sender match, timestamp, and signature.
    Applies reputation penalties on failure.
    Returns True if valid.
    """
    # 1. Verify sender_id matches the authenticated connection.
    if type(sender_id) is not str or not sender_id or message.sender_id != sender_id:
        logger.warning(
            f"Sender ID mismatch: expected {sender_id[:8] if isinstance(sender_id, str) else ''}, "
            f"got {message.sender_id[:8]}"
        )
        if reputation and sender_id:
            reputation.get_or_create(sender_id).add_invalid_contribution()
        return False

    # 2. Verify timestamp (Replay/Drift check)
    now = time.time()
    if message.timestamp > now + settings.COMETNET_GOSSIP_VALIDATION_FUTURE_TOLERANCE:
        logger.debug(f"Rejecting message from {sender_id[:8]}: timestamp in future")
        return False

    if message.timestamp < now - settings.COMETNET_GOSSIP_VALIDATION_PAST_TOLERANCE:
        logger.debug(f"Rejecting message from {sender_id[:8]}: timestamp too old")
        return False

    # 3. Every post-handshake message must have a verified sender key and a
    # valid signature. Handshakes are verified directly by the transport before
    # their key is inserted into this store.
    if not message.signature or not keystore or not keystore.is_verified(sender_id):
        logger.warning(f"Missing verified signature key from {sender_id[:8]}")
        if reputation:
            reputation.get_or_create(sender_id).add_signature_failure_penalty()
        return False

    sender_key = keystore.get_key_obj(sender_id)
    if sender_key is None:
        logger.warning(f"Missing public key from {sender_id[:8]}")
        if reputation:
            reputation.get_or_create(sender_id).add_signature_failure_penalty()
        return False

    try:
        signature_bytes = bytes.fromhex(message.signature)
    except ValueError:
        logger.warning(f"Invalid hex signature from {sender_id[:8]}")
        if reputation:
            reputation.get_or_create(sender_id).add_signature_failure_penalty()
        return False

    is_valid = await run_in_executor(
        verify_message_signature_sync, message, signature_bytes, sender_key
    )
    if not is_valid:
        logger.warning(f"Invalid signature from {sender_id[:8]} on {message.type}")
        if reputation:
            reputation.get_or_create(sender_id).add_signature_failure_penalty()
        return False
    return True


def verify_torrent_signature_sync(torrent: TorrentMetadata) -> bool:
    """
    Verify a torrent signature synchronously.
    Intended to be run in an executor to offload CPU work.
    """
    try:
        if (
            not torrent.contributor_id
            or not torrent.contributor_signature
            or not torrent.contributor_public_key
        ):
            return False

        # Verify that public key matches contributor_id
        derived_id = NodeIdentity.node_id_from_public_key(
            torrent.contributor_public_key
        )
        if derived_id != torrent.contributor_id:
            return False

        data_to_sign = torrent.to_signable_bytes()

        return NodeIdentity.verify_hex(
            data_to_sign,
            torrent.contributor_signature,
            torrent.contributor_public_key,
        )
    except Exception:
        return False
