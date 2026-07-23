import math
import unittest

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from comet.cometnet.crypto import NodeIdentity
from comet.cometnet.keystore import PublicKeyStore


def key_identity():
    public_key = ec.generate_private_key(ec.SECP256K1()).public_key()
    public_key_hex = public_key.public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).hex()
    return NodeIdentity.node_id_from_public_key(public_key_hex), public_key_hex


class CometNetPublicKeyStoreTests(unittest.TestCase):
    def test_only_handshake_authority_can_promote_a_valid_key(self):
        node_id, public_key_hex = key_identity()
        store = PublicKeyStore()

        store.store_key(node_id, public_key_hex)
        self.assertFalse(store.is_verified(node_id))

        store.store_verified_key(node_id, public_key_hex)
        self.assertTrue(store.is_verified(node_id))

    def test_store_rejects_unbound_or_non_current_inputs(self):
        node_id, public_key_hex = key_identity()
        store = PublicKeyStore()

        malformed = [
            ("wrong-node", public_key_hex),
            (node_id, "not-der"),
        ]
        for arguments in malformed:
            with self.subTest(arguments=arguments):
                with self.assertRaises(ValueError):
                    store.store_key(*arguments)

        self.assertEqual(store.get_stats()["total_keys"], 0)

    def test_persisted_schema_is_strict_atomic_and_lru_ordered(self):
        first_id, first_key = key_identity()
        second_id, second_key = key_identity()
        store = PublicKeyStore()
        valid = {
            "keys": {
                first_id: {
                    "public_key_hex": first_key,
                    "first_seen": 1,
                    "last_seen": 3,
                    "verified": True,
                },
                second_id: {
                    "public_key_hex": second_key,
                    "first_seen": 1,
                    "last_seen": 2,
                    "verified": False,
                },
            }
        }
        store.from_dict(valid)

        self.assertEqual(list(store._keys), [second_id, first_id])
        original = store.to_dict()
        malformed = [
            valid | {"legacy": True},
            {"keys": {first_id: valid["keys"][first_id] | {"legacy": True}}},
            {"keys": {first_id: valid["keys"][first_id] | {"last_seen": float("nan")}}},
            {"keys": {first_id: valid["keys"][first_id] | {"first_seen": 4}}},
        ]
        for payload in malformed:
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    store.from_dict(payload)
                self.assertEqual(store.to_dict(), original)

    def test_constructor_and_cleanup_reject_boolean_nonfinite_or_zero_limits(self):
        for value in (True, 0, -1):
            with self.subTest(max_keys=value):
                with self.assertRaises(ValueError):
                    PublicKeyStore(value)

        store = PublicKeyStore()
        for value in (True, 0, math.inf, math.nan):
            with self.subTest(max_age_days=value):
                with self.assertRaises(ValueError):
                    store.cleanup_old_keys(value)
