"""Instantiate discovery adapters only for explicitly configured v2 sources."""

import hashlib
from collections.abc import Mapping
from pathlib import Path

from comet.core.capability_bindings import resolve_capability_options
from comet.core.credentials import (
    easynews_credentials as _easynews_credentials,
)
from comet.core.discovery_sources import effective_discovery_sources
from comet.core.models import settings
from comet.core.provider_governor import ProviderGovernor
from comet.core.sources import REAL_NZB_PROVIDER_KINDS
from comet.discovery.adapters.animetosho import (
    AnimeToshoAdapter,
    animetosho_configuration,
)
from comet.discovery.adapters.easynews import (
    EasynewsSearchAccount,
    EasynewsSearchAdapter,
)
from comet.discovery.adapters.newznab import (
    NewznabAdapter,
    newznab_account_from_options,
)
from comet.discovery.adapters.stremio_addon import (
    StremioAddonAdapter,
    stremio_addon_configuration,
)
from comet.usenet.engine_client import EngineClient
from comet.usenet.nzb_broker import NzbBroker

_NEWZNAB_LABELS = {
    "newznab": "Newznab",
    "nzbhydra2": "NZBHydra2",
    "prowlarr_usenet": "Prowlarr",
}


def _governor_scope(account_partition: bytes, source_id: str) -> bytes:
    return hashlib.sha256(
        b"comet-discovery-governor-v1\0" + account_partition + source_id.encode("utf-8")
    ).digest()


def build_discovery_adapters(
    config: Mapping[str, object],
    session,
    *,
    user_session=None,
    database=None,
    account_partition: bytes | None = None,
    runtime_failure_recorder=None,
) -> dict:
    """Build adapters reachable from one explicit v2 user configuration."""
    if config.get("schemaVersion") != 2:
        return {}
    user_session = session if user_session is None else user_session

    accounts = config.get("accounts")
    if accounts is None:
        accounts = {}
    elif not isinstance(accounts, Mapping):
        raise ValueError("discovery accounts are invalid")
    providers = config.get("playbackProviders")
    if providers is None:
        providers = []
    elif not isinstance(providers, list):
        raise ValueError("playback providers are invalid")
    governor = (
        ProviderGovernor(database)
        if database is not None and account_partition is not None
        else None
    )
    adapters = {}
    broker = None
    for source in effective_discovery_sources(config):
        if not source.get("enabled"):
            continue
        source_id = source.get("configurationId")
        if not isinstance(source_id, str) or not source_id:
            raise ValueError("discovery source is invalid")
        source_kind = source.get("kind")
        governor_scope = (
            _governor_scope(account_partition, source_id)
            if governor is not None
            else None
        )
        if source_kind == "animetosho":
            configuration = animetosho_configuration(
                source_id,
                source.get("options"),
            )
            adapters[source_id] = AnimeToshoAdapter(
                session,
                configuration,
                governor=governor,
            )
            continue
        if source_kind == "stremio_addon":
            configuration = stremio_addon_configuration(
                source_id,
                resolve_capability_options(source, accounts),
            )
            if (
                broker is None
                and database is not None
                and account_partition is not None
            ):
                broker = NzbBroker(
                    settings.USENET_ARTIFACT_DIR,
                    database,
                    EngineClient(Path(settings.USENET_RUNTIME_DIR) / "engine.json"),
                )
            adapters[source_id] = StremioAddonAdapter(
                configuration,
                broker=broker,
                governor=governor,
                governor_scope=governor_scope,
            )
            continue
        if source_kind in _NEWZNAB_LABELS:
            account = newznab_account_from_options(
                resolve_capability_options(source, accounts),
                source_id,
                label=_NEWZNAB_LABELS[source_kind],
            )
            adapters[source_id] = NewznabAdapter(
                user_session,
                account,
                governor=governor,
                governor_scope=governor_scope,
            )
            continue
        if source_kind == "easynews":
            account_id = source.get("accountId")
            credentials = _easynews_credentials(source.get("options"))
            if credentials is None and isinstance(account_id, str):
                credentials = _easynews_credentials(accounts.get(account_id))
            matching_provider_id = next(
                (
                    provider.get("configurationId")
                    for provider in providers
                    if provider.get("enabled")
                    and provider.get("kind") == "easynews"
                    and provider.get("accountId") == account_id
                    and isinstance(provider.get("configurationId"), str)
                ),
                None,
            )
            generated_provider_kinds = frozenset(
                provider["kind"]
                for provider in providers
                if provider.get("enabled")
                and provider.get("kind") in REAL_NZB_PROVIDER_KINDS
                and isinstance(provider.get("configurationId"), str)
            )
            if credentials is not None and (
                matching_provider_id is not None or generated_provider_kinds
            ):
                account = EasynewsSearchAccount(
                    *credentials,
                    matching_provider_id,
                    source_id,
                    generated_provider_kinds,
                )
                adapters[source_id] = EasynewsSearchAdapter(
                    session,
                    account,
                    governor=governor,
                    governor_scope=governor_scope,
                    runtime_failure_recorder=runtime_failure_recorder,
                )
            continue
    return adapters
