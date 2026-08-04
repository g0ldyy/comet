"""Instantiate playback providers only from explicit v2 profile bindings."""

from collections.abc import Mapping

from comet.core.capability_bindings import resolve_capability_options
from comet.core.credentials import (
    api_credential as _api_credential,
)
from comet.core.credentials import (
    easynews_credentials as _easynews_credentials,
)
from comet.core.models import settings
from comet.core.provider_governor import ProviderGovernor
from comet.core.sources import TORRENT_PROVIDER_KINDS
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
from comet.playback.tokens import CapabilityCodec
from comet.usenet.access import NativeAccessAuthorizer


def build_playback_providers(
    config: Mapping[str, object],
    session,
    *,
    user_session=None,
    database=None,
    client_ip: str = "",
    eligible_configuration_ids: frozenset[str] | None = None,
) -> dict[str, object]:
    """Build provider instances without inferring a legacy credential binding."""
    if config.get("schemaVersion") != 2:
        return {}
    user_session = session if user_session is None else user_session
    accounts = config.get("accounts") or {}
    debrid_entries = {
        (binding["configurationId"], binding["service"]): binding
        for binding in config.get("_debridEntries") or ()
        if binding.get("configurationId") is not None
    }
    result = {}
    for entry in config.get("playbackProviders") or []:
        if not entry["enabled"]:
            continue
        configuration_id = entry["configurationId"]
        if (
            eligible_configuration_ids is not None
            and configuration_id not in eligible_configuration_ids
        ):
            continue
        kind = entry["kind"]
        options = resolve_capability_options(entry, accounts)
        if kind == "stremio_nntp":
            result[configuration_id] = StremioNntpProvider()
        elif kind == "nzbdav":
            result[configuration_id] = NzbDavProvider(user_session)
        elif kind == "altmount":
            result[configuration_id] = AltMountProvider(user_session)
        elif kind == "comet_native_usenet":
            result[configuration_id] = NativeUsenetProvider(
                NativeAccessAuthorizer(settings.USENET_NATIVE_ACCESS_TOKEN).error_code(
                    config.get("nativeAccessToken")
                )
            )
        elif kind == "torbox_usenet":
            api_key = _api_credential(options)
            account_id = entry.get("accountId")
            if api_key is None and account_id is not None:
                api_key = _api_credential(accounts.get(account_id))
            if api_key is None:
                continue
            provider = TorBoxUsenetProvider(session, api_key, client_ip)
            if database is not None:
                endpoint, credential_material = provider.credential_binding()
                scope = bytes.fromhex(
                    CapabilityCodec(
                        settings.COMET_CAPABILITY_SECRET
                    ).provider_credential_fingerprint(
                        "torbox_usenet",
                        endpoint,
                        credential_material,
                    )
                )
                provider = TorBoxUsenetProvider(
                    session,
                    api_key,
                    client_ip,
                    governor=ProviderGovernor(database),
                    governor_scope=scope,
                )
            result[configuration_id] = provider
        elif kind == "easynews":
            credentials = _easynews_credentials(options)
            account_id = entry.get("accountId")
            if credentials is None and account_id is not None:
                credentials = _easynews_credentials(accounts.get(account_id))
            if credentials is None:
                continue
            result[configuration_id] = EasynewsProvider(session, *credentials)
        elif kind == "stremthru_newz":
            provider = StremThruNewzProvider(
                user_session,
                options,
            )
            if database is not None:
                try:
                    endpoint, credential_material = provider.credential_binding()
                except ValueError:
                    result[configuration_id] = provider
                    continue
                scope = bytes.fromhex(
                    CapabilityCodec(
                        settings.COMET_CAPABILITY_SECRET
                    ).provider_credential_fingerprint(
                        "stremthru_newz",
                        endpoint,
                        credential_material,
                    )
                )
                provider = StremThruNewzProvider(
                    user_session,
                    options,
                    governor=ProviderGovernor(database),
                    governor_scope=scope,
                )
            result[configuration_id] = provider
        elif kind in TORRENT_PROVIDER_KINDS and kind != "direct_torrent":
            binding = debrid_entries.get((configuration_id, kind))
            if binding is None:
                continue
            result[configuration_id] = TorrentDebridProvider(
                session,
                kind,
                binding.get("apiKey"),
                client_ip,
            )
    return result
