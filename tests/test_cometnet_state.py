import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from comet.cometnet.manager import CometNetService


def current_state():
    return {
        "saved_at": 1.0,
        "node_id": "self",
        "reputation": {
            "peers": {
                "peer": {
                    "reputation": 50.0,
                    "first_seen": 1.0,
                    "last_seen": 2.0,
                    "valid_contributions": 3,
                    "invalid_contributions": 1,
                    "is_blacklisted": False,
                }
            },
            "blacklist": [],
        },
        "keystore": {"keys": {}},
        "discovery": {
            "known_peers": [
                {
                    "address": "wss://peer.example",
                    "node_id": "peer",
                    "source": "pex",
                    "last_seen": 2.0,
                }
            ]
        },
        "gossip": {
            "stats": {
                "torrents_received": 1,
                "torrents_propagated": 2,
                "torrents_repropagated": 3,
                "messages_sent": 4,
                "messages_received": 5,
                "invalid_messages": 6,
                "duplicates_ignored": 7,
                "validation_skipped_exists": 8,
                "torrents_filtered_untrusted": 9,
                "torrents_filtered_blacklisted": 10,
                "torrents_skipped_mode": 11,
            }
        },
    }


class Recorder:
    def __init__(self):
        self.calls = []
        self.max_keys = 10000

    def from_dict(self, data):
        self.calls.append(data)


class AsyncRecorder(Recorder):
    async def from_dict(self, data):
        self.calls.append(data)


class FailingAsyncRecorder(Recorder):
    async def from_dict(self, data):
        raise RuntimeError("address validation failed")


class StateComponent:
    def __init__(self, value):
        self.value = value

    def to_dict(self):
        return self.value


class CometNetStateTests(unittest.IsolatedAsyncioTestCase):
    async def test_state_save_failure_is_visible_to_its_owner(self):
        class Identity:
            node_id = "self"

            async def sign_hex_async(self, data):
                del data
                raise RuntimeError("signing failed")

        with tempfile.TemporaryDirectory() as directory:
            service = CometNetService(keys_dir=directory)
            service.identity = Identity()
            with self.assertRaisesRegex(RuntimeError, "signing failed"):
                await service._save_state()

            self.assertFalse(Path(directory, CometNetService.STATE_FILE).exists())

    async def test_state_is_signed_and_verified_before_restore(self):
        class Identity:
            node_id = "self"
            public_key_hex = "public-key"

            async def sign_hex_async(self, data):
                self.signed_data = data
                return "signature"

        state = current_state()
        state.pop("saved_at")
        state.pop("node_id")
        identity = Identity()

        with tempfile.TemporaryDirectory() as directory:
            writer = CometNetService(keys_dir=directory)
            writer.identity = identity
            writer.reputation = StateComponent(state["reputation"])
            writer.keystore = StateComponent(state["keystore"])
            writer.discovery = StateComponent(state["discovery"])
            writer.gossip = StateComponent(state["gossip"])
            await writer._save_state()

            saved = json.loads(Path(directory, CometNetService.STATE_FILE).read_text())
            self.assertEqual(saved.pop("integrity_signature"), "signature")
            self.assertEqual(
                identity.signed_data,
                json.dumps(saved, sort_keys=True).encode("utf-8"),
            )

            reader = CometNetService(keys_dir=directory)
            reader.identity = identity
            reader.reputation = Recorder()
            reader.keystore = Recorder()
            reader.discovery = AsyncRecorder()
            reader.gossip = Recorder()
            with patch(
                "comet.cometnet.manager.NodeIdentity.verify_hex", return_value=True
            ) as verify:
                await reader._load_state()

            verify.assert_called_once_with(
                identity.signed_data, "signature", identity.public_key_hex
            )
            self.assertEqual(len(reader.reputation.calls), 1)
            self.assertEqual(len(reader.keystore.calls), 1)
            self.assertEqual(len(reader.discovery.calls), 1)
            self.assertEqual(len(reader.gossip.calls), 1)

    async def test_initialized_identity_rejects_unsigned_or_invalid_state(self):
        class Identity:
            public_key_hex = "public-key"

        for signature in (None, "invalid"):
            with (
                self.subTest(signature=signature),
                tempfile.TemporaryDirectory() as directory,
            ):
                state = current_state()
                if signature is not None:
                    state["integrity_signature"] = signature
                Path(directory, CometNetService.STATE_FILE).write_text(
                    json.dumps(state)
                )
                service = CometNetService(keys_dir=directory)
                service.identity = Identity()
                service.reputation = Recorder()
                service.keystore = Recorder()
                service.discovery = AsyncRecorder()
                service.gossip = Recorder()

                with patch(
                    "comet.cometnet.manager.NodeIdentity.verify_hex", return_value=False
                ) as verify:
                    await service._load_state()

                if signature is None:
                    verify.assert_not_called()
                else:
                    verify.assert_called_once()
                self.assertEqual(service.reputation.calls, [])
                self.assertEqual(service.keystore.calls, [])
                self.assertEqual(service.discovery.calls, [])
                self.assertEqual(service.gossip.calls, [])

    async def test_signed_state_must_belong_to_the_current_identity(self):
        class Identity:
            node_id = "current-node"
            public_key_hex = "public-key"

        state = current_state()
        state["node_id"] = "different-node"
        state["integrity_signature"] = "signature"

        with tempfile.TemporaryDirectory() as directory:
            Path(directory, CometNetService.STATE_FILE).write_text(json.dumps(state))
            service = CometNetService(keys_dir=directory)
            service.identity = Identity()
            service.reputation = Recorder()
            service.keystore = Recorder()
            service.discovery = AsyncRecorder()
            service.gossip = Recorder()

            with patch(
                "comet.cometnet.manager.NodeIdentity.verify_hex", return_value=True
            ):
                await service._load_state()

        self.assertEqual(service.reputation.calls, [])
        self.assertEqual(service.keystore.calls, [])
        self.assertEqual(service.discovery.calls, [])
        self.assertEqual(service.gossip.calls, [])

    async def test_invalid_late_section_does_not_partially_restore_state(self):
        state = current_state()
        state["gossip"]["stats"]["messages_sent"] = "4"

        with tempfile.TemporaryDirectory() as directory:
            Path(directory, CometNetService.STATE_FILE).write_text(json.dumps(state))
            service = CometNetService(keys_dir=directory)
            service.reputation = Recorder()
            service.keystore = Recorder()
            service.discovery = AsyncRecorder()
            service.gossip = Recorder()

            await service._load_state()

        self.assertEqual(service.reputation.calls, [])
        self.assertEqual(service.keystore.calls, [])
        self.assertEqual(service.discovery.calls, [])
        self.assertEqual(service.gossip.calls, [])

    async def test_invalid_keystore_identity_is_rejected_before_any_restore(self):
        state = current_state()
        state["keystore"]["keys"]["wrong-node"] = {
            "public_key_hex": "not-der",
            "first_seen": 1,
            "last_seen": 2,
            "verified": True,
        }

        with tempfile.TemporaryDirectory() as directory:
            Path(directory, CometNetService.STATE_FILE).write_text(json.dumps(state))
            service = CometNetService(keys_dir=directory)
            service.reputation = Recorder()
            service.keystore = Recorder()
            service.keystore.max_keys = 100
            service.discovery = AsyncRecorder()
            service.gossip = Recorder()

            await service._load_state()

        self.assertEqual(service.reputation.calls, [])
        self.assertEqual(service.keystore.calls, [])
        self.assertEqual(service.discovery.calls, [])
        self.assertEqual(service.gossip.calls, [])

    async def test_address_validation_failure_does_not_restore_other_sections(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, CometNetService.STATE_FILE).write_text(
                json.dumps(current_state())
            )
            service = CometNetService(keys_dir=directory)
            service.reputation = Recorder()
            service.keystore = Recorder()
            service.discovery = FailingAsyncRecorder()
            service.gossip = Recorder()

            await service._load_state()

        self.assertEqual(service.reputation.calls, [])
        self.assertEqual(service.keystore.calls, [])
        self.assertEqual(service.gossip.calls, [])

    async def test_current_state_restores_every_section(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, CometNetService.STATE_FILE).write_text(
                json.dumps(current_state())
            )
            service = CometNetService(keys_dir=directory)
            service.reputation = Recorder()
            service.keystore = Recorder()
            service.discovery = AsyncRecorder()
            service.gossip = Recorder()

            await service._load_state()

        self.assertEqual(len(service.reputation.calls), 1)
        self.assertEqual(len(service.keystore.calls), 1)
        self.assertEqual(len(service.discovery.calls), 1)
        self.assertEqual(len(service.gossip.calls), 1)
