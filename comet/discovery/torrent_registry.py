import hashlib
import importlib
import inspect
import pkgutil
from pathlib import Path

import orjson

from comet.core.discovery_sources import instance_discovery_source_id
from comet.core.models import settings
from comet.core.scrape import ScrapeContext, normalize_scraper_name
from comet.core.settings_catalog import build_settings_catalog
from comet.discovery.capabilities import DiscoveryBranchFingerprint
from comet.discovery.torrent_base import TorrentDiscoveryAdapter
from comet.discovery.torrent_models import ScrapeRequest
from comet.services.anime import anime_mapper
from comet.utils.network_manager import network_manager
from comet.utils.parsing import (
    associate_urls_credentials,
    parse_url_scrape_mode,
    url_mode_matches_context,
)

SERVER_TORRENT_ACCOUNT_PARTITION = hashlib.sha256(
    b"comet-server-torrent-public-partition-v1"
).digest()


class TorrentAdapterRegistry:
    def __init__(self):
        self.adapter_types: dict[str, type[TorrentDiscoveryAdapter]] = {}
        self.discover_adapters()

    def discover_adapters(self) -> None:
        """Discover the server-configured torrent DiscoveryAdapter classes."""
        package = "comet.discovery.adapters.torrent"
        path = Path(__file__).parent / "adapters" / "torrent"

        for _, module_name, _ in pkgutil.iter_modules([str(path)]):
            if module_name == "helpers":
                continue

            module = importlib.import_module(f"{package}.{module_name}")

            # Find classes inheriting from TorrentDiscoveryAdapter
            for _name, obj in inspect.getmembers(module):
                if (
                    inspect.isclass(obj)
                    and issubclass(obj, TorrentDiscoveryAdapter)
                    and obj is not TorrentDiscoveryAdapter
                ):
                    self.adapter_types[obj.__name__] = obj

    @staticmethod
    def _resolve_timeout(
        scraper_class: type[TorrentDiscoveryAdapter], context: ScrapeContext
    ) -> float:
        normalized_name = normalize_scraper_name(scraper_class.__name__)
        overrides = settings.SCRAPER_TIMEOUT_OVERRIDES
        context_selector = f"{normalized_name}:{context.value}"
        if context_selector in overrides:
            return overrides[context_selector]
        if normalized_name in overrides:
            return overrides[normalized_name]

        timeout = (
            settings.LIVE_SCRAPE_TIMEOUT
            if context == ScrapeContext.LIVE
            else settings.BACKGROUND_SCRAPE_TIMEOUT
        )
        if scraper_class.startup_timeout_setting is not None:
            timeout += settings.__dict__[scraper_class.startup_timeout_setting]
        return timeout

    @staticmethod
    def _resolve_url_for_context(url: str, context: str):
        parsed_url, mode = parse_url_scrape_mode(url)
        if not parsed_url or not url_mode_matches_context(mode, context):
            return None
        return parsed_url

    def build_adapters(
        self, request: ScrapeRequest
    ) -> dict[str, TorrentDiscoveryAdapter]:
        """Instantiate only enabled torrent DiscoveryAdapter implementations."""
        adapters = {}
        is_anime_content = None
        for scraper_name, scraper_class in self.adapter_types.items():
            scraper_name_clean = scraper_name.removesuffix("Scraper")
            enabled_setting = f"SCRAPE_{scraper_name_clean.upper()}"
            if not settings.is_scraper_enabled(
                settings.__dict__[enabled_setting], request.context
            ):
                continue

            anime_only_setting = scraper_class.anime_only_setting
            if anime_only_setting is not None and settings.__dict__[anime_only_setting]:
                if is_anime_content is None:
                    is_anime_content = anime_mapper.is_anime_content(
                        request.media_id, request.media_only_id
                    )
                if not is_anime_content:
                    continue

            url_credentials_pairs = None
            if scraper_class.url_setting is not None:
                credential_setting = scraper_class.credential_setting
                url_credentials_pairs = [
                    (resolved_url, credentials)
                    for url, credentials in associate_urls_credentials(
                        settings.__dict__[scraper_class.url_setting],
                        (
                            settings.__dict__[credential_setting]
                            if credential_setting is not None
                            else None
                        ),
                    )
                    if (
                        resolved_url := self._resolve_url_for_context(
                            url, request.context
                        )
                    )
                    is not None
                ]
                if not url_credentials_pairs:
                    continue

            scrape_timeout = self._resolve_timeout(scraper_class, request.context)
            client = network_manager.get_client(
                scraper_name=scraper_name_clean, impersonate=scraper_class.impersonate
            )

            if url_credentials_pairs is not None:
                self._register_url_adapters(
                    adapters,
                    scraper_name_clean,
                    scraper_class,
                    client,
                    scrape_timeout,
                    url_credentials_pairs,
                )
            else:
                scraper = scraper_class(self, client)
                self._register_adapter(
                    adapters,
                    scraper_name_clean,
                    scraper,
                    scrape_timeout,
                )
        return adapters

    @staticmethod
    def branch_fingerprints(
        adapters: dict[str, TorrentDiscoveryAdapter],
        context: ScrapeContext,
    ) -> dict[tuple[str, str], DiscoveryBranchFingerprint]:
        """Bind shared source coverage to the effective server configuration."""

        catalog = {entry.key: entry.category for entry in build_settings_catalog()}
        configuration = settings.model_dump(mode="json")
        source_configuration = {
            key: value
            for key, value in configuration.items()
            if catalog.get(key) in {"scrapers_proxies", "discovery_indexers"}
            or key.startswith("DMM_")
        }
        generation = hashlib.sha256(
            b"comet-server-torrent-settings-v1\0"
            + orjson.dumps(source_configuration, option=orjson.OPT_SORT_KEYS)
        ).digest()
        result = {}
        for configuration_id, adapter in adapters.items():
            fingerprint = hashlib.sha256(
                b"comet-server-torrent-branch-v1\0"
                + generation
                + b"\0"
                + configuration_id.encode("utf-8")
                + b"\0"
                + type(adapter).__module__.encode("utf-8")
                + b"."
                + type(adapter).__qualname__.encode("utf-8")
                + b"\0"
                + context.value.encode("ascii")
            ).hexdigest()
            result[(configuration_id, "bittorrent")] = DiscoveryBranchFingerprint(
                configuration_id,
                "bittorrent",
                fingerprint,
                public_visibility=True,
            )
        return result

    def _register_url_adapters(
        self,
        adapters,
        scraper_name,
        scraper_class,
        client,
        scrape_timeout,
        url_credentials,
    ) -> None:
        active_instance_count = 0
        for url, credentials in url_credentials:
            active_instance_count += 1
            args = (self, client, url)
            if scraper_class.credential_setting is not None:
                args += (credentials,)
            scraper = scraper_class(*args)
            self._register_adapter(
                adapters,
                f"{scraper_name} #{active_instance_count}",
                scraper,
                scrape_timeout,
            )

    @staticmethod
    def _register_adapter(
        adapters: dict[str, TorrentDiscoveryAdapter],
        display_name: str,
        adapter: TorrentDiscoveryAdapter,
        timeout: float,
    ) -> None:
        source_key = "server-torrent:" + normalize_scraper_name(
            display_name.partition(" #")[0]
        )
        configuration_id = instance_discovery_source_id(source_key)
        suffix = 2
        while configuration_id in adapters:
            configuration_id = instance_discovery_source_id(f"{source_key}:{suffix}")
            suffix += 1
        adapter.discovery_name = display_name
        adapter.discovery_timeout = timeout
        adapters[configuration_id] = adapter


torrent_adapter_registry = TorrentAdapterRegistry()
