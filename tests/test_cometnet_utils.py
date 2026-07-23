import unittest

from comet.cometnet.utils import (
    extract_ip_from_address,
    format_websocket_url,
    replace_websocket_url_port,
)


class CometNetUtilsTests(unittest.TestCase):
    def test_format_websocket_url_supports_ipv4_hostname_and_ipv6(self):
        cases = [
            ("192.0.2.1", 8765, "ws", "ws://192.0.2.1:8765"),
            ("peer.example", 443, "wss", "wss://peer.example:443"),
            ("2001:db8::1", 8765, "ws", "ws://[2001:db8::1]:8765"),
            ("[2001:db8::1]", 8765, "ws", "ws://[2001:db8::1]:8765"),
            ("fe80::1%eth0", 8765, "ws", "ws://[fe80::1%eth0]:8765"),
        ]
        for host, port, scheme, expected in cases:
            with self.subTest(host=host):
                self.assertEqual(
                    format_websocket_url(host, port, scheme),
                    expected,
                )

    def test_replace_websocket_url_port_preserves_ipv6_and_url_suffix(self):
        self.assertEqual(
            replace_websocket_url_port(
                "wss://[2001:db8::1]:49152/cometnet/ws?token=value",
                8765,
            ),
            "wss://[2001:db8::1]:8765/cometnet/ws?token=value",
        )

    def test_extract_ip_from_address_supports_ipv6_forms(self):
        cases = [
            ("wss://[2001:db8::1]:8765/cometnet/ws", "2001:db8::1"),
            ("[2001:db8::1]:8765", "2001:db8::1"),
            ("2001:0db8:0:0:0:0:0:1", "2001:db8::1"),
            ("ws://192.0.2.1:8765", "192.0.2.1"),
            ("peer.example:8765", "peer.example"),
        ]
        for address, expected in cases:
            with self.subTest(address=address):
                self.assertEqual(extract_ip_from_address(address), expected)
