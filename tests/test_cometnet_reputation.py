import math
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
    def test_contribution_counts_require_positive_exact_integers(self):
        peer = ReputationStore().get_or_create("peer")
        for count in (True, 0, -1, 1.5):
            with self.subTest(count=count):
                with self.assertRaises(ValueError):
                    peer.add_valid_contribution(count)
                with self.assertRaises(ValueError):
                    peer.add_invalid_contribution(count)

        peer.add_valid_contribution(2)
        peer.add_invalid_contribution(1)
        self.assertEqual(peer.valid_contributions, 2)
        self.assertEqual(peer.invalid_contributions, 1)

    def test_persisted_schema_is_strict_atomic_and_deterministic(self):
        store = ReputationStore()
        store.from_dict(current_reputation())

        self.assertEqual(list(store.to_dict()["peers"]), ["peer-a", "peer-b"])
        self.assertEqual(store.to_dict()["blacklist"], ["peer-a"])
        original = store.to_dict()
        valid = current_reputation()
        malformed = [
            valid | {"legacy": True},
            valid | {"blacklist": ["peer-a", "peer-a"]},
            {
                **valid,
                "peers": {"peer-a": valid["peers"]["peer-a"] | {"legacy": True}},
            },
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

    def test_node_and_cleanup_inputs_are_current_and_finite(self):
        store = ReputationStore()
        for node_id in (None, "", 1):
            with self.subTest(node_id=node_id):
                with self.assertRaises(ValueError):
                    store.get_or_create(node_id)

        for max_age_days in (True, 0, -1, math.inf, math.nan):
            with self.subTest(max_age_days=max_age_days):
                with self.assertRaises(ValueError):
                    store.cleanup_old_peers(max_age_days)
