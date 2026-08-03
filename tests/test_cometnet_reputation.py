import unittest

from comet.cometnet.reputation import ReputationStore


def current_reputation():
    return {
        "peers": {
            "peer-b": {
                "reputation": 100.0,
                "first_seen": 1,
                "last_seen": 3,
                "valid_contributions": 2,
                "invalid_contributions": 1,
                "is_blacklisted": False,
            },
            "peer-a": {
                "reputation": 0.0,
                "first_seen": 1,
                "last_seen": 2,
                "valid_contributions": 0,
                "invalid_contributions": 0,
                "is_blacklisted": True,
            },
        },
        "blacklist": ["peer-a"],
    }


class CometNetReputationStoreTests(unittest.TestCase):
    def test_contribution_counts_update_reputation(self):
        peer = ReputationStore().get_or_create("peer")
        peer.add_valid_contribution(2)
        peer.add_invalid_contribution(1)
        self.assertEqual(peer.valid_contributions, 2)
        self.assertEqual(peer.invalid_contributions, 1)

    def test_persisted_schema_is_extensible_atomic_and_deterministic(self):
        store = ReputationStore()
        store.from_dict(current_reputation())

        self.assertEqual(list(store.to_dict()["peers"]), ["peer-a", "peer-b"])
        self.assertEqual(store.to_dict()["blacklist"], ["peer-a"])
        store.from_dict(
            {
                **current_reputation(),
                "extension": True,
                "peers": {
                    node_id: value | {"extension": True}
                    for node_id, value in current_reputation()["peers"].items()
                },
            }
        )
        original = store.to_dict()
        valid = current_reputation()
        malformed = [
            {
                **valid,
                "peers": {
                    "peer-a": valid["peers"]["peer-a"] | {"reputation": float("nan")}
                },
            },
            {
                **valid,
                "peers": {"peer-a": valid["peers"]["peer-a"] | {"last_seen": 0}},
            },
            {
                **valid,
                "peers": {
                    "peer-a": valid["peers"]["peer-a"] | {"valid_contributions": True}
                },
            },
            valid | {"blacklist": []},
        ]
        for payload in malformed:
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    store.from_dict(payload)
                self.assertEqual(store.to_dict(), original)

        store.from_dict(valid | {"blacklist": ["peer-a", "peer-a"]})
        self.assertEqual(store.to_dict()["blacklist"], ["peer-a"])

    def test_node_ids_must_be_non_empty_strings(self):
        store = ReputationStore()
        for node_id in (None, "", 1):
            with self.subTest(node_id=node_id):
                with self.assertRaises(ValueError):
                    store.get_or_create(node_id)
