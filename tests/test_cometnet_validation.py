import unittest
from unittest.mock import AsyncMock, Mock, patch

from comet.cometnet.protocol import PeerRequest
from comet.cometnet.reputation import ReputationStore
from comet.cometnet.validation import validate_message_security


class CometNetValidationTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _keystore(*, verified=True, key=object()):
        return Mock(
            is_verified=Mock(return_value=verified),
            get_key_obj=Mock(return_value=key),
        )

    async def test_current_signed_message_requires_verified_sender_key(self):
        message = PeerRequest(sender_id="peer", signature="00")
        keystore = self._keystore()

        with patch(
            "comet.cometnet.validation.run_in_executor",
            new=AsyncMock(return_value=True),
        ) as verify:
            self.assertTrue(
                await validate_message_security(message, "peer", keystore, None)
            )

        keystore.is_verified.assert_called_once_with("peer")
        keystore.get_key_obj.assert_called_once_with("peer")
        verify.assert_awaited_once()

    async def test_message_security_fails_closed(self):
        cases = [
            (PeerRequest(sender_id="", signature="00"), "peer", self._keystore()),
            (
                PeerRequest(sender_id="different", signature="00"),
                "peer",
                self._keystore(),
            ),
            (PeerRequest(sender_id="peer"), "peer", self._keystore()),
            (PeerRequest(sender_id="peer", signature="00"), "peer", None),
            (
                PeerRequest(sender_id="peer", signature="00"),
                "peer",
                self._keystore(verified=False),
            ),
            (
                PeerRequest(sender_id="peer", signature="00"),
                "peer",
                self._keystore(key=None),
            ),
            (
                PeerRequest(sender_id="peer", signature="not-hex"),
                "peer",
                self._keystore(),
            ),
        ]

        for message, sender_id, keystore in cases:
            with self.subTest(message=message, keystore=keystore):
                self.assertFalse(
                    await validate_message_security(
                        message,
                        sender_id,
                        keystore,
                        ReputationStore(),
                    )
                )

    async def test_invalid_signature_is_rejected(self):
        message = PeerRequest(sender_id="peer", signature="00")
        reputation = ReputationStore()
        initial_reputation = reputation.get_or_create("peer").reputation

        with patch(
            "comet.cometnet.validation.run_in_executor",
            new=AsyncMock(return_value=False),
        ):
            self.assertFalse(
                await validate_message_security(
                    message,
                    "peer",
                    self._keystore(),
                    reputation,
                )
            )

        self.assertLess(
            reputation.get_or_create("peer").reputation,
            initial_reputation,
        )
