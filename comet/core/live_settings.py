"""Load and atomically publish durable operator-setting revisions."""

from __future__ import annotations

import asyncio
import os
import signal
from dataclasses import dataclass

from comet.core.operator_settings import resolve_effective_settings
from comet.core.settings_policy import (
    COMETNET_SETTING_KEYS,
    COMPONENT_SETTING_KEYS,
    FILTER_SETTING_KEYS,
    HTTP_CLIENT_SETTING_KEYS,
    INDEXER_MANAGER_SETTING_KEYS,
    NETWORK_CLIENT_SETTING_KEYS,
    SECURITY_SETTING_KEYS,
    USENET_ENGINE_SETTING_KEYS,
    settings_requiring_restart,
)


async def _restart_cometnet(config) -> None:
    from comet.cometnet.manager import (
        init_cometnet_service,
        stop_cometnet_service,
    )
    from comet.cometnet.relay import init_relay, stop_relay
    from comet.services.torrent_manager import (
        check_torrents_exist,
        torrent_update_queue,
    )

    await stop_relay()
    await stop_cometnet_service()
    if config.COMETNET_RELAY_URL:
        await init_relay(
            config.COMETNET_RELAY_URL,
            api_key=config.COMETNET_API_KEY,
        )
    elif config.COMETNET_ENABLED:
        service = init_cometnet_service(
            enabled=True,
            listen_port=config.COMETNET_LISTEN_PORT,
            bootstrap_nodes=config.COMETNET_BOOTSTRAP_NODES,
            manual_peers=config.COMETNET_MANUAL_PEERS,
            max_peers=config.COMETNET_MAX_PEERS,
            min_peers=config.COMETNET_MIN_PEERS,
        )
        service.set_save_torrent_callback(torrent_update_queue.add_network_torrent)
        service.set_check_torrents_exist_callback(check_torrents_exist)
        await service.start()


@dataclass(frozen=True, slots=True)
class SettingsApplication:
    revision: int
    live_keys: tuple[str, ...]
    component_keys: tuple[str, ...]
    restart_keys: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PreparedSettingsApplication:
    current: object
    candidate: object
    changed_keys: tuple[str, ...]
    application: SettingsApplication
    logging: object
    logging_changed: bool


_apply_lock = asyncio.Lock()
_observed_revision = 0
_pending_restart_keys: tuple[str, ...] = ()


def observed_revision() -> int:
    return _observed_revision


def pending_restart_keys() -> tuple[str, ...]:
    return _pending_restart_keys


def record_settings_application(application: SettingsApplication) -> None:
    global _observed_revision, _pending_restart_keys
    _observed_revision = application.revision
    _pending_restart_keys = application.restart_keys


async def prepare_settings_application(database) -> PreparedSettingsApplication:
    from comet.core.models import AppSettings, finalize_app_settings, settings
    from comet.observability.logging import LoggingSettings, current_settings

    payload = await resolve_effective_settings(database)
    current = settings.active_snapshot()
    desired = finalize_app_settings(
        AppSettings(**payload.values),
        generated_keys=payload.generated_keys,
        revision=payload.revision,
    )
    application_changed = tuple(
        key
        for key in AppSettings.model_fields
        if key.isupper() and getattr(current, key) != getattr(desired, key)
    )
    logging_values = {
        key: value
        for key, value in payload.values.items()
        if key in LoggingSettings.model_fields
    }
    logging = LoggingSettings(**logging_values)
    active_logging = current_settings()
    logging_keys = tuple(
        key
        for key in LoggingSettings.model_fields
        if getattr(active_logging, key) != getattr(logging, key)
    )
    changed = application_changed + logging_keys
    restart_key_set = settings_requiring_restart(changed)
    restart_keys = tuple(key for key in changed if key in restart_key_set)
    component_keys = tuple(
        key
        for key in changed
        if key not in restart_key_set and key in COMPONENT_SETTING_KEYS
    )
    live_keys = tuple(
        key
        for key in changed
        if key not in restart_key_set and key not in COMPONENT_SETTING_KEYS
    )
    values = desired.model_dump()
    for key in restart_keys:
        values[key] = getattr(current, key)
    candidate = finalize_app_settings(
        AppSettings(_env_file=None, **values),
        generated_keys=payload.generated_keys,
        revision=payload.revision,
    )
    if restart_keys:
        candidate._applied_settings_revision = current.APPLIED_SETTINGS_REVISION
    application = SettingsApplication(
        payload.revision,
        live_keys,
        component_keys,
        restart_keys,
    )
    return PreparedSettingsApplication(
        current,
        candidate,
        tuple(key for key in changed if key not in restart_key_set),
        application,
        logging,
        bool(logging_keys),
    )


async def apply_latest_settings(database) -> SettingsApplication:
    """Apply the latest revision once per process without partial publication."""

    from comet.api.v1.security import configure_security
    from comet.background_scraper.worker import background_scraper
    from comet.core.execution import (
        discard_executor,
        install_executor,
        prepare_executor,
    )
    from comet.core.models import (
        _build_database_instance,
        settings,
    )
    from comet.observability import create_detached_task
    from comet.observability.logging import (
        configure,
        current_process_role,
    )
    from comet.services.dmm_ingester import dmm_ingester
    from comet.services.filtering import configure_filtering
    from comet.services.indexer_manager import indexer_manager
    from comet.services.usenet_operations import usenet_operation_monitor
    from comet.utils.http_client import http_client_manager
    from comet.utils.memory import memory_trimmer
    from comet.utils.network_manager import network_manager

    async with _apply_lock:
        prepared = await prepare_settings_application(database)
        current = prepared.current
        candidate = prepared.candidate
        changed = prepared.changed_keys
        application = prepared.application
        next_logging = prepared.logging
        logging_changed = prepared.logging_changed
        next_http_session = None
        next_user_http_session = None
        next_replicas = None
        next_executor = None
        try:
            if not HTTP_CLIENT_SETTING_KEYS.isdisjoint(changed):
                next_http_session = http_client_manager.build(candidate)
                next_user_http_session = http_client_manager.build_user(candidate)
            if "DATABASE_READ_REPLICA_URLS" in changed:
                next_replicas = await database.prepare_replicas(
                    [
                        _build_database_instance(url)
                        for url in candidate.DATABASE_READ_REPLICA_URLS
                    ]
                )
            if "EXECUTOR_MAX_WORKERS" in changed or logging_changed:
                next_executor = prepare_executor(
                    candidate.EXECUTOR_MAX_WORKERS,
                    next_logging.LOG_PROFILE.value,
                    next_logging.LOG_FORMAT.value,
                    next_logging.no_color,
                )
        except BaseException:
            if next_http_session is not None:
                await next_http_session.close()
            if next_user_http_session is not None:
                await next_user_http_session.close()
            if next_replicas is not None:
                await database.retire_replicas(next_replicas)
            discard_executor(next_executor)
            raise
        if not COMETNET_SETTING_KEYS.isdisjoint(changed):
            try:
                with settings.bind_snapshot(candidate):
                    await _restart_cometnet(candidate)
            except BaseException:
                if next_http_session is not None:
                    await next_http_session.close()
                if next_user_http_session is not None:
                    await next_user_http_session.close()
                if next_replicas is not None:
                    await database.retire_replicas(next_replicas)
                discard_executor(next_executor)
                with settings.bind_snapshot(current):
                    await _restart_cometnet(current)
                raise
        if next_executor is not None:
            install_executor(next_executor)
        if not SECURITY_SETTING_KEYS.isdisjoint(changed):
            configure_security(candidate)
        if logging_changed:
            configure(next_logging, process_role=current_process_role())
        if not FILTER_SETTING_KEYS.isdisjoint(changed):
            configure_filtering(candidate)
        if not INDEXER_MANAGER_SETTING_KEYS.isdisjoint(changed):
            indexer_manager.reconfigure(candidate)
        if {"DMM_INGEST_CONCURRENT_WORKERS", "DMM_INGEST_INTERVAL"}.intersection(
            changed
        ):
            dmm_ingester.reconfigure(candidate)
        if "BACKGROUND_SCRAPER_INTERVAL" in changed:
            background_scraper.reconfigure()
        if "MEMORY_TRIM_INTERVAL" in changed:
            memory_trimmer.reconfigure(candidate)
        if "USENET_RUNTIME_DIR" in changed:
            usenet_operation_monitor.reconfigure(candidate)
        settings.publish(candidate)
        previous_replicas = (
            database.replace_replicas(next_replicas)
            if next_replicas is not None
            else None
        )
        if current.BACKGROUND_SCRAPER_ENABLED != candidate.BACKGROUND_SCRAPER_ENABLED:
            if candidate.BACKGROUND_SCRAPER_ENABLED:
                background_scraper.task = create_detached_task(
                    background_scraper.start(),
                    name="background.scraper",
                )
            else:
                await background_scraper.stop()
        if current.DMM_INGEST_ENABLED != candidate.DMM_INGEST_ENABLED:
            if candidate.DMM_INGEST_ENABLED:
                create_detached_task(dmm_ingester.start(), name="dmm.ingester")
            else:
                await dmm_ingester.stop()
        if next_http_session is not None:
            await http_client_manager.replace(next_http_session)
            await http_client_manager.replace_user(next_user_http_session)
        if not (NETWORK_CLIENT_SETTING_KEYS | HTTP_CLIENT_SETTING_KEYS).isdisjoint(
            changed
        ):
            await network_manager.retire_all()
        if previous_replicas is not None:
            await database.retire_replicas(previous_replicas)
        record_settings_application(application)
        supervisor_pid = os.environ.get("COMET_RUNTIME_SUPERVISOR_PID")
        if supervisor_pid and (
            not USENET_ENGINE_SETTING_KEYS.isdisjoint(changed) or logging_changed
        ):
            os.kill(int(supervisor_pid), signal.SIGUSR1)
        return application
