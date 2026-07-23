import math
import unittest

import msgpack

from comet.cometnet.protocol import (
    PROTOCOL_VERSION,
    HandshakeMessage,
    MessageType,
    PeerRequest,
    PoolMemberUpdate,
    PoolManifestMessage,
    TorrentAnnounce,
    TorrentQuery,
    parse_message,
)


class CometNetProtocolTests(unittest.TestCase):
    @staticmethod
    def _pack(payload):
        return msgpack.packb(payload)

    def test_pool_manifest_keeps_protocol_and_manifest_versions_distinct(self):
        message = PoolManifestMessage(
            pool_id="pool-a",
            display_name="Pool A",
            creator_key="creator",
            manifest_version=7,
        )

        parsed = parse_message(message.to_bytes())

        self.assertEqual(parsed.version, PROTOCOL_VERSION)
        self.assertEqual(parsed.manifest_version, 7)

    def test_parser_rejects_unknown_fields_and_non_mapping_payloads(self):
        self.assertIsNone(
            parse_message(
                self._pack(
                    {
                        "version": PROTOCOL_VERSION,
                        "type": MessageType.PING.value,
                        "unknown": True,
                    }
                )
            )
        )
        self.assertIsNone(parse_message(self._pack(["not", "a", "message"])))

    def test_parser_rejects_coerced_or_out_of_range_control_fields(self):
        invalid_messages = [
            PeerRequest(max_peers=1).model_dump() | {"max_peers": True},
            PeerRequest(max_peers=1).model_dump() | {"max_peers": "20"},
            TorrentAnnounce().model_dump() | {"ttl": 0},
            TorrentAnnounce().model_dump() | {"ttl": 33},
            TorrentQuery().model_dump() | {"limit": True},
            HandshakeMessage().model_dump() | {"listen_port": 65536},
            PoolManifestMessage(
                pool_id="pool-a", display_name="Pool A", creator_key="creator"
            ).model_dump()
            | {"manifest_version": True},
            PoolManifestMessage(
                pool_id="pool-a", display_name="Pool A", creator_key="creator"
            ).model_dump()
            | {"updated_at": math.nan},
        ]

        for payload in invalid_messages:
            with self.subTest(payload=payload):
                self.assertIsNone(parse_message(self._pack(payload)))

    def test_parser_rejects_wrong_protocol_version(self):
        payload = PeerRequest().model_dump()
        payload["version"] = "2.0"

        self.assertIsNone(parse_message(self._pack(payload)))

    def test_pool_messages_require_current_content_schema(self):
        manifest = PoolManifestMessage(
            pool_id="pool-a",
            display_name="Pool A",
            creator_key="creator",
        ).model_dump()
        update = PoolMemberUpdate(
            pool_id="pool-a",
            action="add",
            member_key="member",
            updated_by="creator",
        ).model_dump()
        invalid_messages = [
            manifest | {"pool_id": " Pool-A "},
            manifest | {"members": [{"public_key": "member"}]},
            manifest
            | {
                "members": [
                    {
                        "public_key": "member",
                        "role": "member",
                        "added_at": 1.0,
                        "added_by": "creator",
                        "node_id": "not-on-wire",
                    }
                ]
            },
            manifest | {"manifest_signatures": {"creator": 123}},
            update | {"action": "ban"},
            update | {"new_role": "creator"},
            update | {"updated_by": ""},
        ]

        for payload in invalid_messages:
            with self.subTest(payload=payload):
                self.assertIsNone(parse_message(self._pack(payload)))
