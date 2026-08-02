import pytest

from comet.usenet.nntp_config import parse_instance_servers, parse_personal_servers


def _server(**overrides):
    server = {
        "name": "personal",
        "host": "news.example.test",
        "port": 563,
        "tls_mode": "implicit",
        "username": "alice",
        "password": "secret",
        "connections": 2,
        "priority": 0,
    }
    server.update(overrides)
    return server


def test_personal_nntp_servers_are_strictly_normalized_without_persistence():
    parsed = parse_personal_servers([_server()])

    assert parsed[0].name == "personal"
    assert parsed[0].pipeline == 16
    assert parsed[0].backup is False


def test_instance_nntp_servers_use_the_same_normalized_connection_contract():
    parsed = parse_instance_servers([_server(name="instance")])

    assert parsed[0].name == "instance"


def test_explicit_nntp_pipeline_depth_is_preserved():
    parsed = parse_personal_servers([_server(pipeline=1)])

    assert parsed[0].pipeline == 1


def test_instance_nntp_servers_share_the_native_engine_cardinality_limit():
    with pytest.raises(ValueError, match="one to 16"):
        parse_instance_servers(
            [_server(name=f"instance_{index}") for index in range(17)]
        )


def test_nntp_hosts_are_canonical_for_dns_and_tls():
    unicode_host, ipv6 = parse_personal_servers(
        [
            _server(name="unicode", host="NËWS.Example.COM"),
            _server(name="ipv6", host="2001:0db8:0:0::1"),
        ]
    )

    assert unicode_host.host == "xn--nws-jma.example.com"
    assert ipv6.host == "2001:db8::1"


@pytest.mark.parametrize(
    "servers",
    [
        [],
        [_server(host="https://news.example.test")],
        [_server(host="news..example.test")],
        [_server(host="news_example.test")],
        [_server(host="news.example.test.")],
        [_server(host="fe80::1%eth0")],
        [_server(host="\u200d.example.test")],
        [_server(name="x" * 65)],
        [_server(name="primary"), _server(name="PRIMARY")],
        [_server(username="alice", password=None)],
        [_server(username="alice user", password="secret")],
        [_server(username="alice\x7f", password="secret")],
        [_server(username="\ud800", password="secret")],
        [_server(username="é" * 257, password="secret")],
        [_server(username="a" * 513, password="secret")],
        [_server(username="a" * 2049, password="secret")],
        [_server(tls_mode="opportunistic")],
        [_server(), _server()],
    ],
)
def test_personal_nntp_servers_reject_ambiguous_or_unsafe_shapes(servers):
    with pytest.raises(ValueError):
        parse_personal_servers(servers)
