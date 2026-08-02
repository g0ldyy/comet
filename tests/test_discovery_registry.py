import pytest

from comet.discovery.adapters.animetosho import AnimeToshoAdapter
from comet.discovery.adapters.easynews import EasynewsSearchAdapter
from comet.discovery.adapters.newznab import NewznabAdapter
from comet.discovery.adapters.stremio_addon import StremioAddonAdapter
from comet.discovery.registry import build_discovery_adapters

STREMIO_ADDON_SOURCE_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
EASYNEWS_SOURCE_ID = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
EASYNEWS_PROVIDER_ID = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"


def test_torbox_debrid_settings_do_not_create_a_discovery_adapter():
    config = {
        "schemaVersion": 1,
        "debridServices": [{"service": "torbox", "apiKey": "secret"}],
    }

    assert build_discovery_adapters(config, object()) == {}


def test_registry_does_not_hide_invalid_adapter_configuration():
    config = {
        "schemaVersion": 2,
        "discoverySources": [
            {
                "configurationId": "source",
                "kind": "newznab",
                "enabled": True,
                "options": {"endpoint": "not-a-url", "apiKey": "secret"},
            }
        ],
    }

    with pytest.raises(ValueError):
        build_discovery_adapters(config, object())


def test_registry_builds_the_server_managed_animetosho_source():
    config = {
        "schemaVersion": 2,
        "discoverySources": [
            {
                "configurationId": "source",
                "kind": "animetosho",
                "enabled": True,
                "options": {"maxResults": 100, "pageSize": 50},
            }
        ],
    }

    adapter = build_discovery_adapters(config, object())["source"]

    assert isinstance(adapter, AnimeToshoAdapter)
    assert adapter._configuration.max_results == 100
    assert adapter._configuration.page_size == 50


def test_registry_resolves_the_configured_stremio_addon_account():
    config = {
        "schemaVersion": 2,
        "accounts": {"addon": {"authorization": "Bearer secret"}},
        "discoverySources": [
            {
                "configurationId": STREMIO_ADDON_SOURCE_ID,
                "kind": "stremio_addon",
                "enabled": True,
                "accountId": "addon",
                "options": {"baseUrl": "https://addon.example/config"},
            }
        ],
    }

    adapter = build_discovery_adapters(
        config,
        object(),
    )[STREMIO_ADDON_SOURCE_ID]

    assert isinstance(adapter, StremioAddonAdapter)
    assert adapter._configuration.authorization == "Bearer secret"
    assert adapter._configuration.base_url == "https://addon.example/config"


def test_easynews_search_requires_the_matching_enabled_direct_provider():
    config = {
        "schemaVersion": 2,
        "accounts": {"easynews": {"username": "member", "password": "secret"}},
        "discoverySources": [
            {
                "configurationId": EASYNEWS_SOURCE_ID,
                "kind": "easynews",
                "enabled": True,
                "accountId": "easynews",
            }
        ],
        "playbackProviders": [],
    }

    assert build_discovery_adapters(config, object()) == {}
    config["playbackProviders"] = [
        {
            "configurationId": EASYNEWS_PROVIDER_ID,
            "kind": "easynews",
            "enabled": True,
            "accountId": "easynews",
        }
    ]

    recorder = object()
    adapters = build_discovery_adapters(
        config,
        object(),
        runtime_failure_recorder=recorder,
    )

    assert isinstance(adapters[EASYNEWS_SOURCE_ID], EasynewsSearchAdapter)
    assert (
        adapters[EASYNEWS_SOURCE_ID]._account.source_configuration_id
        == EASYNEWS_SOURCE_ID
    )
    assert adapters[EASYNEWS_SOURCE_ID]._runtime_failure_recorder is recorder


def test_easynews_search_automatically_targets_enabled_real_nzb_providers():
    target_id = "11111111-1111-4111-8111-111111111111"
    disabled_target_id = "22222222-2222-4222-8222-222222222222"
    torrent_target_id = "33333333-3333-4333-8333-333333333333"
    second_target_id = "44444444-4444-4444-8444-444444444444"
    config = {
        "schemaVersion": 2,
        "accounts": {"easynews": {"username": "member", "password": "secret"}},
        "discoverySources": [
            {
                "configurationId": EASYNEWS_SOURCE_ID,
                "kind": "easynews",
                "enabled": True,
                "accountId": "easynews",
            }
        ],
        "playbackProviders": [
            {
                "configurationId": target_id,
                "kind": "stremio_nntp",
                "enabled": True,
            },
            {
                "configurationId": disabled_target_id,
                "kind": "nzbdav",
                "enabled": False,
            },
            {
                "configurationId": torrent_target_id,
                "kind": "realdebrid",
                "enabled": True,
            },
            {
                "configurationId": second_target_id,
                "kind": "torbox_usenet",
                "enabled": True,
            },
        ],
    }

    adapter = build_discovery_adapters(
        config,
        object(),
    )[EASYNEWS_SOURCE_ID]

    assert isinstance(adapter, EasynewsSearchAdapter)
    assert adapter._account.provider_configuration_id is None
    assert adapter._account.source_configuration_id == EASYNEWS_SOURCE_ID
    assert adapter._account.generated_provider_kinds == frozenset(
        {"stremio_nntp", "torbox_usenet"}
    )


def test_newznab_presets_share_one_adapter_and_resolve_account_options():
    regular_session = object()
    user_session = object()
    for kind in ("newznab", "nzbhydra2", "prowlarr_usenet"):
        config = {
            "schemaVersion": 2,
            "accounts": {"indexer": {"apiKey": "secret"}},
            "discoverySources": [
                {
                    "configurationId": "source",
                    "kind": kind,
                    "enabled": True,
                    "accountId": "indexer",
                    "options": {
                        "endpoint": "https://indexer.example/api",
                        "userAgentMode": "custom",
                        "queryUserAgent": "Query-UA",
                        "grabUserAgent": "Grab-UA",
                    },
                }
            ],
        }

        adapter = build_discovery_adapters(
            config,
            regular_session,
            user_session=user_session,
        )["source"]

        assert isinstance(adapter, NewznabAdapter)
        assert adapter._account.api_key == "secret"
        assert adapter._request_session is user_session


def test_newznab_sources_have_independent_governor_scopes():
    config = {
        "schemaVersion": 2,
        "discoverySources": [
            {
                "configurationId": f"source-{index}",
                "kind": "newznab",
                "enabled": True,
                "options": {
                    "endpoint": f"https://indexer-{index}.example/api",
                    "apiKey": f"secret-{index}",
                    "userAgentMode": "custom",
                },
            }
            for index in range(4)
        ],
    }
    partition = b"a" * 32

    adapters = build_discovery_adapters(
        config,
        object(),
        database=object(),
        account_partition=partition,
    )
    scopes = {adapter._governor_scope for adapter in adapters.values()}

    assert len(scopes) == 4
    assert all(isinstance(scope, bytes) and len(scope) == 32 for scope in scopes)
    assert scopes == {
        adapter._governor_scope
        for adapter in build_discovery_adapters(
            config,
            object(),
            database=object(),
            account_partition=partition,
        ).values()
    }
