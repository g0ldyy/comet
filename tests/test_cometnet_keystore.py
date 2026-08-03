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

    def test_persisted_schema_is_extensible_atomic_and_lru_ordered(self):
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
        store.from_dict(
            {
                **valid,
                "extension": True,
                "keys": {
                    node_id: value | {"extension": True}
                    for node_id, value in valid["keys"].items()
                },
            }
        )
        self.assertEqual(list(store._keys), [second_id, first_id])
        original = store.to_dict()
        malformed = [
            {"keys": {first_id: valid["keys"][first_id] | {"last_seen": float("nan")}}},
            {"keys": {first_id: valid["keys"][first_id] | {"first_seen": 4}}},
        ]
        for payload in malformed:
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    store.from_dict(payload)
                self.assertEqual(store.to_dict(), original)
