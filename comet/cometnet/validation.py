import time
from typing import Optional

from comet.cometnet.crypto import NodeIdentity
from comet.cometnet.keystore import PublicKeyStore
from comet.cometnet.protocol import BaseMessage
from comet.cometnet.reputation import ReputationStore
from comet.core.logger import logger
from comet.core.models import settings


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
    # 1. Verify sender_id matches (spoofing check)
    if message.sender_id and message.sender_id != sender_id:
        logger.warning(
            f"Sender ID mismatch: expected {sender_id[:8]}, got {message.sender_id[:8]}"
        )
        if reputation:
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

    # 3. Verify signature if we have the key
    if message.signature and keystore:
        sender_key = keystore.get_key_obj(sender_id)
        if sender_key:
            try:
                signature_bytes = bytes.fromhex(message.signature)
                if not await NodeIdentity.verify_with_key_async(
                    message.to_signable_bytes(),
                    signature_bytes,
                    sender_key,
                ):
                    logger.warning(
                        f"Invalid signature from {sender_id[:8]} on {message.type}"
                    )
                    if reputation:
                        reputation.get_or_create(
                            sender_id
                        ).add_signature_failure_penalty()
                    return False
            except ValueError:
                logger.warning(f"Invalid hex signature from {sender_id[:8]}")
                return False

    return True
