import base64
from unittest.mock import patch

import pytest

from comet.playback.providers import (
    AltMountProvider,
    EasynewsProvider,
    NativeUsenetProvider,
    NzbDavProvider,
    StremioNntpProvider,
    StremThruNewzProvider,
    TorBoxUsenetProvider,
    TorrentDebridProvider,
)
from comet.playback.registry import build_playback_providers


def test_registry_uses_only_explicit_v2_provider_credentials():
    config = {
        "schemaVersion": 2,
        "accounts": {"torbox": {"apiKey": "account-key"}},
        "playbackProviders": [
            {
                "configurationId": "torbox",
                "kind": "torbox_usenet",
                "enabled": True,
                "accountId": "torbox",
            },
            {
                "configurationId": "nntp",
                "kind": "stremio_nntp",
                "enabled": True,
            },
        ],
        "debridServices": [{"service": "torbox", "apiKey": "legacy-key"}],
    }

    providers = build_playback_providers(config, object())

    assert isinstance(providers["torbox"], TorBoxUsenetProvider)
    assert isinstance(providers["nntp"], StremioNntpProvider)


def test_registry_does_not_create_torbox_from_legacy_settings_or_disabled_binding():
    assert (
        build_playback_providers(
            {
                "schemaVersion": 1,
                "debridServices": [{"service": "torbox", "apiKey": "secret"}],
            },
            object(),
        )
        == {}
    )


def test_registry_builds_debrid_only_from_the_exact_normalized_v2_binding():
    provider_id = "11111111-1111-4111-8111-111111111111"
    providers = build_playback_providers(
        {
            "schemaVersion": 2,
            "_debridEntries": [
                {
                    "configurationId": provider_id,
                    "service": "realdebrid",
                    "apiKey": "account-key",
                }
            ],
            "playbackProviders": [
                {
                    "configurationId": provider_id,
                    "kind": "realdebrid",
                    "enabled": True,
                }
            ],
        },
        object(),
    )

    assert isinstance(providers[provider_id], TorrentDebridProvider)
    assert providers[provider_id].descriptor.kind == "realdebrid"


def test_registry_keeps_easynews_separate_from_other_account_credentials():
    providers = build_playback_providers(
        {
            "schemaVersion": 2,
            "accounts": {"easynews": {"username": "member", "password": "secret"}},
            "playbackProviders": [
                {
                    "configurationId": "easynews",
                    "kind": "easynews",
                    "enabled": True,
                    "accountId": "easynews",
                }
            ],
        },
        object(),
    )

    assert isinstance(providers["easynews"], EasynewsProvider)


def test_registry_requires_complete_stremthru_newz_options():
    providers = build_playback_providers(
        {
            "schemaVersion": 2,
            "playbackProviders": [
                {
                    "configurationId": "newz",
                    "kind": "stremthru_newz",
                    "enabled": True,
                    "options": {
                        "baseUrl": "https://bridge.example",
                        "authToken": "user:pass",
                    },
                }
            ],
        },
        object(),
    )

    assert isinstance(providers["newz"], StremThruNewzProvider)


def test_user_controlled_http_providers_use_the_strict_session():
    regular_session = object()
    user_session = object()
    providers = build_playback_providers(
        {
            "schemaVersion": 2,
            "playbackProviders": [
                {
                    "configurationId": "altmount",
                    "kind": "altmount",
                    "enabled": True,
                },
                {
                    "configurationId": "nzbdav",
                    "kind": "nzbdav",
                    "enabled": True,
                },
                {
                    "configurationId": "newz",
                    "kind": "stremthru_newz",
                    "enabled": True,
                    "options": {
                        "baseUrl": "https://bridge.example",
                        "authToken": "user:pass",
                    },
                },
                {
                    "configurationId": "torbox",
                    "kind": "torbox_usenet",
                    "enabled": True,
                    "options": {"apiKey": "secret"},
                },
            ],
        },
        regular_session,
        user_session=user_session,
    )

    assert isinstance(providers["altmount"], AltMountProvider)
    assert isinstance(providers["nzbdav"], NzbDavProvider)
    assert providers["altmount"]._session is user_session
    assert providers["nzbdav"]._session is user_session
    assert providers["newz"]._session is user_session
    assert providers["torbox"]._session is regular_session


def test_registry_gives_stremthru_one_database_coordinated_account_scope():
    secret = base64.urlsafe_b64encode(b"s" * 32).decode().rstrip("=")
    provider = {
        "kind": "stremthru_newz",
        "enabled": True,
        "options": {
            "baseUrl": "https://bridge.example",
            "authToken": "user:pass",
        },
    }
    config = {
        "schemaVersion": 2,
        "playbackProviders": [
            {
                **provider,
                "configurationId": "newz-a",
            },
            {
                **provider,
                "configurationId": "newz-b",
            },
            {
                **provider,
                "configurationId": "newz-other",
                "options": {
                    **provider["options"],
                    "authToken": "user:other",
                },
            },
        ],
    }
    database = object()

    with patch(
        "comet.playback.registry.settings.COMET_CAPABILITY_SECRET",
        secret,
    ):
        providers = build_playback_providers(
            config,
            object(),
            database=database,
        )

    assert providers["newz-a"]._governor._database is database
    assert isinstance(providers["newz-a"]._governor_scope, bytes)
    assert len(providers["newz-a"]._governor_scope) == 32
    assert providers["newz-a"]._governor_scope == providers["newz-b"]._governor_scope
    assert (
        providers["newz-a"]._governor_scope != providers["newz-other"]._governor_scope
    )


def test_registry_gives_torbox_one_database_coordinated_account_scope():
    secret = base64.urlsafe_b64encode(b"s" * 32).decode().rstrip("=")
    config = {
        "schemaVersion": 2,
        "accounts": {
            "same": {"apiKey": "account-key"},
            "other": {"apiKey": "other-key"},
        },
        "playbackProviders": [
            {
                "configurationId": "torbox-a",
                "kind": "torbox_usenet",
                "enabled": True,
                "accountId": "same",
            },
            {
                "configurationId": "torbox-b",
                "kind": "torbox_usenet",
                "enabled": True,
                "accountId": "same",
            },
            {
                "configurationId": "torbox-other",
                "kind": "torbox_usenet",
                "enabled": True,
                "accountId": "other",
            },
        ],
    }
    database = object()

    with patch(
        "comet.playback.registry.settings.COMET_CAPABILITY_SECRET",
        secret,
    ):
        providers = build_playback_providers(
            config,
            object(),
            database=database,
        )

    assert providers["torbox-a"]._governor._database is database
    assert isinstance(providers["torbox-a"]._governor_scope, bytes)
    assert len(providers["torbox-a"]._governor_scope) == 32
    assert (
        providers["torbox-a"]._governor_scope == providers["torbox-b"]._governor_scope
    )
    assert (
        providers["torbox-a"]._governor_scope
        != providers["torbox-other"]._governor_scope
    )


def test_registry_requires_the_native_access_token_for_native_playback():
    config = {
        "schemaVersion": 2,
        "nativeAccessToken": "wrong",
        "playbackProviders": [
            {
                "configurationId": "native",
                "kind": "comet_native_usenet",
                "enabled": True,
            }
        ],
    }
    with patch(
        "comet.playback.registry.settings.USENET_NATIVE_ACCESS_TOKEN", "correct"
    ):
        rejected = build_playback_providers(config, object())["native"]
        config["nativeAccessToken"] = "correct"
        providers = build_playback_providers(config, object())

    assert rejected._access_error_code == "native_access_token_rejected"
    assert isinstance(providers["native"], NativeUsenetProvider)
    assert providers["native"]._access_error_code is None
    assert (
        build_playback_providers(
            {
                "schemaVersion": 2,
                "playbackProviders": [
                    {
                        "configurationId": "torbox",
                        "kind": "torbox_usenet",
                        "enabled": False,
                        "options": {"apiKey": "key"},
                    }
                ],
            },
            object(),
        )
        == {}
    )


def test_blank_inline_easynews_credentials_fall_back_to_the_account():
    """A blank inline pair is not a credential, so the account must still supply one."""
    from comet.playback.registry import build_playback_providers

    config = {
        "schemaVersion": 2,
        "accounts": {"acct-1": {"username": "real-user", "password": "real-pass"}},
        "playbackProviders": [
            {
                "configurationId": "11111111-1111-4111-8111-111111111111",
                "kind": "easynews",
                "enabled": True,
                "displayName": "Easynews",
                "accountId": "acct-1",
                "options": {"username": "", "password": "secret"},
            }
        ],
    }

    built = build_playback_providers(config, session=object())

    assert list(built) == ["11111111-1111-4111-8111-111111111111"]


def test_credential_extraction_uses_the_shared_helpers():
    """One derivation: a drifted copy previously blocked the Easynews account fallback."""
    from comet.core.credentials import api_credential, easynews_credentials
    from comet.discovery.registry import _easynews_credentials as discovery_easynews
    from comet.playback.registry import _api_credential as playback_api
    from comet.playback.registry import _easynews_credentials as playback_easynews

    assert playback_api is api_credential
    assert discovery_easynews is playback_easynews is easynews_credentials


def test_registry_does_not_hide_provider_construction_failures():
    config = {
        "schemaVersion": 2,
        "accounts": {"torbox": {"apiKey": "account-key"}},
        "playbackProviders": [
            {
                "configurationId": "torbox",
                "kind": "torbox_usenet",
                "enabled": True,
                "accountId": "torbox",
            }
        ],
    }

    with (
        patch(
            "comet.playback.registry.TorBoxUsenetProvider",
            side_effect=ValueError("implementation failed"),
        ),
        pytest.raises(ValueError, match="implementation failed"),
    ):
        build_playback_providers(config, object())
