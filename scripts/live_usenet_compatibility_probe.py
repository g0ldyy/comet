#!/usr/bin/env python3
"""Exercise real Newznab releases against the native NNTP engine."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import hmac
import json
import os
import re
import signal
import sys
import tempfile
from pathlib import Path

import orjson

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from comet.discovery.adapters.newznab import (
    NewznabAccount,
    NewznabAdapter,
)
from comet.discovery.models import DiscoveryContext, MediaQuery
from comet.playback.manager import (
    _iter_par2_recovery,
    _native_manifest_source,
    _open_par2_proven_session_archive_source,
    _repair_archive_closure,
    _repair_direct_asset,
)
from comet.usenet.archive_passwords import (
    ARCHIVE_CREDENTIAL_FAILURES,
    resolve_archive_passphrase,
)
from comet.usenet.engine_client import (
    EngineArchiveError,
    EngineClient,
    EngineNntpError,
)
from comet.usenet.file_selection import (
    FileSelectionError,
    UsenetAsset,
    catalog_archive_members,
    catalog_engine_source_assets,
    catalog_nested_archive_members,
    eligible_video_assets,
    select_archive_volume_group,
    select_asset,
)
from comet.usenet.supervisor import EngineSupervisor


def _load_array(path: Path) -> list[dict[str, object]]:
    value = json.loads(path.read_text())
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError(f"{path} must contain a JSON array of objects")
    return value


def _indexer_account(item: dict[str, object], position: int) -> NewznabAccount:
    endpoint = str(item["endpoint"]).rstrip("/")
    api_path = str(item.get("apiPath", "/api"))
    endpoint += "/" + api_path.strip("/")
    return NewznabAccount(
        endpoint=endpoint,
        api_key=str(item["apiKey"]),
        configuration_id=f"live-{position}",
        label=str(item["name"]),
        user_agent_mode="stealth",
        max_results=int(item.get("maxResults", 30)),
        page_size=int(item.get("pageSize", 30)),
    )


def _engine_servers(items: list[dict[str, object]]) -> list[dict[str, object]]:
    return [
        {
            "provider_configuration_id": str(item["name"]),
            "host": str(item["host"]),
            "port": int(item["port"]),
            "tls_mode": str(item["tls_mode"]),
            "allow_private": False,
            "username": item.get("username"),
            "password": item.get("password"),
            "connections": int(item.get("connections", 4)),
            "pipeline": int(item.get("pipeline", 16)),
            "priority": int(item.get("priority", 0)),
            "backup": bool(item.get("backup", False)),
        }
        for item in items
    ]


def _provider_generation(partition: bytes, servers: list[dict[str, object]]) -> str:
    return hmac.digest(
        partition,
        b"comet-native-provider-set-v1\0"
        + orjson.dumps(servers, option=orjson.OPT_SORT_KEYS),
        "sha256",
    ).hex()


def _event(**fields: object) -> None:
    print(orjson.dumps(fields).decode(), flush=True)


async def _probe_direct_asset(
    engine: EngineClient,
    manifest: list[dict[str, object]],
    asset,
    *,
    artifact_sha256: str,
    servers: list[dict[str, object]],
    partition: bytes,
    generation: str,
) -> dict[str, object]:
    postings, group = _native_manifest_source(manifest, asset)
    inspection = await engine.inspect_nntp_postings(
        artifact_sha256,
        postings,
        group=group,
        servers=servers,
        account_partition=partition,
        provider_set_generation=generation,
    )
    identity, size, _revision, asset_revision = await engine.open_nntp_session(
        postings,
        group=group,
        servers=servers,
        account_partition=partition,
        provider_set_generation=generation,
        allow_degraded_playback=False,
    )
    lease = await engine.open_session_reader(identity)
    try:
        head_size = min(size, 256 * 1024)
        await engine.read_session_range(identity, lease, size, 0, head_size - 1)
        tail_size = min(size, 64 * 1024)
        await engine.read_session_range(
            identity,
            lease,
            size,
            size - tail_size,
            size - 1,
        )
    finally:
        await engine.close_session_reader(identity, lease)
    return {
        "path": asset.relative_path,
        "bytes": size,
        "inspection": inspection["inspection_state"],
        "strong_revision": asset_revision is not None,
    }


async def _probe_raw_composite(engine: EngineClient, identity: str, size: int) -> None:
    await engine.inspect_raw_composite(identity, size)
    lease = await engine.open_raw_composite_reader(identity)
    try:
        head_size = min(size, 256 * 1024)
        await engine.read_raw_composite_range(
            identity,
            lease,
            size,
            0,
            head_size - 1,
        )
        tail_size = min(size, 64 * 1024)
        await engine.read_raw_composite_range(
            identity,
            lease,
            size,
            size - tail_size,
            size - 1,
        )
    finally:
        await engine.close_raw_composite_reader(identity, lease)


async def _probe_release(
    engine: EngineClient,
    adapter: NewznabAdapter,
    candidate,
    *,
    servers: list[dict[str, object]],
    partition: bytes,
    generation: str,
    artifact_dir: Path | None,
    materialize_archives: bool,
    archive_passphrase: str | None,
) -> dict[str, object]:
    document = await adapter.grab(candidate.locators[0].remote_guid)
    return await _probe_document(
        engine,
        document,
        servers=servers,
        partition=partition,
        generation=generation,
        artifact_dir=artifact_dir,
        materialize_archives=materialize_archives,
        archive_passphrase=archive_passphrase,
        release_name=candidate.title,
    )


async def _materialize_assets(
    engine: EngineClient,
    manifest: list[dict[str, object]],
    assets,
    *,
    servers: list[dict[str, object]],
    partition: bytes,
    generation: str,
    source_failures: list[tuple[UsenetAsset, EngineNntpError]] | None = None,
) -> list[tuple[str, str, int]]:
    runtime_stats = await engine.stats()
    materialization_slots = min(
        sum(server["connections"] for server in servers),
        runtime_stats["request_workers"] - runtime_stats["requests_active"] + 1,
        runtime_stats["nntp_preparation_slots"],
    )
    gate = asyncio.Semaphore(materialization_slots)
    _event(
        stage="materialization_capacity",
        slots=materialization_slots,
        requests_active=runtime_stats["requests_active"],
        nntp_reserved_decoded_bytes=runtime_stats["nntp_reserved_decoded_bytes"],
        nntp_queue_preparation=runtime_stats["nntp_queue_preparation"],
    )
    completed = 0
    completed_bytes = 0
    progress_step = max(1, len(assets) // 10)

    async def materialize(asset):
        nonlocal completed, completed_bytes
        postings, news_group = _native_manifest_source(manifest, asset)
        try:
            async with gate:
                identity, size, _revision = await engine.materialize_nntp_postings(
                    postings,
                    group=news_group,
                    servers=servers,
                    account_partition=partition,
                    provider_set_generation=generation,
                )
        except EngineNntpError as exc:
            if source_failures is None or not exc.source_failure:
                raise
            source_failures.append((asset, exc))
            result = None
        else:
            completed_bytes += size
            result = (identity, asset.relative_path, size)
        completed += 1
        if completed == len(assets) or completed % progress_step == 0:
            _event(
                stage="materialization_progress",
                completed=completed,
                total=len(assets),
                bytes=completed_bytes,
            )
        return result

    return [
        result
        for result in await asyncio.gather(*(materialize(asset) for asset in assets))
        if result is not None
    ]


def _reused_materializations(directory: Path, assets) -> list[tuple[str, str, int]]:
    expected_sizes: dict[int, int] = {}
    for asset in assets:
        expected_sizes[asset.declared_bytes] = (
            expected_sizes.get(asset.declared_bytes, 0) + 1
        )
    materialized = []
    for path in sorted(directory.glob("*.bin")):
        identity = path.stem
        size = path.stat().st_size
        if (
            len(identity) == 64
            and all(character in "0123456789abcdef" for character in identity)
            and size > 0
            and expected_sizes.get(size, 0) > 0
        ):
            materialized.append((identity, f"reused/{path.name}", size))
            expected_sizes[size] -= 1
    if any(expected_sizes.values()):
        raise RuntimeError("reusable_materializations_incomplete")
    _event(stage="materializations_reused", files=len(materialized))
    return materialized


async def _open_materialized_archive(
    engine,
    materialized,
    plan,
    *,
    archive_passphrase: str | None,
):
    try:
        catalog_plan, members = await engine.catalog_stored_archive_volumes(
            materialized
        )
        source_assets = catalog_archive_members(plan["set_identity"], members)
        selected = select_asset(source_assets, (0,))
        _plan, identity, size, _revision = await engine.open_stored_archive_member(
            materialized,
            selected.declared_bytes,
            selected.relative_path,
        )
        await _probe_raw_composite(engine, identity, size)
    except EngineArchiveError as catalog_error:
        if catalog_error.retryable:
            raise
        catalog_plan, members = await engine.catalog_nested_archive_volumes(
            materialized,
            passphrase=archive_passphrase,
        )
        source_assets = catalog_nested_archive_members(plan["set_identity"], members)
        selected = select_asset(source_assets, (0,))
        identity, size, _revision = await engine.extract_nested_archive_volume_set(
            materialized,
            selected.declared_bytes,
            selected.selected_paths,
            passphrase=archive_passphrase,
        )
        await engine.inspect_materialization(identity, size)
    if catalog_plan != plan:
        raise RuntimeError("archive_plan_changed")
    return selected.relative_path, size


async def _probe_document(
    engine: EngineClient,
    document: bytes,
    *,
    servers: list[dict[str, object]],
    partition: bytes,
    generation: str,
    artifact_dir: Path | None,
    materialize_archives: bool,
    par2_catalog_only: bool = False,
    par2_head_map: bool = False,
    reuse_materialized: Path | None = None,
    archive_passphrase: str | None = None,
    release_name: str | None = None,
) -> dict[str, object]:
    artifact_sha256 = hashlib.sha256(document).hexdigest()
    if artifact_dir is not None:
        artifact_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        destination = artifact_dir / f"{artifact_sha256}.nzb"
        if not destination.exists():
            descriptor = os.open(
                destination,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o600,
            )
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(document)
    parsed = await engine.parse_nzb(artifact_sha256, document)
    archive_passphrase = resolve_archive_passphrase(
        (
            {"password": archive_passphrase}
            if archive_passphrase is not None
            else parsed["metadata"]
        ),
        release_name,
    )
    catalog = await engine.catalog_nntp_artifact(
        artifact_sha256,
        parsed["nm1"],
        parsed["metadata"],
        parsed["manifest"],
    )
    assets = catalog_engine_source_assets(artifact_sha256, catalog)
    if par2_catalog_only:
        par2_assets = tuple(asset for asset in assets if asset.kind == "par2")
        async for recovery in _iter_par2_recovery(
            engine,
            parsed["manifest"],
            par2_assets,
            EngineArchiveError("par2_catalog_unavailable", retryable=False),
            engine_servers=servers,
            account_partition=partition,
            provider_set_generation=generation,
        ):
            head_map = []
            if par2_head_map:
                descriptions = {}
                for item in recovery.catalog["files"]:
                    descriptions.setdefault(item["first_16k_md5"], []).append(item)
                for asset in assets:
                    if asset.kind == "par2":
                        continue
                    while True:
                        stats = await engine.stats()
                        if (
                            stats["session_prefetches_active"] == 0
                            and stats["nntp_reserved_decoded_bytes"] == 0
                        ):
                            break
                        await asyncio.sleep(0.05)
                    postings, news_group = _native_manifest_source(
                        parsed["manifest"], asset
                    )
                    (
                        identity,
                        size,
                        _revision,
                        _asset_revision,
                    ) = await engine.open_nntp_session(
                        postings,
                        group=news_group,
                        servers=servers,
                        account_partition=partition,
                        provider_set_generation=generation,
                        allow_degraded_playback=False,
                        preparation=True,
                    )
                    lease = await engine.open_session_reader(identity)
                    try:
                        head = await engine.read_session_range(
                            identity,
                            lease,
                            size,
                            0,
                            min(size, 16 * 1024) - 1,
                        )
                    finally:
                        await engine.close_session_reader(identity, lease)
                    matches = descriptions.get(
                        hashlib.md5(head, usedforsecurity=False).hexdigest(), []
                    )
                    head_map.append(
                        {
                            "source": asset.relative_path,
                            "matches": [item["relative_path"] for item in matches],
                            "head": head[:64].hex(),
                        }
                    )
            return {
                "stage": "par2_catalog",
                "set_id": recovery.catalog["set_id"],
                "slice_size": recovery.catalog["slice_size"],
                "files": [
                    [item["relative_path"], item["exact_size"]]
                    for item in recovery.catalog["files"]
                ],
                "head_map": head_map,
            }
        raise RuntimeError("par2_catalog_unavailable")
    direct = eligible_video_assets(assets)
    if direct:
        selected = select_asset(direct, (0,))
        try:
            playback = await _probe_direct_asset(
                engine,
                parsed["manifest"],
                selected,
                artifact_sha256=artifact_sha256,
                servers=servers,
                partition=partition,
                generation=generation,
            )
        except EngineNntpError as exc:
            if not exc.source_failure:
                raise
            identity, size, _revision, target_ref = await _repair_direct_asset(
                engine,
                parsed["manifest"],
                tuple(asset for asset in assets if asset.kind == "par2"),
                selected,
                exc,
                (0,),
                engine_servers=servers,
                account_partition=partition,
                provider_set_generation=generation,
            )
            playback = {
                "path": target_ref["relative_path"],
                "bytes": size,
                "inspection": "par2_repaired",
                "strong_revision": target_ref.get("strong_asset_revision") is not None,
                "identity": identity,
            }
        return {
            "stage": "playable",
            "layout": "direct",
            "nzb_files": parsed["files"],
            "segments": parsed["segments"],
            "assets": len(assets),
            **playback,
        }
    archive_assets = tuple(
        asset
        for asset in assets
        if asset.kind in {"archive", "split", "logical_split", "logical_archive"}
    )
    par2_assets = tuple(asset for asset in assets if asset.kind == "par2")
    par2_source_assets = tuple(asset for asset in assets if asset.kind == "par2_source")
    if archive_assets:
        group = select_archive_volume_group(archive_assets, (0,))
        selected_volume_ids = {asset.asset_id for asset in group.volumes}
        recovery_source_assets = par2_source_assets + tuple(
            asset
            for asset in archive_assets
            if asset.asset_id not in selected_volume_ids
        )
        # Opening every sparse volume verifies the NNTP topology and archive
        # headers without materializing the complete release.
        volumes = []
        try:
            for asset in group.volumes:
                postings, news_group = _native_manifest_source(
                    parsed["manifest"], asset
                )
                (
                    identity,
                    size,
                    revision,
                    _asset_revision,
                ) = await engine.open_nntp_session(
                    postings,
                    group=news_group,
                    servers=servers,
                    account_partition=partition,
                    provider_set_generation=generation,
                    allow_degraded_playback=False,
                    preparation=True,
                )
                volumes.append((identity, revision, asset.relative_path, size))
            plan, members = await engine.catalog_session_archive_volumes(
                volumes,
                passphrase=archive_passphrase,
            )
            if not any(member["kind"] == "video" for member in members):
                _event(
                    stage="nested_archive_detected",
                    archive_members=sum(
                        member["kind"] == "archive" for member in members
                    ),
                )
                raise EngineArchiveError(
                    "archive_direct_unsupported",
                    retryable=False,
                )
        except (EngineArchiveError, EngineNntpError) as exc:
            if (
                isinstance(exc, EngineArchiveError)
                and exc.code in ARCHIVE_CREDENTIAL_FAILURES
            ):
                raise
            if par2_assets and len(archive_assets) > len(group.volumes):
                try:
                    (
                        proven_group,
                        sparse,
                    ) = await _open_par2_proven_session_archive_source(
                        engine,
                        parsed["manifest"],
                        archive_assets,
                        par2_assets,
                        exc,
                        (0,),
                        engine_servers=servers,
                        account_partition=partition,
                        provider_set_generation=generation,
                    )
                except (EngineArchiveError, EngineNntpError, FileSelectionError):
                    pass
                else:
                    plan, _identity, size, _revision, selected = sparse
                    return {
                        "stage": "playable",
                        "layout": plan["kind"]["layout"],
                        "format": plan["kind"].get("format"),
                        "materialized": False,
                        "nzb_files": parsed["files"],
                        "segments": parsed["segments"],
                        "assets": len(assets),
                        "volumes": len(proven_group.volumes),
                        "path": selected.relative_path,
                        "bytes": size,
                    }
            if not materialize_archives:
                for identity, _revision, path, size in volumes[:2] + volumes[-1:]:
                    lease = await engine.open_session_reader(identity)
                    try:
                        head = await engine.read_session_range(
                            identity,
                            lease,
                            size,
                            0,
                            min(size, 64) - 1,
                        )
                    finally:
                        await engine.close_session_reader(identity, lease)
                    _event(stage="archive_head", path=path, head_hex=head.hex())
            if not materialize_archives or exc.retryable:
                return {
                    "stage": "fallback_required",
                    "code": exc.code,
                    "nzb_files": parsed["files"],
                    "segments": parsed["segments"],
                    "assets": len(assets),
                    "volumes": len(group.volumes),
                }

            source_failures = []
            materialized = await _materialize_assets(
                engine,
                parsed["manifest"],
                group.volumes,
                servers=servers,
                partition=partition,
                generation=generation,
                source_failures=source_failures,
            )
            layout_error = source_failures[0][1] if source_failures else None
            if layout_error is None:
                try:
                    plan = await engine.plan_archive_volumes(materialized)
                except EngineArchiveError as exc:
                    layout_error = exc
            if layout_error is not None:
                if not par2_assets:
                    raise layout_error
                recovery_sources = await _materialize_assets(
                    engine,
                    parsed["manifest"],
                    recovery_source_assets,
                    servers=servers,
                    partition=partition,
                    generation=generation,
                    source_failures=source_failures,
                )
                (
                    group,
                    materialized,
                    repair_evidence,
                    _artifacts,
                ) = await _repair_archive_closure(
                    engine,
                    parsed["manifest"],
                    par2_assets,
                    tuple(asset for asset, _exc in source_failures),
                    materialized + recovery_sources,
                    layout_error,
                    (0,),
                    engine_servers=servers,
                    account_partition=partition,
                    provider_set_generation=generation,
                )
                _event(
                    stage="par2_closure_recovered",
                    volumes=len(group.volumes),
                    repaired_files=len(repair_evidence["file_ids"])
                    if repair_evidence is not None
                    else 0,
                )
                plan = await engine.plan_archive_volumes(materialized)
            selected_path, size = await _open_materialized_archive(
                engine,
                materialized,
                plan,
                archive_passphrase=archive_passphrase,
            )
            return {
                "stage": "playable",
                "layout": plan["kind"]["layout"],
                "format": plan["kind"].get("format"),
                "materialized": True,
                "nzb_files": parsed["files"],
                "segments": parsed["segments"],
                "assets": len(assets),
                "path": selected_path,
                "bytes": size,
            }
        videos = [member for member in members if member["kind"] == "video"]
        selected = max(videos, key=lambda member: member["exact_size"])
        _plan, identity, size, _revision = await engine.open_session_archive_member(
            volumes,
            selected["exact_size"],
            selected["relative_path"],
            passphrase=archive_passphrase,
        )
        await _probe_raw_composite(engine, identity, size)
        return {
            "stage": "playable",
            "layout": plan["kind"]["layout"],
            "format": plan["kind"].get("format"),
            "nzb_files": parsed["files"],
            "segments": parsed["segments"],
            "assets": len(assets),
            "path": selected["relative_path"],
            "bytes": size,
        }
    if par2_assets and par2_source_assets:
        if not materialize_archives:
            return {
                "stage": "fallback_required",
                "code": "archive_identity_unproven",
                "nzb_files": parsed["files"],
                "segments": parsed["segments"],
                "assets": len(assets),
                "volumes": len(par2_source_assets),
            }
        source_failures = []
        materialized = (
            _reused_materializations(reuse_materialized, par2_source_assets)
            if reuse_materialized is not None
            else await _materialize_assets(
                engine,
                parsed["manifest"],
                par2_source_assets,
                servers=servers,
                partition=partition,
                generation=generation,
                source_failures=source_failures,
            )
        )
        partial_source_assets = tuple(asset for asset, _exc in source_failures)
        (
            group,
            materialized,
            repair_evidence,
            _artifacts,
        ) = await _repair_archive_closure(
            engine,
            parsed["manifest"],
            par2_assets,
            partial_source_assets,
            materialized,
            source_failures[0][1]
            if source_failures
            else EngineArchiveError("archive_identity_unproven", retryable=False),
            (0,),
            engine_servers=servers,
            account_partition=partition,
            provider_set_generation=generation,
        )
        _event(
            stage="par2_closure_recovered",
            volumes=len(group.volumes),
            repaired_files=len(repair_evidence["file_ids"])
            if repair_evidence is not None
            else 0,
        )
        plan = await engine.plan_archive_volumes(materialized)
        selected_path, size = await _open_materialized_archive(
            engine,
            materialized,
            plan,
            archive_passphrase=archive_passphrase,
        )
        return {
            "stage": "playable",
            "layout": plan["kind"]["layout"],
            "format": plan["kind"].get("format"),
            "materialized": True,
            "nzb_files": parsed["files"],
            "segments": parsed["segments"],
            "assets": len(assets),
            "path": selected_path,
            "bytes": size,
        }
    return {
        "stage": "no_media_asset",
        "nzb_files": parsed["files"],
        "segments": parsed["segments"],
        "assets": len(assets),
    }


async def _run(args: argparse.Namespace) -> None:
    indexers = _load_array(args.indexers) if args.indexers is not None else []
    raw_servers = _load_array(args.servers)
    servers = _engine_servers(raw_servers)
    partition = os.urandom(32)
    generation = _provider_generation(partition, servers)
    temporary_context = (
        contextlib.nullcontext(str(args.resume_work_dir))
        if args.resume_work_dir is not None
        else contextlib.nullcontext(
            tempfile.mkdtemp(prefix="comet-live-usenet-", dir=args.work_root)
        )
        if args.keep_work
        else tempfile.TemporaryDirectory(
            prefix="comet-live-usenet-",
            dir=args.work_root,
        )
    )
    with temporary_context as temporary:
        root = Path(temporary)
        if args.keep_work:
            _event(stage="work_directory", path=str(root))
        supervisor = EngineSupervisor(
            str(root / "run"),
            str(root / "data"),
            str(args.engine),
            artifact_dir=str(root / "artifacts"),
            memory_cache_bytes=args.memory_cache_mib * 1024 * 1024,
            disk_cache_bytes=512 * 1024 * 1024,
            minimum_free_disk_bytes=0,
            maximum_nntp_connections=args.connections,
            maximum_spool_bytes=args.spool_gib * 1024 * 1024 * 1024,
            maximum_archive_jobs=args.archive_jobs,
            maximum_repair_jobs=1,
            par2_binary=str(args.par2),
            libarchive_library=str(args.libarchive),
            log_profile="normal",
            log_format="json",
            no_color=True,
        )
        supervisor.prepare_runtime_dir()
        supervisor.next_engine_generation()
        native_log = (root / "native.log").open("wb")
        child = await asyncio.create_subprocess_exec(
            *supervisor.engine_command(),
            env=supervisor.engine_environment(),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=native_log,
            close_fds=True,
            start_new_session=True,
        )
        try:
            await supervisor.publish_descriptor(timeout=30)
            engine = EngineClient(supervisor.descriptor_path)
            _event(stage="engine_ready", health=await engine.health())
            if args.nzb:
                for nzb_path in args.nzb:
                    fields = {"nzb": nzb_path.name}
                    try:
                        result = await asyncio.wait_for(
                            _probe_document(
                                engine,
                                nzb_path.read_bytes(),
                                servers=servers,
                                partition=partition,
                                generation=generation,
                                artifact_dir=None,
                                materialize_archives=args.materialize_archives,
                                par2_catalog_only=args.par2_catalog_only,
                                par2_head_map=args.par2_head_map,
                                reuse_materialized=(
                                    root / "artifacts" / "materialized"
                                    if args.reuse_materialized
                                    else None
                                ),
                                archive_passphrase=args.archive_passphrase,
                                release_name=nzb_path.stem,
                            ),
                            timeout=args.release_timeout,
                        )
                        _event(**fields, **result)
                    except Exception as exc:
                        _event(
                            **fields,
                            stage="release_error",
                            error=type(exc).__name__,
                            code=str(exc),
                        )
                return
            queries = (
                MediaQuery(
                    "tt0332452",
                    "movie",
                    title="Troy",
                    title_aliases=("Troy",),
                    year=2004,
                ),
                MediaQuery(
                    "tt0427340",
                    "movie",
                    title="Masters of the Universe",
                    title_aliases=("Masters of the Universe",),
                    year=2026,
                ),
            )
            if args.title:
                media_type = "series" if args.season is not None else "movie"
                queries = (
                    MediaQuery(
                        args.media_id,
                        media_type,
                        season=args.season,
                        episode=args.episode,
                        title=args.title,
                        title_aliases=(args.title,),
                        year=args.year,
                    ),
                )
            elif args.media_id:
                queries = tuple(
                    query for query in queries if query.media_id == args.media_id
                )
            context = DiscoveryContext(
                frozenset({"usenet"}), account_partition=partition
            )
            for position, item in enumerate(indexers):
                account = _indexer_account(item, position)
                if args.indexer and account.label != args.indexer:
                    continue
                adapter = NewznabAdapter(None, account)
                for query in queries:
                    try:
                        batch = await adapter.search(query, context)
                        _event(
                            stage="search",
                            indexer=account.label,
                            media_id=query.media_id,
                            results=len(batch.candidates),
                        )
                    except Exception as exc:
                        _event(
                            stage="search_error",
                            indexer=account.label,
                            media_id=query.media_id,
                            error=type(exc).__name__,
                            code=str(exc),
                        )
                        continue
                    candidates = batch.candidates
                    if args.release_title:
                        pattern = re.compile(args.release_title, re.IGNORECASE)
                        candidates = tuple(
                            candidate
                            for candidate in candidates
                            if pattern.search(candidate.title)
                        )
                    selected_candidates = candidates[
                        args.skip_releases : args.skip_releases
                        + args.releases_per_query
                    ]
                    for candidate in selected_candidates:
                        await asyncio.sleep(1 / account.requests_per_second)
                        fields = {
                            "indexer": account.label,
                            "media_id": query.media_id,
                            "title": candidate.title,
                            "published_at_ms": candidate.published_at_ms,
                            "feed_bytes": candidate.size,
                        }
                        try:
                            result = await asyncio.wait_for(
                                _probe_release(
                                    engine,
                                    adapter,
                                    candidate,
                                    servers=servers,
                                    partition=partition,
                                    generation=generation,
                                    artifact_dir=args.artifact_dir,
                                    materialize_archives=args.materialize_archives,
                                    archive_passphrase=args.archive_passphrase,
                                ),
                                timeout=args.release_timeout,
                            )
                            _event(**fields, **result)
                        except Exception as exc:
                            _event(
                                **fields,
                                stage="release_error",
                                error=type(exc).__name__,
                                code=str(exc),
                            )
        finally:
            supervisor.withdraw_descriptor()
            if child.returncode is None:
                os.killpg(child.pid, signal.SIGTERM)
                try:
                    await asyncio.wait_for(child.wait(), timeout=10)
                except TimeoutError:
                    os.killpg(child.pid, signal.SIGKILL)
                    await asyncio.wait_for(child.wait(), timeout=5)
            native_log.close()
            supervisor.close()


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--indexers", type=Path)
    parser.add_argument("--servers", type=Path, required=True)
    parser.add_argument(
        "--engine",
        type=Path,
        default=ROOT / "native/usenet-engine/target/release/usenet-engine",
    )
    parser.add_argument("--par2", type=Path, default=Path("/usr/bin/par2"))
    parser.add_argument(
        "--libarchive", type=Path, default=Path("/usr/lib/libarchive.so.13")
    )
    parser.add_argument("--connections", type=int, default=25)
    parser.add_argument("--archive-jobs", type=int, default=2)
    parser.add_argument("--memory-cache-mib", type=int, default=64)
    parser.add_argument("--releases-per-query", type=int, default=4)
    parser.add_argument("--skip-releases", type=int, default=0)
    parser.add_argument("--release-timeout", type=float, default=120)
    parser.add_argument("--artifact-dir", type=Path)
    parser.add_argument("--nzb", type=Path, action="append")
    parser.add_argument("--materialize-archives", action="store_true")
    parser.add_argument("--par2-catalog-only", action="store_true")
    parser.add_argument("--par2-head-map", action="store_true")
    parser.add_argument("--work-root", type=Path)
    parser.add_argument("--keep-work", action="store_true")
    parser.add_argument("--resume-work-dir", type=Path)
    parser.add_argument("--reuse-materialized", action="store_true")
    parser.add_argument("--archive-passphrase")
    parser.add_argument("--spool-gib", type=int, default=8)
    parser.add_argument("--indexer")
    parser.add_argument("--media-id")
    parser.add_argument("--title")
    parser.add_argument("--year", type=int)
    parser.add_argument("--season", type=int)
    parser.add_argument("--episode", type=int)
    parser.add_argument("--release-title")
    arguments = parser.parse_args()
    if arguments.indexers is None and not arguments.nzb:
        parser.error("--indexers or --nzb is required")
    if arguments.reuse_materialized and arguments.resume_work_dir is None:
        parser.error("--reuse-materialized requires --resume-work-dir")
    if arguments.title and not arguments.media_id:
        parser.error("--title requires --media-id")
    if arguments.year is not None and not arguments.title:
        parser.error("--year requires --title")
    if arguments.season is not None and not arguments.title:
        parser.error("--season requires --title")
    if arguments.episode is not None and arguments.season is None:
        parser.error("--episode requires --season")
    if (
        arguments.media_id not in {None, "tt0332452", "tt0427340"}
        and not arguments.title
    ):
        parser.error("a custom --media-id requires --title")
    return arguments


if __name__ == "__main__":
    asyncio.run(_run(_arguments()))
