import asyncio
import base64
import hashlib
import hmac
import unittest
import uuid
from dataclasses import replace
from unittest.mock import ANY, AsyncMock, patch

import orjson

from comet.core.capability_states import EffectiveCapabilityState
from comet.core.sources import MAX_SIGNED_BIGINT
from comet.playback.base import Readiness
from comet.playback.manager import (
    NzbSourceError,
    _altmount_download_url,
    _archive_acquisition_plan,
    _easynews_download_url,
    _maximum_par2_recovery_blocks,
    _native_representation_signature,
    _nzbdav_download_url,
    _open_session_archive_source,
    _par2_proven_archive_group,
    _reconcile_native_representation,
    _repair_archive_closure,
    _repair_direct_asset,
    _run_published_materialization,
    _stremthru_download_url,
    _torbox_download_url,
    artifact_selection_hint,
    broker_nzb_release,
    broker_nzb_sources,
    cleanup_torbox_usenet,
    create_playback_preparation,
    poll_nzbdav,
    poll_stremthru_newz,
    poll_torbox_usenet,
    prepare_altmount,
    prepare_easynews,
    prepare_native_usenet,
    prepare_nzbdav,
    prepare_stremthru_newz,
    prepare_torbox_usenet,
    resolve_nzb_handoff_intent,
    resolve_playback_intent,
    resolve_prepared_asset,
)
from comet.playback.preparations import PlaybackPreparation
from comet.playback.provider_preparations import ProviderPreparation
from comet.playback.providers.nzbdav import NzbDavError
from comet.playback.providers.stremthru_newz import (
    StremThruGeneratedLink,
    StremThruNewzError,
)
from comet.playback.providers.torbox_usenet import (
    TorBoxDownloadTarget,
    TorBoxUsenetError,
)
from comet.playback.tokens import CapabilityCodec
from comet.usenet.engine_client import EngineArchiveError, EngineNntpError
from comet.usenet.file_selection import UsenetAsset

ROOT = base64.urlsafe_b64encode(b"a" * 32).decode().rstrip("=")


def _native_catalog_asset(
    artifact_sha256, file_index, relative_path, declared_bytes, *, kind="video"
):
    digest = hashlib.sha256()
    digest.update(b"comet-nzb-asset-v1\0")
    digest.update(bytes.fromhex(artifact_sha256))
    digest.update(file_index.to_bytes(4, "big"))
    encoded = relative_path.encode()
    digest.update(len(encoded).to_bytes(4, "big"))
    digest.update(encoded)
    return {
        "asset_id": digest.hexdigest(),
        "file_index": file_index,
        "relative_path": relative_path,
        "declared_bytes": declared_bytes,
        "kind": kind,
    }


def _archive_member(set_identity, relative_path, exact_size):
    encoded = relative_path.encode()
    digest = hashlib.sha256()
    digest.update(b"comet-archive-member-v1\0")
    digest.update(set_identity.encode())
    digest.update(len(encoded).to_bytes(4, "big"))
    digest.update(encoded)
    digest.update(exact_size.to_bytes(8, "big"))
    return {
        "member_id": digest.hexdigest(),
        "relative_path": relative_path,
        "exact_size": exact_size,
        "kind": "video",
    }


def _nested_archive_member(set_identity, *selected_paths, exact_size):
    member = _archive_member(set_identity, "!/".join(selected_paths), exact_size)
    member["selected_paths"] = list(selected_paths)
    return member


def _torbox_artifact_prepared(provider):
    preparation_id, candidate_id, provider_id, locator_id = (
        str(uuid.uuid4()) for _ in range(4)
    )
    release = type(
        "Release",
        (),
        {
            "title": "Example",
            "byte_size": 42,
            "locators": (
                {
                    "locator_id": locator_id,
                    "kind": "nzb_artifact",
                    "payload": {
                        "artifact_sha256": "a" * 64,
                    },
                },
            ),
        },
    )()
    preparation = PlaybackPreparation(
        preparation_id,
        candidate_id,
        provider_id,
        "torbox_usenet",
        (locator_id,),
        (0,),
        "stremio",
        "pending",
        None,
        None,
        1,
    )
    return type(
        "Prepared",
        (),
        {
            "preparation": preparation,
            "resolution": type(
                "Resolution",
                (),
                {
                    "provider": provider,
                    "account_partition": b"c" * 32,
                    "release": release,
                },
            )(),
        },
    )()


def _torbox_artifact_broker():
    return type(
        "Broker",
        (),
        {
            "resolve_owned_artifact": AsyncMock(
                return_value=type(
                    "Artifact",
                    (),
                    {
                        "grant_id": str(uuid.uuid4()),
                        "manifest": [{"first_segment_md5": "b" * 32}],
                    },
                )()
            )
        },
    )()


def _stremthru_pending_prepared(provider):
    preparation_id, candidate_id, provider_id, locator_id, ledger_id = (
        str(uuid.uuid4()) for _ in range(5)
    )
    preparation = PlaybackPreparation(
        preparation_id,
        candidate_id,
        provider_id,
        "stremthru_newz",
        (locator_id,),
        (0,),
        "stremio",
        "pending",
        "cloud",
        {
            "provider_preparation_id": ledger_id,
            "remote_id": "remote-1",
            "remote_hash": "hash-1",
        },
        1,
    )
    prepared = type(
        "Prepared",
        (),
        {
            "preparation": preparation,
            "resolution": type(
                "Resolution",
                (),
                {
                    "provider": provider,
                    "account_partition": b"c" * 32,
                },
            )(),
        },
    )()
    return prepared, ledger_id


class PlaybackManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_publication_operation_allows_an_explicit_reuse_only_result(self):
        preparation_id = str(uuid.uuid4())
        prepared = type(
            "Prepared",
            (),
            {
                "preparation": type(
                    "Preparation",
                    (),
                    {"preparation_id": preparation_id},
                )()
            },
        )()
        resolved = type("Resolved", (), {"nm1": "nm1:" + "a" * 64})()
        lease = type("PublicationLease", (), {"close": AsyncMock()})()
        with (
            patch(
                "comet.playback.manager.MaterializedArtifactRepository."
                "acquire_publication_lease",
                AsyncMock(return_value=lease),
            ),
            patch(
                "comet.playback.manager.MaterializedArtifactRepository."
                "register_for_preparation",
                AsyncMock(),
            ) as register,
        ):
            result = await _run_published_materialization(
                prepared,
                resolved,
                object(),
                b"a" * 32,
                AsyncMock(return_value="reused"),
                lambda _result: (),
                allow_empty=True,
            )

        self.assertEqual(result, ("reused", ()))
        register.assert_not_awaited()
        lease.close.assert_awaited_once()

    def test_brokered_selection_hint_uses_database_bigint_domain(self):
        self.assertEqual(
            artifact_selection_hint(
                {
                    "selection_hint_name": "Movie.mkv",
                    "selection_hint_size": MAX_SIGNED_BIGINT,
                }
            ),
            ("Movie.mkv", MAX_SIGNED_BIGINT),
        )

    def test_opaque_par2_sources_are_initial_archive_acquisition_inputs(self):
        sources = (
            UsenetAsset(b"a" * 32, 0, "par2-source/0000", 100, "par2_source"),
            UsenetAsset(b"b" * 32, 1, "par2-source/0001", 100, "par2_source"),
        )

        group, initial, recovery = _archive_acquisition_plan((), sources, (0,))

        self.assertIsNone(group)
        self.assertEqual(initial, sources)
        self.assertEqual(recovery, ())

    def test_par2_catalog_binds_a_fully_obfuscated_archive_set(self):
        archive_assets = (
            UsenetAsset(b"a" * 32, 0, "opaque-a.rar", 100, "archive"),
            UsenetAsset(b"b" * 32, 1, "opaque-b.rar", 80, "archive"),
        )
        files = [
            {
                "file_id": f"{index + 1:032x}",
                "relative_path": f"Movie.part{index + 1}.rar",
                "exact_size": size,
                "full_md5": f"{index + 3:032x}",
                "first_16k_md5": f"{index + 5:032x}",
                "slice_count": size // 4,
            }
            for index, size in enumerate((100, 80))
        ]
        recovery = type(
            "Recovery",
            (),
            {
                "catalog": {
                    "set_id": "f" * 32,
                    "slice_size": 4,
                    "files": files,
                }
            },
        )()

        group = _par2_proven_archive_group(archive_assets, recovery, (0,))

        self.assertEqual(group.selection_path, "movie")
        self.assertEqual(group.volumes, archive_assets)

    def test_par2_catalog_does_not_bind_a_partial_obfuscated_archive_set(self):
        archive_assets = (UsenetAsset(b"a" * 32, 0, "opaque-a.rar", 100, "archive"),)
        recovery = type(
            "Recovery",
            (),
            {
                "catalog": {
                    "set_id": "f" * 32,
                    "slice_size": 4,
                    "files": [
                        {
                            "file_id": "1" * 32,
                            "relative_path": "Movie.part1.rar",
                            "exact_size": 80,
                            "full_md5": "2" * 32,
                            "first_16k_md5": "3" * 32,
                            "slice_count": 20,
                        }
                    ],
                }
            },
        )()

        with self.assertRaisesRegex(EngineArchiveError, "par2_source_unmatched"):
            _par2_proven_archive_group(archive_assets, recovery, (0,))

    def test_native_representation_signature_binds_the_strong_revision(self):
        target = {
            "source_kind": "raw_composite",
            "raw_composite_id": "a" * 64,
            "byte_size": 42,
            "asset_revision": "b" * 64,
            "strong_asset_revision": "c" * 64,
            "selected_asset_id": "d" * 64,
            "relative_path": "Movie.mkv",
        }

        recreated = {**target, "raw_composite_id": "e" * 64}
        changed = {**target, "strong_asset_revision": "f" * 64}
        self.assertEqual(
            _native_representation_signature(target),
            _native_representation_signature(recreated),
        )
        self.assertNotEqual(
            _native_representation_signature(target),
            _native_representation_signature(changed),
        )

    def test_native_representation_signature_ignores_legacy_volume_metadata(self):
        target = {
            "source_kind": "raw_composite",
            "raw_composite_id": "a" * 64,
            "byte_size": 42,
            "asset_revision": "b" * 64,
            "selected_asset_id": "c" * 64,
            "relative_path": "Movie.mkv",
            "archive_set_identity": "d" * 64,
        }
        legacy = {
            **target,
            "archive_volume_asset_ids": [f"{index:064x}" for index in range(646)],
        }

        self.assertEqual(
            _native_representation_signature(target),
            _native_representation_signature(legacy),
        )

    def test_native_representation_reconciliation_only_promotes_revision_evidence(self):
        weak = {
            "source_kind": "session",
            "session_id": "A" * 22,
            "byte_size": 42,
            "session_revision": "b" * 64,
            "selected_asset_id": "c" * 64,
            "relative_path": "Movie.mkv",
        }
        promoted = {
            **weak,
            "session_id": "B" * 22,
            "strong_asset_revision": "d" * 64,
        }
        self.assertTrue(_reconcile_native_representation(weak, promoted))

        recreated = {**weak, "session_id": "C" * 22}
        self.assertTrue(_reconcile_native_representation(promoted, recreated))
        self.assertEqual(recreated["strong_asset_revision"], "d" * 64)

        changed = {**recreated, "strong_asset_revision": "e" * 64}
        self.assertFalse(_reconcile_native_representation(promoted, changed))
        self.assertFalse(
            _reconcile_native_representation(
                promoted,
                {**recreated, "byte_size": 43},
            )
        )

    async def test_brokerage_failure_preserves_its_safe_source_identity(self):
        source_id = "easynews-source"
        release = type(
            "Release",
            (),
            {
                "candidate_id": str(uuid.uuid4()),
                "locators": (
                    {
                        "locator_id": str(uuid.uuid4()),
                        "kind": "easynews_http",
                        "payload": {
                            "account_configuration_id": source_id,
                        },
                    },
                ),
            },
        )()
        with patch(
            "comet.playback.manager.RenderedReleaseRepository.brokered_artifacts",
            AsyncMock(return_value=()),
        ):
            with self.assertRaises(NzbSourceError) as raised:
                await broker_nzb_release(
                    release,
                    object(),
                    object(),
                    {},
                    provider_configuration_id=str(uuid.uuid4()),
                    provider_kind="nzbdav",
                    owner_configuration_partition=b"a" * 32,
                )

        self.assertEqual(raised.exception.source_configuration_id, source_id)
        self.assertEqual(raised.exception.source_kind, "easynews")
        self.assertEqual(raised.exception.code, "nzb_source_unavailable")
        self.assertEqual(raised.exception.operation, "nzb_generate")
        self.assertFalse(raised.exception.retryable)
        self.assertIsNone(raised.exception.__cause__)

    async def test_easynews_nzb_brokerage_uses_only_the_exact_source_account(self):
        candidate_id = str(uuid.uuid4())
        source_locator_id = str(uuid.uuid4())
        target_provider_id = str(uuid.uuid4())
        source_payload = {
            "account_configuration_id": "easynews-source",
            "hash": "opaque-hash",
            "filename": "Movie",
            "extension": "mkv",
            "signature": "opaque-signature",
        }
        source = {
            "locator_id": source_locator_id,
            "kind": "easynews_http",
            "payload": source_payload,
            "policy": {
                "allowed_provider_kinds": ["nzbdav"],
                "exact_provider_configuration_id": target_provider_id,
                "expires_at": None,
                "owner_configuration_partition": (b"a" * 32).hex(),
            },
        }
        release = type(
            "Release",
            (),
            {
                "candidate_id": candidate_id,
                "transport": "usenet",
                "title": "Example",
                "byte_size": 42,
                "locators": (source,),
                "media_id": "",
            },
        )()
        artifact = type(
            "Artifact",
            (),
            {
                "artifact_sha256": "b" * 64,
                "grant_id": str(uuid.uuid4()),
                "nh1": "nh1:" + "d" * 40,
                "nm1": "nm1:" + "c" * 64,
                "metadata": {},
            },
        )()
        attached = {
            "locator_id": str(uuid.uuid4()),
            "kind": "nzb_artifact",
            "payload": {
                "artifact_sha256": artifact.artifact_sha256,
                "manifest_identity": artifact.nm1,
            },
            "policy": source["policy"],
        }
        broker = type(
            "Broker",
            (),
            {
                "resolve_owned_artifact": AsyncMock(),
                "ingest_bytes": AsyncMock(return_value=artifact),
            },
        )()
        origin = type(
            "Adapter",
            (),
            {"generate_nzb": AsyncMock(return_value=b"<nzb/>")},
        )()
        unrelated = type(
            "Adapter",
            (),
            {"generate_nzb": AsyncMock(return_value=b"foreign")},
        )()

        class Lock:
            acquire = AsyncMock(return_value=True)
            release = AsyncMock()

            async def run(self, operation):
                return await operation

        with (
            patch(
                "comet.playback.manager.RenderedReleaseRepository.brokered_artifacts",
                AsyncMock(return_value=()),
            ),
            patch(
                "comet.playback.manager.RenderedReleaseRepository.attach_brokered_artifact",
                AsyncMock(return_value=attached),
            ) as attach,
            patch(
                "comet.playback.manager.DistributedLock",
                return_value=Lock(),
            ),
        ):
            transformed = await broker_nzb_release(
                release,
                broker,
                object(),
                {"easynews-source": origin, "other-source": unrelated},
                provider_configuration_id=target_provider_id,
                provider_kind="nzbdav",
                owner_configuration_partition=b"a" * 32,
            )

        self.assertEqual(transformed.locators, (attached,))
        origin.generate_nzb.assert_awaited_once_with(source_payload)
        unrelated.generate_nzb.assert_not_awaited()
        broker.ingest_bytes.assert_awaited_once_with(
            b"<nzb/>",
            owner_configuration_partition=b"a" * 32,
        )
        attach.assert_awaited_once_with(
            candidate_id,
            source_locator_id,
            artifact.artifact_sha256,
            artifact.nm1,
            owner_configuration_partition=b"a" * 32,
        )

    async def test_real_nzb_brokerage_uses_exact_origin_and_reuses_attachment(self):
        preparation_id, candidate_id, provider_id, source_locator_id = (
            str(uuid.uuid4()) for _ in range(4)
        )
        policy = {
            "allowed_provider_kinds": ["nzbdav"],
            "exact_provider_configuration_id": None,
            "expires_at": None,
            "owner_configuration_partition": (b"a" * 32).hex(),
        }
        source = {
            "locator_id": source_locator_id,
            "kind": "real_nzb",
            "payload": {
                "adapter_configuration_id": "origin",
                "remote_guid": "opaque-guid",
            },
            "policy": policy,
        }
        attached = {
            "locator_id": str(uuid.uuid4()),
            "kind": "nzb_artifact",
            "payload": {
                "artifact_sha256": "b" * 64,
                "manifest_identity": "nm1:" + "c" * 64,
            },
            "policy": policy,
        }
        provider = type(
            "Provider",
            (),
            {"descriptor": type("Descriptor", (), {"kind": "nzbdav"})()},
        )()
        intent = type("Intent", (), {})()
        release = type(
            "Release",
            (),
            {
                "candidate_id": candidate_id,
                "transport": "usenet",
                "title": "Example",
                "byte_size": 42,
                "locators": (source,),
                "media_id": "",
            },
        )()
        resolution = type(
            "Resolution",
            (),
            {
                "intent": intent,
                "provider": provider,
                "account_partition": b"c" * 32,
                "credential_fingerprint": "d" * 64,
                "provider_options": {},
                "release": release,
            },
        )()
        preparation = PlaybackPreparation(
            preparation_id,
            candidate_id,
            provider_id,
            "nzbdav",
            (source_locator_id,),
            (0,),
            "stremio",
            "pending",
            None,
            None,
            1,
        )
        prepared = type(
            "Prepared",
            (),
            {
                "resolution": resolution,
                "preparation": preparation,
                "capability": "pa2.token",
            },
        )()
        artifact = type(
            "Artifact",
            (),
            {
                "artifact_sha256": "b" * 64,
                "grant_id": str(uuid.uuid4()),
                "nh1": "nh1:" + "d" * 40,
                "nm1": "nm1:" + "c" * 64,
                "metadata": {},
            },
        )()
        broker = type(
            "Broker",
            (),
            {
                "resolve_owned_artifact": AsyncMock(return_value=artifact),
                "ingest_bytes": AsyncMock(return_value=artifact),
            },
        )()
        origin = type("Adapter", (), {"grab": AsyncMock(return_value=b"<nzb/>")})()
        unrelated = type(
            "Adapter",
            (),
            {"grab": AsyncMock(return_value=b"secret-disclosure")},
        )()

        class Lock:
            acquire = AsyncMock(return_value=True)
            release = AsyncMock()

            async def run(self, operation):
                return await operation

        with (
            patch(
                "comet.playback.manager.RenderedReleaseRepository.brokered_artifacts",
                AsyncMock(side_effect=[(), (), (attached,)]),
            ),
            patch(
                "comet.playback.manager.RenderedReleaseRepository.attach_brokered_artifact",
                AsyncMock(return_value=attached),
            ) as attach,
            patch(
                "comet.playback.manager.DistributedLock",
                return_value=Lock(),
            ),
            patch(
                "comet.playback.manager.PlaybackPreparationRepository.bind_artifact",
                AsyncMock(),
            ) as bound,
        ):
            first = await broker_nzb_sources(
                prepared,
                broker,
                object(),
                {"origin": origin, "other": unrelated},
                owner_configuration_partition=b"a" * 32,
            )
            second = await broker_nzb_sources(
                prepared,
                broker,
                object(),
                {"origin": origin, "other": unrelated},
                owner_configuration_partition=b"a" * 32,
            )

        self.assertEqual(first.resolution.release.locators, (attached,))
        self.assertEqual(second.resolution.release.locators, (attached,))
        origin.grab.assert_awaited_once_with("opaque-guid")
        unrelated.grab.assert_not_awaited()
        broker.ingest_bytes.assert_awaited_once_with(
            b"<nzb/>",
            owner_configuration_partition=b"a" * 32,
        )
        self.assertEqual(bound.await_count, 2)
        attach.assert_awaited_once_with(
            candidate_id,
            source_locator_id,
            "b" * 64,
            "nm1:" + "c" * 64,
            owner_configuration_partition=b"a" * 32,
        )

    async def test_real_nzb_brokerage_never_tries_an_unrelated_adapter(self):
        candidate_id, provider_id, source_locator_id = (
            str(uuid.uuid4()) for _ in range(3)
        )
        unrelated = type(
            "Adapter",
            (),
            {"grab": AsyncMock(return_value=b"secret-disclosure")},
        )()
        provider = type(
            "Provider",
            (),
            {"descriptor": type("Descriptor", (), {"kind": "nzbdav"})()},
        )()
        prepared = type(
            "Prepared",
            (),
            {
                "resolution": type(
                    "Resolution",
                    (),
                    {
                        "provider": provider,
                        "account_partition": b"c" * 32,
                        "release": type(
                            "Release",
                            (),
                            {
                                "candidate_id": candidate_id,
                                "locators": (
                                    {
                                        "locator_id": source_locator_id,
                                        "kind": "real_nzb",
                                        "payload": {
                                            "adapter_configuration_id": "missing",
                                            "remote_guid": "opaque-guid",
                                        },
                                    },
                                ),
                            },
                        )(),
                    },
                )(),
                "preparation": type(
                    "Preparation",
                    (),
                    {
                        "provider_configuration_id": provider_id,
                        "provider_kind": "nzbdav",
                    },
                )(),
            },
        )()
        with patch(
            "comet.playback.manager.RenderedReleaseRepository.brokered_artifacts",
            AsyncMock(return_value=()),
        ):
            with self.assertRaisesRegex(ValueError, "brokerage failed"):
                await broker_nzb_sources(
                    prepared,
                    object(),
                    object(),
                    {"other": unrelated},
                    owner_configuration_partition=b"a" * 32,
                )

        unrelated.grab.assert_not_awaited()

    async def test_session_archive_source_maps_ranges_without_materialization(self):
        revision = "b" * 64
        set_identity = "a" * 64
        member = _archive_member(set_identity, "Movie.mkv", 42)
        plan = {
            "set_identity": set_identity,
            "kind": {"layout": "single_archive", "format": "rar5"},
            "exact_size": 100,
            "volumes": [
                {
                    "content_identity": revision,
                    "relative_path": "opaque",
                    "number": 0,
                    "exact_size": 100,
                }
            ],
        }
        asset = UsenetAsset(b"a" * 32, 0, "opaque", 100, kind="archive")
        archive_group = type(
            "ArchiveGroup",
            (),
            {"volumes": (asset,), "selection_path": "Movie.mkv"},
        )()
        identity = member["member_id"]
        engine = type(
            "Engine",
            (),
            {
                "stats": AsyncMock(
                    return_value={
                        "requests_active": 1,
                        "request_workers": 32,
                        "nntp_preparation_slots": 8,
                    }
                ),
                "open_nntp_session": AsyncMock(
                    return_value=("S" * 22, 100, revision, None)
                ),
                "catalog_session_archive_volumes": AsyncMock(
                    return_value=(plan, [member])
                ),
                "open_session_archive_member": AsyncMock(
                    return_value=(plan, identity, 42, identity)
                ),
                "inspect_raw_composite": AsyncMock(),
            },
        )()
        manifest = [
            {
                "groups": ["alt.video"],
                "postings": [
                    {
                        "number": 1,
                        "bytes": 100,
                        "message_id": "archive@example.test",
                    }
                ],
            }
        ]

        result = await _open_session_archive_source(
            engine,
            manifest,
            archive_group,
            (0,),
            engine_servers=[{"connections": 4}],
            account_partition=b"a" * 32,
            provider_set_generation="c" * 64,
        )

        self.assertEqual(result[:4], (plan, identity, 42, identity))
        self.assertIs(
            engine.open_nntp_session.await_args.kwargs["preparation"],
            True,
        )
        session_volumes = [("S" * 22, revision, "opaque", 100)]
        engine.catalog_session_archive_volumes.assert_awaited_once_with(session_volumes)
        engine.open_session_archive_member.assert_awaited_once_with(
            session_volumes, 42, "Movie.mkv"
        )
        engine.inspect_raw_composite.assert_awaited_once_with(identity, 42)

    async def test_session_archive_failure_cancels_sibling_engine_requests(self):
        started = asyncio.Event()
        cancelled = asyncio.Event()
        calls = 0

        async def open_session(*_args, **_kwargs):
            nonlocal calls
            call_index = calls
            calls += 1
            if calls == 2:
                started.set()
            await started.wait()
            if call_index == 0:
                raise EngineNntpError("invalid_yenc_crc")
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        assets = tuple(
            UsenetAsset(bytes([index]) * 32, index, f"part{index}.rar", 100)
            for index in range(2)
        )
        archive_group = type(
            "ArchiveGroup",
            (),
            {"volumes": assets, "selection_path": "Movie.mkv"},
        )()
        engine = type(
            "Engine",
            (),
            {
                "stats": AsyncMock(
                    return_value={
                        "requests_active": 1,
                        "request_workers": 32,
                        "nntp_preparation_slots": 8,
                    }
                ),
                "open_nntp_session": AsyncMock(side_effect=open_session),
            },
        )()
        manifest = [
            {
                "groups": ["alt.video"],
                "postings": [
                    {
                        "number": 1,
                        "bytes": 100,
                        "message_id": f"part-{index}@example.test",
                    }
                ],
            }
            for index in range(2)
        ]

        with self.assertRaisesRegex(EngineNntpError, "invalid_yenc_crc"):
            await _open_session_archive_source(
                engine,
                manifest,
                archive_group,
                (0,),
                engine_servers=[{"connections": 2}],
                account_partition=b"a" * 32,
                provider_set_generation="c" * 64,
            )

        self.assertTrue(cancelled.is_set())

    async def test_intent_resolution_rechecks_configured_provider_and_locator_policy(
        self,
    ):
        candidate_id, provider_id, locator_id = (uuid.uuid4() for _ in range(3))
        config = {
            "schemaVersion": 2,
            "enabledTransports": ["usenet"],
            "accounts": None,
            "playbackProviders": [
                {
                    "configurationId": str(provider_id),
                    "displayName": "TorBox",
                    "kind": "torbox_usenet",
                    "enabled": True,
                    "options": {"apiKey": "key"},
                }
            ],
        }
        codec = CapabilityCodec(ROOT)
        partition = codec.configuration_partition_for_config(config)
        token = codec.encode(
            "pi2",
            partition=partition,
            suffix=[
                candidate_id.bytes,
                provider_id.bytes,
                [locator_id.bytes],
                [0],
                "stremio",
            ],
            ttl=60,
        )
        database = type("Database", (), {})()
        database.fetch_one = AsyncMock(
            return_value={
                "candidate_id": str(candidate_id),
                "media_id": "tt123",
                "transport": "usenet",
                "title": "Example",
                "byte_size": 1,
            }
        )
        database.fetch_all = AsyncMock(
            return_value=[
                {
                    "locator_id": str(locator_id),
                    "locator_kind": "nzb_artifact",
                    "locator_json": (
                        '{"artifact_sha256":"'
                        + "a" * 64
                        + '","manifest_identity":"nm1:'
                        + "b" * 64
                        + '"}'
                    ),
                    "policy_json": (
                        '{"allowed_provider_kinds":["torbox_usenet"],'
                        f'"exact_provider_configuration_id":"{provider_id}",'
                        f'"owner_configuration_partition":"{partition.hex()}","expires_at":null}}'
                    ),
                },
            ]
        )
        with (
            patch("comet.playback.manager.settings.USENET_ENABLED", True),
            patch(
                "comet.playback.manager.settings.COMET_CAPABILITY_SECRET",
                ROOT,
            ),
            patch(
                "comet.playback.manager.ensure_playback_capability_states",
                new=AsyncMock(
                    return_value={
                        str(provider_id): EffectiveCapabilityState(
                            "valid", True, False, False
                        )
                    }
                ),
            ) as preflight,
        ):
            resolved = await resolve_playback_intent(token, config, database, object())

        self.assertEqual(resolved.provider.descriptor.kind, "torbox_usenet")
        self.assertEqual(resolved.release.candidate_id, str(candidate_id))
        self.assertEqual(
            preflight.await_args.kwargs["provider_configuration_ids"],
            frozenset({str(provider_id)}),
        )

    async def test_intent_resolution_rechecks_terminal_capability_evidence(self):
        candidate_id, provider_id, locator_id = (uuid.uuid4() for _ in range(3))
        config = {
            "schemaVersion": 2,
            "enabledTransports": ["usenet"],
            "playbackProviders": [
                {
                    "configurationId": str(provider_id),
                    "displayName": "TorBox",
                    "kind": "torbox_usenet",
                    "enabled": True,
                    "options": {"apiKey": "key"},
                }
            ],
        }
        codec = CapabilityCodec(ROOT)
        token = codec.encode(
            "pi2",
            partition=codec.configuration_partition_for_config(config),
            suffix=[
                candidate_id.bytes,
                provider_id.bytes,
                [locator_id.bytes],
                [0],
                "stremio",
            ],
            ttl=60,
        )
        with (
            patch("comet.playback.manager.settings.USENET_ENABLED", True),
            patch(
                "comet.playback.manager.settings.COMET_CAPABILITY_SECRET",
                ROOT,
            ),
            patch(
                "comet.playback.manager.ensure_playback_capability_states",
                new=AsyncMock(
                    return_value={
                        str(provider_id): EffectiveCapabilityState(
                            "auth_failed",
                            False,
                            False,
                            False,
                            "api_key_rejected",
                        )
                    }
                ),
            ),
            self.assertRaisesRegex(ValueError, "provider is unavailable"),
        ):
            await resolve_playback_intent(token, config, object(), object())

    async def test_intent_resolution_rejects_a_different_stremio_client_kind(self):
        codec = CapabilityCodec(ROOT)
        config = {
            "schemaVersion": 2,
            "enabledTransports": ["usenet"],
            "playbackProviders": [],
        }
        partition = codec.configuration_partition_for_config(config)
        token = codec.encode(
            "pi2",
            partition=partition,
            suffix=[
                uuid.uuid4().bytes,
                uuid.uuid4().bytes,
                [uuid.uuid4().bytes],
                [0],
                "kodi",
            ],
            ttl=60,
        )
        with (
            patch("comet.playback.manager.settings.USENET_ENABLED", True),
            patch("comet.playback.manager.settings.COMET_CAPABILITY_SECRET", ROOT),
            self.assertRaisesRegex(ValueError, "client"),
        ):
            await resolve_playback_intent(token, config, object(), object())

    async def test_nzb_handoff_rechecks_the_exact_stremio_nntp_binding(self):
        candidate_id, provider_id, locator_id = (uuid.uuid4() for _ in range(3))
        config = {"schemaVersion": 2, "playbackProviders": []}
        codec = CapabilityCodec(ROOT)
        token = codec.encode(
            "ni2",
            partition=codec.configuration_partition_for_config(config),
            suffix=[
                candidate_id.bytes,
                provider_id.bytes,
                [locator_id.bytes],
                [0],
                "stremio",
            ],
            ttl=60,
        )
        provider = type(
            "Provider",
            (),
            {"descriptor": type("Descriptor", (), {"kind": "stremio_nntp"})()},
        )()
        resolution = type(
            "Resolution",
            (),
            {"provider": provider, "account_partition": b"c" * 32},
        )()

        with (
            patch("comet.playback.manager.settings.USENET_ENABLED", True),
            patch("comet.playback.manager.settings.COMET_CAPABILITY_SECRET", ROOT),
            patch(
                "comet.playback.manager._resolve_decoded_intent",
                AsyncMock(return_value=resolution),
            ) as resolved_intent,
        ):
            resolved = await resolve_nzb_handoff_intent(
                token,
                config,
                object(),
                object(),
            )

        self.assertIs(resolved, resolution)
        decoded = resolved_intent.await_args.args[0]
        self.assertEqual(decoded.candidate_id, str(candidate_id))
        self.assertEqual(decoded.locator_ids, (str(locator_id),))
        self.assertEqual(
            resolved_intent.await_args.kwargs["expected_client"],
            "stremio",
        )

    async def test_preparation_issues_a_reusable_pa2_capability(self):
        candidate_id, provider_id, locator_id, preparation_id = (
            str(uuid.uuid4()) for _ in range(4)
        )
        config = {
            "schemaVersion": 2,
            "enabledTransports": ["usenet"],
            "playbackProviders": [],
        }
        intent = type(
            "Intent",
            (),
            {
                "candidate_id": candidate_id,
                "provider_configuration_id": provider_id,
                "locator_ids": (locator_id,),
                "selection_intent": (0,),
                "client": "stremio",
            },
        )()
        provider = type(
            "Provider",
            (),
            {"descriptor": type("Descriptor", (), {"kind": "torbox_usenet"})()},
        )()
        resolution = type(
            "Resolution",
            (),
            {
                "intent": intent,
                "provider": provider,
                "account_partition": b"c" * 32,
            },
        )()
        preparation = PlaybackPreparation(
            preparation_id,
            candidate_id,
            provider_id,
            "torbox_usenet",
            (locator_id,),
            (0,),
            "stremio",
            "pending",
            None,
            None,
            1,
        )
        with (
            patch("comet.playback.manager.settings.USENET_ENABLED", True),
            patch("comet.playback.manager.settings.COMET_CAPABILITY_SECRET", ROOT),
            patch(
                "comet.playback.manager.resolve_playback_intent",
                AsyncMock(return_value=resolution),
            ),
            patch(
                "comet.playback.manager.PlaybackPreparationRepository.get_or_create",
                AsyncMock(return_value=preparation),
            ),
        ):
            created = await create_playback_preparation(
                "pi2.ignored", config, object(), object()
            )

        codec = CapabilityCodec(ROOT)
        partition = codec.configuration_partition_for_config(config)
        assert (
            codec.decode_prepared_asset(
                created.capability, partition=partition
            ).preparation_id
            == preparation_id
        )

    async def test_prepared_asset_rechecks_the_current_provider_graph(self):
        candidate_id, provider_id, locator_id, preparation_id = (
            str(uuid.uuid4()) for _ in range(4)
        )
        config = {
            "schemaVersion": 2,
            "enabledTransports": ["usenet"],
            "playbackProviders": [],
        }
        codec = CapabilityCodec(ROOT)
        partition = codec.configuration_partition_for_config(config)
        token = codec.encode(
            "pa2", partition=partition, suffix=[uuid.UUID(preparation_id).bytes], ttl=60
        )
        preparation = PlaybackPreparation(
            preparation_id,
            candidate_id,
            provider_id,
            "torbox_usenet",
            (locator_id,),
            (0,),
            "stremio",
            "pending",
            None,
            None,
            1,
        )
        provider = type(
            "Provider",
            (),
            {"descriptor": type("Descriptor", (), {"kind": "torbox_usenet"})()},
        )()
        resolution = type(
            "Resolution",
            (),
            {"provider": provider, "account_partition": b"c" * 32},
        )()
        with (
            patch("comet.playback.manager.settings.USENET_ENABLED", True),
            patch("comet.playback.manager.settings.COMET_CAPABILITY_SECRET", ROOT),
            patch(
                "comet.playback.manager.PlaybackPreparationRepository.resolve",
                AsyncMock(return_value=preparation),
            ),
            patch(
                "comet.playback.manager._resolve_decoded_intent",
                AsyncMock(return_value=resolution),
            ),
        ):
            resolved = await resolve_prepared_asset(token, config, object(), object())

        self.assertEqual(resolved.preparation.preparation_id, preparation_id)

    async def test_nzbdav_retries_a_definitively_rejected_initial_submission(self):
        preparation_id, candidate_id, provider_id, locator_id, grant_id, ledger_id = (
            str(uuid.uuid4()) for _ in range(6)
        )
        artifact_sha256 = "a" * 64
        remote_job_id = str(uuid.uuid4())
        broker = type(
            "Broker",
            (),
            {
                "read_owned_artifact": AsyncMock(return_value=b"<nzb/>"),
                "resolve_owned_artifact": AsyncMock(
                    return_value=type(
                        "Artifact",
                        (),
                        {"grant_id": grant_id},
                    )()
                ),
            },
        )()
        provider = type(
            "Provider",
            (),
            {
                "descriptor": type("Descriptor", (), {"kind": "nzbdav"})(),
                "submit_artifact": AsyncMock(
                    side_effect=[
                        NzbDavError(
                            "nzbdav_rate_limited",
                            mutation_rejected=True,
                        ),
                        remote_job_id,
                    ]
                ),
                "category_for": staticmethod(lambda _options, _selection: "movies"),
                "credential_binding": staticmethod(
                    lambda _options: ("https://bridge.example", b"secret")
                ),
            },
        )()
        release = type(
            "Release",
            (),
            {
                "locators": (
                    {
                        "locator_id": locator_id,
                        "kind": "nzb_artifact",
                        "payload": {"artifact_sha256": artifact_sha256},
                    },
                )
            },
        )()
        resolution = type(
            "Resolution",
            (),
            {
                "provider": provider,
                "account_partition": b"c" * 32,
                "provider_options": {"value": "safe"},
                "release": release,
            },
        )()
        preparation = PlaybackPreparation(
            preparation_id,
            candidate_id,
            provider_id,
            "nzbdav",
            (locator_id,),
            (0,),
            "stremio",
            "pending",
            None,
            None,
            1,
        )
        prepared = type(
            "Prepared", (), {"preparation": preparation, "resolution": resolution}
        )()
        with (
            patch(
                "comet.playback.manager.settings.COMET_CAPABILITY_SECRET",
                ROOT,
            ),
            patch(
                "comet.playback.manager.ProviderPreparationRepository.get_existing",
                AsyncMock(return_value=None),
            ) as get_existing,
            patch(
                "comet.playback.manager.ProviderPreparationRepository.get_or_create",
                AsyncMock(
                    return_value=(
                        ProviderPreparation(
                            ledger_id,
                            "mutation_pending",
                            {},
                            0,
                        ),
                        True,
                    )
                ),
            ),
            patch(
                "comet.playback.manager.ProviderPreparationRepository."
                "record_submission",
                AsyncMock(),
            ) as submission,
            patch(
                "comet.playback.manager.ProviderPreparationRepository."
                "discard_rejected_nzbdav_submission",
                AsyncMock(),
            ) as discarded,
            patch(
                "comet.playback.manager.PlaybackPreparationRepository."
                "record_pending_target",
                AsyncMock(),
            ) as recorded,
            patch(
                "comet.playback.manager.PlaybackPreparationRepository.mark_ready",
                AsyncMock(),
            ) as marked_ready,
        ):
            with self.assertRaisesRegex(NzbDavError, "rate_limited"):
                await prepare_nzbdav(
                    prepared,
                    broker,
                    object(),
                    owner_configuration_partition=b"a" * 32,
                )
            submitted = await prepare_nzbdav(
                prepared, broker, object(), owner_configuration_partition=b"a" * 32
            )
            get_existing.return_value = ProviderPreparation(
                ledger_id,
                "submitted",
                {
                    "remote_id": remote_job_id,
                    "remote_hash": artifact_sha256,
                    "status": "queued",
                    "ownership": "created",
                },
                0,
            )
            repeated = await prepare_nzbdav(
                prepared,
                broker,
                object(),
                owner_configuration_partition=b"a" * 32,
            )
            get_existing.return_value = ProviderPreparation(
                ledger_id,
                "terminal",
                {
                    "remote_id": remote_job_id,
                    "remote_hash": {"ignored": True},
                    "file_index": 0,
                    "file_size": 42,
                    "locked_link": "video.mkv",
                    "status": "selected",
                },
                0,
            )
            ready = await prepare_nzbdav(
                prepared,
                broker,
                object(),
                owner_configuration_partition=b"a" * 32,
            )

        self.assertEqual(submitted, "pending")
        self.assertEqual(repeated, "pending")
        self.assertEqual(ready, "ready")
        self.assertEqual(broker.read_owned_artifact.await_count, 2)
        broker.read_owned_artifact.assert_awaited_with(
            artifact_sha256,
            owner_configuration_partition=b"a" * 32,
        )
        self.assertEqual(provider.submit_artifact.await_count, 2)
        provider.submit_artifact.assert_awaited_with(
            {"value": "safe"}, b"<nzb/>", artifact_sha256, "movies"
        )
        discarded.assert_awaited_once_with(
            ledger_id,
            owner_configuration_partition=b"a" * 32,
        )
        submission.assert_awaited_once_with(
            ledger_id,
            owner_configuration_partition=b"a" * 32,
            remote_id=remote_job_id,
            remote_hash=artifact_sha256,
            status="queued",
            ownership="created",
        )
        self.assertEqual(recorded.await_count, 2)
        self.assertEqual(
            recorded.await_args.kwargs["target_ref"],
            {
                "provider_preparation_id": ledger_id,
                "remote_job_id": remote_job_id,
                "artifact_sha256": artifact_sha256,
                "category": "movies",
            },
        )
        self.assertEqual(
            marked_ready.await_args.kwargs["target_ref"],
            {
                "provider_preparation_id": ledger_id,
                "relative_path": "video.mkv",
                "byte_size": 42,
                "verified_name": "comet-" + artifact_sha256,
                "category": "movies",
            },
        )

    async def test_torbox_create_receives_one_account_scoped_governor(self):
        preparation_id, candidate_id, provider_id, locator_id, grant_id, ledger_id = (
            str(uuid.uuid4()) for _ in range(6)
        )
        artifact_sha256 = "a" * 64
        broker = type(
            "Broker",
            (),
            {
                "read_owned_artifact": AsyncMock(return_value=b"<nzb/>"),
                "resolve_owned_artifact": AsyncMock(
                    return_value=type(
                        "Artifact",
                        (),
                        {
                            "grant_id": grant_id,
                            "manifest": [
                                {
                                    "first_segment_md5": "b" * 32,
                                }
                            ],
                        },
                    )()
                ),
            },
        )()
        item = type(
            "Item",
            (),
            {
                "usenet_id": 17,
                "status": "queued",
            },
        )()
        provider = type(
            "Provider",
            (),
            {
                "descriptor": type(
                    "Descriptor",
                    (),
                    {"kind": "torbox_usenet"},
                )(),
                "credential_binding": staticmethod(
                    lambda: ("https://api.torbox.app/v1/api/usenet", b"secret")
                ),
                "submit_artifact": AsyncMock(return_value=item),
                "find_existing": AsyncMock(return_value=None),
                "status": staticmethod(
                    lambda _item: type("Status", (), {"readiness": Readiness.UNKNOWN})()
                ),
            },
        )()
        release = type(
            "Release",
            (),
            {
                "title": "Dune.Part.Two.2024.1080p.BluRay",
                "byte_size": 42,
                "locators": (
                    {
                        "locator_id": locator_id,
                        "kind": "nzb_artifact",
                        "payload": {
                            "artifact_sha256": artifact_sha256,
                        },
                    },
                ),
            },
        )()
        resolution = type(
            "Resolution",
            (),
            {"provider": provider, "account_partition": b"c" * 32, "release": release},
        )()
        preparation = PlaybackPreparation(
            preparation_id,
            candidate_id,
            provider_id,
            "torbox_usenet",
            (locator_id,),
            (0,),
            "stremio",
            "pending",
            None,
            None,
            1,
        )
        prepared = type(
            "Prepared",
            (),
            {"preparation": preparation, "resolution": resolution},
        )()
        governor = type(
            "Governor",
            (),
            {"acquire_window": AsyncMock(return_value=object())},
        )()
        database = object()
        with (
            patch(
                "comet.playback.manager.ProviderGovernor",
                return_value=governor,
            ),
            patch(
                "comet.playback.manager.PlaybackPreparationRepository."
                "record_pending_target",
                AsyncMock(),
            ) as recorded,
            patch(
                "comet.playback.manager.ProviderPreparationRepository."
                "claim_torbox_cleanup",
                AsyncMock(return_value=None),
            ),
            patch(
                "comet.playback.manager.ProviderPreparationRepository.get_existing",
                AsyncMock(return_value=None),
            ),
            patch(
                "comet.playback.manager.ProviderPreparationRepository.get_or_create",
                AsyncMock(
                    return_value=(
                        type(
                            "Ledger",
                            (),
                            {
                                "preparation_id": ledger_id,
                                "state": "mutation_pending",
                                "payload": {},
                            },
                        )(),
                        True,
                    )
                ),
            ) as get_ledger,
            patch(
                "comet.playback.manager.ProviderPreparationRepository."
                "record_submission",
                AsyncMock(),
            ) as submitted,
            patch(
                "comet.playback.manager.settings.COMET_CAPABILITY_SECRET",
                ROOT,
            ),
        ):
            result = await prepare_torbox_usenet(
                prepared,
                broker,
                database,
                owner_configuration_partition=b"a" * 32,
            )

        self.assertEqual(result, "pending")
        account_scope = bytes.fromhex(
            CapabilityCodec(ROOT).provider_credential_fingerprint(
                "torbox_usenet",
                "https://api.torbox.app/v1/api/usenet",
                b"secret",
            )
        )
        provider.submit_artifact.assert_awaited_once_with(
            b"<nzb/>",
            name="Dune.Part.Two.2024.1080p.BluRay",
            governor=governor,
            governor_scope=account_scope,
        )
        provider.find_existing.assert_awaited_once_with(("b" * 32,))
        self.assertEqual(
            get_ledger.await_args.kwargs["provider_kind"],
            "torbox_usenet",
        )
        self.assertEqual(
            get_ledger.await_args.kwargs["artifact_grant_id"],
            grant_id,
        )
        submitted.assert_awaited_once_with(
            ledger_id,
            owner_configuration_partition=b"a" * 32,
            remote_id="17",
            remote_hash=artifact_sha256,
            status="queued",
            ownership="created",
        )
        self.assertEqual(
            recorded.await_args.kwargs["target_ref"],
            {
                "provider_preparation_id": ledger_id,
                "usenet_id": 17,
            },
        )

    async def test_torbox_submitted_ledger_skips_remote_discovery_and_cleanup(self):
        provider = type(
            "Provider",
            (),
            {
                "descriptor": type("Descriptor", (), {"kind": "torbox_usenet"})(),
                "credential_binding": staticmethod(
                    lambda: (
                        "https://api.torbox.app/v1/api/usenet",
                        b"secret",
                    )
                ),
                "find_existing": AsyncMock(),
            },
        )()
        prepared = _torbox_artifact_prepared(provider)
        ledger = ProviderPreparation(
            str(uuid.uuid4()),
            "submitted",
            {
                "remote_id": "17",
                "remote_hash": "a" * 64,
                "status": "queued",
                "ownership": "created",
            },
            0,
        )
        with (
            patch(
                "comet.playback.manager.settings.COMET_CAPABILITY_SECRET",
                ROOT,
            ),
            patch(
                "comet.playback.manager.ProviderPreparationRepository.get_existing",
                AsyncMock(return_value=ledger),
            ),
            patch(
                "comet.playback.manager.ProviderPreparationRepository.get_or_create",
                AsyncMock(),
            ) as created,
            patch(
                "comet.playback.manager.ProviderPreparationRepository."
                "claim_torbox_cleanup",
                AsyncMock(),
            ) as cleanup,
            patch(
                "comet.playback.manager.PlaybackPreparationRepository."
                "record_pending_target",
                AsyncMock(),
            ) as recorded,
        ):
            result = await prepare_torbox_usenet(
                prepared,
                _torbox_artifact_broker(),
                object(),
                owner_configuration_partition=b"a" * 32,
            )

        self.assertEqual(result, "pending")
        provider.find_existing.assert_not_awaited()
        cleanup.assert_not_awaited()
        created.assert_not_awaited()
        self.assertEqual(
            recorded.await_args.kwargs["target_ref"]["usenet_id"],
            17,
        )

    async def test_torbox_library_failure_aborts_before_persisting_mutation(self):
        provider = type(
            "Provider",
            (),
            {
                "descriptor": type("Descriptor", (), {"kind": "torbox_usenet"})(),
                "credential_binding": staticmethod(
                    lambda: (
                        "https://api.torbox.app/v1/api/usenet",
                        b"secret",
                    )
                ),
                "find_existing": AsyncMock(
                    side_effect=TorBoxUsenetError("torbox_unavailable")
                ),
            },
        )()
        prepared = _torbox_artifact_prepared(provider)
        with (
            patch(
                "comet.playback.manager.settings.COMET_CAPABILITY_SECRET",
                ROOT,
            ),
            patch(
                "comet.playback.manager.ProviderPreparationRepository.get_existing",
                AsyncMock(return_value=None),
            ),
            patch(
                "comet.playback.manager.ProviderPreparationRepository.get_or_create",
                AsyncMock(),
            ) as created,
            patch(
                "comet.playback.manager.ProviderPreparationRepository."
                "claim_torbox_cleanup",
                AsyncMock(return_value=None),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "torbox_unavailable"):
                await prepare_torbox_usenet(
                    prepared,
                    _torbox_artifact_broker(),
                    object(),
                    owner_configuration_partition=b"a" * 32,
                )

        created.assert_not_awaited()

    async def test_torbox_inflight_submission_stays_pending_during_reconciliation(self):
        provider = type(
            "Provider",
            (),
            {
                "descriptor": type("Descriptor", (), {"kind": "torbox_usenet"})(),
                "credential_binding": staticmethod(
                    lambda: (
                        "https://api.torbox.app/v1/api/usenet",
                        b"secret",
                    )
                ),
                "find_existing": AsyncMock(return_value=None),
                "submit_artifact": AsyncMock(),
            },
        )()
        prepared = _torbox_artifact_prepared(provider)
        ledger = ProviderPreparation(
            str(uuid.uuid4()),
            "mutation_pending",
            {},
            990,
        )
        with (
            patch("comet.playback.manager.time.time", return_value=1_000),
            patch(
                "comet.playback.manager.settings.COMET_CAPABILITY_SECRET",
                ROOT,
            ),
            patch(
                "comet.playback.manager.ProviderPreparationRepository.get_existing",
                AsyncMock(return_value=ledger),
            ),
            patch(
                "comet.playback.manager.ProviderPreparationRepository.get_or_create",
                AsyncMock(),
            ) as created,
            patch(
                "comet.playback.manager.ProviderPreparationRepository."
                "claim_torbox_cleanup",
                AsyncMock(return_value=None),
            ),
            patch(
                "comet.playback.manager.ProviderPreparationRepository."
                "record_ambiguous_submission",
                AsyncMock(),
            ) as ambiguous,
            patch(
                "comet.playback.manager.PlaybackPreparationRepository.mark_failed",
                AsyncMock(),
            ) as failed,
        ):
            result = await prepare_torbox_usenet(
                prepared,
                _torbox_artifact_broker(),
                object(),
                owner_configuration_partition=b"a" * 32,
            )

        self.assertEqual(result, "pending")
        created.assert_not_awaited()
        provider.submit_artifact.assert_not_awaited()
        ambiguous.assert_not_awaited()
        failed.assert_not_awaited()

    async def test_torbox_cleanup_uses_only_the_claimed_ledger_target(self):
        provider = type(
            "Provider",
            (),
            {"delete_owned": AsyncMock()},
        )()
        cleanup_target = type(
            "CleanupTarget",
            (),
            {
                "preparation_id": str(uuid.uuid4()),
                "usenet_id": 17,
            },
        )()
        with (
            patch(
                "comet.playback.manager.ProviderPreparationRepository."
                "claim_torbox_cleanup",
                AsyncMock(return_value=cleanup_target),
            ) as claimed,
            patch(
                "comet.playback.manager.ProviderPreparationRepository."
                "record_torbox_cleanup_complete",
                AsyncMock(),
            ) as completed,
        ):
            result = await cleanup_torbox_usenet(
                provider,
                object(),
                owner_configuration_partition=b"a" * 32,
                provider_configuration_id=str(uuid.uuid4()),
                credential_fingerprint="b" * 64,
            )

        self.assertTrue(result)
        claimed.assert_awaited_once()
        provider.delete_owned.assert_awaited_once_with(17)
        completed.assert_awaited_once_with(
            cleanup_target.preparation_id,
            owner_configuration_partition=b"a" * 32,
            usenet_id=17,
        )

    async def test_torbox_reuses_an_exact_account_library_item(self):
        preparation_id, candidate_id, provider_id, locator_id, grant_id, ledger_id = (
            str(uuid.uuid4()) for _ in range(6)
        )
        artifact_sha256 = "a" * 64
        existing = type(
            "Item",
            (),
            {
                "usenet_id": 23,
                "status": "downloading",
            },
        )()
        broker = type(
            "Broker",
            (),
            {
                "read_owned_artifact": AsyncMock(return_value=b"<nzb/>"),
                "resolve_owned_artifact": AsyncMock(
                    return_value=type(
                        "Artifact",
                        (),
                        {
                            "grant_id": grant_id,
                            "manifest": [
                                {
                                    "first_segment_md5": "b" * 32,
                                }
                            ],
                        },
                    )()
                ),
            },
        )()
        provider = type(
            "Provider",
            (),
            {
                "descriptor": type(
                    "Descriptor",
                    (),
                    {"kind": "torbox_usenet"},
                )(),
                "credential_binding": staticmethod(
                    lambda: ("https://api.torbox.app/v1/api/usenet", b"secret")
                ),
                "submit_artifact": AsyncMock(),
                "find_existing": AsyncMock(return_value=existing),
                "status": staticmethod(
                    lambda _item: type(
                        "Status", (), {"readiness": Readiness.PREPARING}
                    )()
                ),
            },
        )()
        release = type(
            "Release",
            (),
            {
                "title": "Exact.Release.Name",
                "byte_size": 42,
                "locators": (
                    {
                        "locator_id": locator_id,
                        "kind": "nzb_artifact",
                        "payload": {
                            "artifact_sha256": artifact_sha256,
                        },
                    },
                ),
            },
        )()
        preparation = PlaybackPreparation(
            preparation_id,
            candidate_id,
            provider_id,
            "torbox_usenet",
            (locator_id,),
            (0,),
            "stremio",
            "pending",
            None,
            None,
            1,
        )
        prepared = type(
            "Prepared",
            (),
            {
                "preparation": preparation,
                "resolution": type(
                    "Resolution",
                    (),
                    {
                        "provider": provider,
                        "account_partition": b"c" * 32,
                        "release": release,
                    },
                )(),
            },
        )()
        with (
            patch(
                "comet.playback.manager.PlaybackPreparationRepository."
                "record_pending_target",
                AsyncMock(),
            ) as recorded,
            patch(
                "comet.playback.manager.ProviderPreparationRepository."
                "claim_torbox_cleanup",
                AsyncMock(return_value=None),
            ),
            patch(
                "comet.playback.manager.ProviderPreparationRepository.get_existing",
                AsyncMock(return_value=None),
            ),
            patch(
                "comet.playback.manager.ProviderPreparationRepository.get_or_create",
                AsyncMock(
                    return_value=(
                        type(
                            "Ledger",
                            (),
                            {
                                "preparation_id": ledger_id,
                                "state": "mutation_pending",
                                "payload": {},
                            },
                        )(),
                        True,
                    )
                ),
            ),
            patch(
                "comet.playback.manager.ProviderPreparationRepository."
                "record_submission",
                AsyncMock(),
            ) as submitted,
            patch(
                "comet.playback.manager.settings.COMET_CAPABILITY_SECRET",
                ROOT,
            ),
        ):
            result = await prepare_torbox_usenet(
                prepared,
                broker,
                object(),
                owner_configuration_partition=b"a" * 32,
            )

        self.assertEqual(result, "pending")
        provider.submit_artifact.assert_not_awaited()
        provider.find_existing.assert_awaited_once_with(("b" * 32,))
        submitted.assert_awaited_once_with(
            ledger_id,
            owner_configuration_partition=b"a" * 32,
            remote_id="23",
            remote_hash=artifact_sha256,
            status="downloading",
            ownership="adopted",
        )
        self.assertEqual(
            recorded.await_args.kwargs["target_ref"],
            {
                "provider_preparation_id": ledger_id,
                "usenet_id": 23,
            },
        )

    async def test_stremthru_reuses_an_exact_selected_remote_job(self):
        preparation_id, candidate_id, provider_id, locator_id, grant_id, ledger_id = (
            str(uuid.uuid4()) for _ in range(6)
        )
        artifact_sha256 = "a" * 64
        provider = type(
            "Provider",
            (),
            {
                "descriptor": type(
                    "Descriptor",
                    (),
                    {"kind": "stremthru_newz"},
                )(),
                "credential_binding": staticmethod(
                    lambda: ("https://stremthru.example", b"secret")
                ),
                "submit_export": AsyncMock(),
            },
        )()
        release = type(
            "Release",
            (),
            {
                "locators": (
                    {
                        "locator_id": locator_id,
                        "kind": "nzb_artifact",
                        "payload": {"artifact_sha256": artifact_sha256},
                    },
                )
            },
        )()
        resolution = type(
            "Resolution",
            (),
            {"provider": provider, "account_partition": b"c" * 32, "release": release},
        )()
        preparation = PlaybackPreparation(
            preparation_id,
            candidate_id,
            provider_id,
            "stremthru_newz",
            (locator_id,),
            (0,),
            "stremio",
            "pending",
            None,
            None,
            1,
        )
        prepared = type(
            "Prepared",
            (),
            {"preparation": preparation, "resolution": resolution},
        )()
        broker = type(
            "Broker",
            (),
            {
                "resolve_owned_artifact": AsyncMock(
                    return_value=type("Artifact", (), {"grant_id": grant_id})()
                )
            },
        )()
        ledger = ProviderPreparation(
            ledger_id,
            "terminal",
            {
                "status": "selected",
                "remote_id": "remote-1",
                "remote_hash": "hash-1",
                "file_index": 3,
                "file_size": 42,
                "locked_link": "locked-ref",
            },
            0,
        )
        with (
            patch(
                "comet.playback.manager.settings.COMET_CAPABILITY_SECRET",
                ROOT,
            ),
            patch(
                "comet.playback.manager.ProviderPreparationRepository.get_existing",
                AsyncMock(return_value=ledger),
            ) as get_existing,
            patch(
                "comet.playback.manager.ProviderPreparationRepository.get_or_create",
                AsyncMock(),
            ) as create_ledger,
            patch(
                "comet.playback.manager.PlaybackPreparationRepository.mark_ready",
                AsyncMock(),
            ) as mark_ready,
            patch(
                "comet.playback.manager.NzbProviderExportRepository.get_or_create",
                AsyncMock(),
            ) as create_export,
            patch(
                "comet.playback.manager.PlaybackPreparationRepository."
                "record_pending_target",
                AsyncMock(),
            ) as pending_target,
        ):
            result = await prepare_stremthru_newz(
                prepared,
                broker,
                object(),
                owner_configuration_partition=b"a" * 32,
            )
            get_existing.return_value = ProviderPreparation(
                ledger_id,
                "submitted",
                {
                    "status": "processing",
                    "remote_id": "remote-1",
                    "remote_hash": "hash-1",
                    "ownership": "created",
                },
                0,
            )
            submitted_result = await prepare_stremthru_newz(
                prepared,
                broker,
                object(),
                owner_configuration_partition=b"a" * 32,
            )

        self.assertEqual(result, "ready")
        self.assertEqual(submitted_result, "pending")
        provider.submit_export.assert_not_awaited()
        create_export.assert_not_awaited()
        create_ledger.assert_not_awaited()
        pending_target.assert_awaited_once()
        mark_ready.assert_awaited_once_with(
            preparation_id,
            owner_configuration_partition=b"a" * 32,
            provider_account_partition=b"c" * 32,
            target_kind="cloud",
            target_ref={
                "provider_preparation_id": ledger_id,
                "byte_size": 42,
                "locked_link": "locked-ref",
            },
        )

    async def test_stremthru_readds_one_tombstoned_job_with_the_same_export(self):
        preparation_id, candidate_id, provider_id, locator_id, grant_id, ledger_id = (
            str(uuid.uuid4()) for _ in range(6)
        )
        artifact_sha256 = "a" * 64
        submission = type(
            "Submission",
            (),
            {
                "remote_id": "remote-2",
                "remote_hash": "hash-2",
                "status": "processing",
            },
        )()
        provider = type(
            "Provider",
            (),
            {
                "descriptor": type(
                    "Descriptor",
                    (),
                    {"kind": "stremthru_newz"},
                )(),
                "credential_binding": staticmethod(
                    lambda: ("https://stremthru.example", b"secret")
                ),
                "submit_export": AsyncMock(return_value=submission),
            },
        )()
        release = type(
            "Release",
            (),
            {
                "locators": (
                    {
                        "locator_id": locator_id,
                        "kind": "nzb_artifact",
                        "payload": {"artifact_sha256": artifact_sha256},
                    },
                )
            },
        )()
        preparation = PlaybackPreparation(
            preparation_id,
            candidate_id,
            provider_id,
            "stremthru_newz",
            (locator_id,),
            (0,),
            "stremio",
            "pending",
            None,
            None,
            1,
        )
        prepared = type(
            "Prepared",
            (),
            {
                "preparation": preparation,
                "resolution": type(
                    "Resolution",
                    (),
                    {
                        "provider": provider,
                        "account_partition": b"c" * 32,
                        "release": release,
                    },
                )(),
            },
        )()
        broker = type(
            "Broker",
            (),
            {
                "resolve_owned_artifact": AsyncMock(
                    return_value=type("Artifact", (), {"grant_id": grant_id})()
                )
            },
        )()
        ledger = ProviderPreparation(
            ledger_id,
            "submitted",
            {
                "status": "remote_missing",
                "ownership": "created",
                "missing_count": 1,
            },
            0,
        )
        with (
            patch(
                "comet.playback.manager.settings.COMET_CAPABILITY_SECRET",
                ROOT,
            ),
            patch(
                "comet.playback.manager.ProviderPreparationRepository.get_existing",
                AsyncMock(return_value=ledger),
            ),
            patch(
                "comet.playback.manager.ProviderPreparationRepository."
                "begin_stremthru_resubmission",
                AsyncMock(return_value=True),
            ) as begin,
            patch(
                "comet.playback.manager.ProviderPreparationRepository."
                "record_stremthru_resubmission",
                AsyncMock(),
            ) as record_resubmission,
            patch(
                "comet.playback.manager.ProviderPreparationRepository."
                "restore_rejected_stremthru_resubmission",
                AsyncMock(),
            ) as restore_rejected,
            patch(
                "comet.playback.manager.NzbProviderExportRepository.get_or_create",
                AsyncMock(return_value="nx1." + "b" * 32),
            ),
            patch(
                "comet.playback.manager.export_base_url",
                return_value="https://comet.example",
            ),
            patch(
                "comet.playback.manager.PlaybackPreparationRepository."
                "record_pending_target",
                AsyncMock(),
            ) as pending_target,
        ):
            result = await prepare_stremthru_newz(
                prepared,
                broker,
                object(),
                owner_configuration_partition=b"a" * 32,
            )
            provider.submit_export.side_effect = StremThruNewzError(
                "stremthru_busy",
                retryable=True,
                mutation_rejected=True,
            )
            with self.assertRaisesRegex(StremThruNewzError, "stremthru_busy"):
                await prepare_stremthru_newz(
                    prepared,
                    broker,
                    object(),
                    owner_configuration_partition=b"a" * 32,
                )

        self.assertEqual(result, "pending")
        self.assertEqual(begin.await_count, 2)
        begin.assert_awaited_with(
            ledger_id,
            owner_configuration_partition=b"a" * 32,
        )
        provider.submit_export.assert_awaited_with(
            "https://comet.example/nzb/export/v1/nx1." + "b" * 32 + ".nzb"
        )
        record_resubmission.assert_awaited_once_with(
            ledger_id,
            owner_configuration_partition=b"a" * 32,
            remote_id="remote-2",
            remote_hash="hash-2",
            status="processing",
        )
        pending_target.assert_awaited_once_with(
            preparation_id,
            owner_configuration_partition=b"a" * 32,
            provider_account_partition=b"c" * 32,
            target_kind="cloud",
            target_ref={
                "provider_preparation_id": ledger_id,
                "remote_id": "remote-2",
                "remote_hash": "hash-2",
            },
        )
        restore_rejected.assert_awaited_once_with(
            ledger_id,
            owner_configuration_partition=b"a" * 32,
        )

    async def test_stremthru_missing_item_clears_only_the_first_remote_target(self):
        preparation_id, candidate_id, provider_id, locator_id, ledger_id = (
            str(uuid.uuid4()) for _ in range(5)
        )
        provider = type(
            "Provider",
            (),
            {
                "descriptor": type(
                    "Descriptor",
                    (),
                    {"kind": "stremthru_newz"},
                )(),
                "get_item": AsyncMock(
                    side_effect=StremThruNewzError(
                        "remote_item_missing",
                        remote_missing=True,
                    )
                ),
            },
        )()
        preparation = PlaybackPreparation(
            preparation_id,
            candidate_id,
            provider_id,
            "stremthru_newz",
            (locator_id,),
            (0,),
            "stremio",
            "pending",
            "cloud",
            {
                "provider_preparation_id": ledger_id,
                "remote_id": "remote-1",
                "remote_hash": "hash-1",
            },
            1,
        )
        prepared = type(
            "Prepared",
            (),
            {
                "preparation": preparation,
                "resolution": type(
                    "Resolution",
                    (),
                    {
                        "provider": provider,
                        "account_partition": b"c" * 32,
                    },
                )(),
            },
        )()
        with (
            patch(
                "comet.playback.manager.ProviderPreparationRepository."
                "record_stremthru_missing",
                AsyncMock(return_value=True),
            ) as record_missing,
            patch(
                "comet.playback.manager.PlaybackPreparationRepository."
                "clear_pending_target",
                AsyncMock(),
            ) as clear_target,
            patch(
                "comet.playback.manager.PlaybackPreparationRepository.mark_failed",
                AsyncMock(),
            ) as mark_failed,
        ):
            result = await poll_stremthru_newz(
                prepared,
                object(),
                owner_configuration_partition=b"a" * 32,
            )

        self.assertEqual(result, "reprepare")
        record_missing.assert_awaited_once_with(
            ledger_id,
            owner_configuration_partition=b"a" * 32,
        )
        clear_target.assert_awaited_once_with(
            preparation_id,
            owner_configuration_partition=b"a" * 32,
            provider_account_partition=b"c" * 32,
        )
        mark_failed.assert_not_awaited()

    async def test_stremthru_second_missing_item_is_terminal(self):
        preparation_id, candidate_id, provider_id, locator_id, ledger_id = (
            str(uuid.uuid4()) for _ in range(5)
        )
        provider = type(
            "Provider",
            (),
            {
                "descriptor": type(
                    "Descriptor",
                    (),
                    {"kind": "stremthru_newz"},
                )(),
                "get_item": AsyncMock(
                    side_effect=StremThruNewzError(
                        "remote_item_missing",
                        remote_missing=True,
                    )
                ),
            },
        )()
        preparation = PlaybackPreparation(
            preparation_id,
            candidate_id,
            provider_id,
            "stremthru_newz",
            (locator_id,),
            (0,),
            "stremio",
            "pending",
            "cloud",
            {
                "provider_preparation_id": ledger_id,
                "remote_id": "remote-2",
                "remote_hash": "hash-2",
            },
            1,
        )
        prepared = type(
            "Prepared",
            (),
            {
                "preparation": preparation,
                "resolution": type(
                    "Resolution",
                    (),
                    {
                        "provider": provider,
                        "account_partition": b"c" * 32,
                    },
                )(),
            },
        )()
        with (
            patch(
                "comet.playback.manager.ProviderPreparationRepository."
                "record_stremthru_missing",
                AsyncMock(return_value=False),
            ),
            patch(
                "comet.playback.manager.PlaybackPreparationRepository."
                "clear_pending_target",
                AsyncMock(),
            ) as clear_target,
            patch(
                "comet.playback.manager.PlaybackPreparationRepository.mark_failed",
                AsyncMock(),
            ) as mark_failed,
        ):
            result = await poll_stremthru_newz(
                prepared,
                object(),
                owner_configuration_partition=b"a" * 32,
            )

        self.assertEqual(result, "failed")
        clear_target.assert_not_awaited()
        mark_failed.assert_awaited_once_with(
            preparation_id,
            owner_configuration_partition=b"a" * 32,
            provider_account_partition=b"c" * 32,
            code="remote_item_missing",
        )

    async def test_stremthru_poll_seals_remote_and_selection_failures(self):
        cases = (
            ("FAILED", False, True, None, "failed", "remote_failed"),
            (
                "provider-ready-v2",
                True,
                False,
                StremThruNewzError(
                    "stremthru_file_selection_ambiguous",
                    terminal=True,
                ),
                "file_selection_ambiguous",
                "file_selection_ambiguous",
            ),
        )
        for (
            item_status,
            ready,
            terminal,
            selection_error,
            terminal_status,
            failure_code,
        ) in cases:
            with self.subTest(item_status=item_status):
                provider = type(
                    "Provider",
                    (),
                    {
                        "descriptor": type(
                            "Descriptor", (), {"kind": "stremthru_newz"}
                        )(),
                        "get_item": AsyncMock(
                            return_value=type(
                                "Item",
                                (),
                                {
                                    "status": item_status,
                                    "files": (object(),) if ready else (),
                                    "terminal": terminal,
                                },
                            )()
                        ),
                        "select_file": (
                            AsyncMock()
                            if selection_error is None
                            else lambda *_args, selection_error=selection_error: (
                                _ for _ in ()
                            ).throw(selection_error)
                        ),
                    },
                )()
                prepared, ledger_id = _stremthru_pending_prepared(provider)
                with (
                    patch(
                        "comet.playback.manager.ProviderPreparationRepository."
                        "record_poll",
                        AsyncMock(),
                    ),
                    patch(
                        "comet.playback.manager.ProviderPreparationRepository."
                        "record_terminal_status",
                        AsyncMock(),
                    ) as sealed,
                    patch(
                        "comet.playback.manager.PlaybackPreparationRepository."
                        "mark_failed",
                        AsyncMock(),
                    ) as failed,
                ):
                    result = await poll_stremthru_newz(
                        prepared,
                        object(),
                        owner_configuration_partition=b"a" * 32,
                    )

                self.assertEqual(result, "failed")
                sealed.assert_awaited_once_with(
                    ledger_id,
                    owner_configuration_partition=b"a" * 32,
                    status=terminal_status,
                )
                failed.assert_awaited_once_with(
                    prepared.preparation.preparation_id,
                    owner_configuration_partition=b"a" * 32,
                    provider_account_partition=b"c" * 32,
                    code=failure_code,
                )

    async def test_stremthru_resolves_the_selected_signed_download_url(self):
        preparation_id, candidate_id, provider_id, locator_id = (
            str(uuid.uuid4()) for _ in range(4)
        )
        provider = type(
            "Provider",
            (),
            {
                "descriptor": type(
                    "Descriptor",
                    (),
                    {"kind": "stremthru_newz"},
                )(),
                "generate_link": AsyncMock(
                    return_value=StremThruGeneratedLink(
                        "https://cdn.example/media",
                    )
                ),
            },
        )()
        preparation = PlaybackPreparation(
            preparation_id,
            candidate_id,
            provider_id,
            "stremthru_newz",
            (locator_id,),
            (0,),
            "stremio",
            "ready",
            "cloud",
            {
                "provider_preparation_id": str(uuid.uuid4()),
                "file_index": 3,
                "byte_size": 42,
                "locked_link": "locked-ref",
            },
            1,
        )
        prepared = type(
            "Prepared",
            (),
            {
                "preparation": preparation,
                "resolution": type(
                    "Resolution",
                    (),
                    {
                        "provider": provider,
                        "account_partition": b"c" * 32,
                        "release": type("Release", (), {"title": "Example"})(),
                    },
                )(),
            },
        )()

        self.assertEqual(
            await _stremthru_download_url(prepared),
            "https://cdn.example/media",
        )
        provider.generate_link.assert_awaited_once_with("locked-ref")
        prepared.preparation = replace(
            preparation,
            target_ref={**preparation.target_ref, "opaque_provider_value": object()},
        )
        self.assertEqual(
            await _stremthru_download_url(prepared),
            "https://cdn.example/media",
        )

    async def test_native_preparation_materializes_only_an_unambiguous_brokered_file(
        self,
    ):
        preparation_id, candidate_id, provider_id, locator_id = (
            str(uuid.uuid4()) for _ in range(4)
        )
        artifact_sha256 = "a" * 64
        artifact = type(
            "Artifact",
            (),
            {
                "nh1": "nh1:" + "1" * 40,
                "nm1": "nm1:" + "b" * 64,
                "metadata": {},
                "manifest": [
                    {
                        "subject": '"opaque.rar" yEnc',
                        "groups": ["alt.video"],
                        "postings": [
                            {
                                "number": 1,
                                "bytes": 11,
                                "message_id": "first@example.test",
                            },
                            {
                                "number": 1,
                                "bytes": 12,
                                "message_id": "fallback@example.test",
                            },
                            {
                                "number": 2,
                                "bytes": 22,
                                "message_id": "second@example.test",
                            },
                        ],
                    }
                ],
            },
        )()
        broker = type(
            "Broker", (), {"resolve_owned_artifact": AsyncMock(return_value=artifact)}
        )()
        server = type(
            "Server",
            (),
            {
                "name": "primary",
                "backup": False,
                "priority": 0,
                "host": "news.example.test",
                "port": 563,
                "tls_mode": "implicit",
                "username": "user",
                "password": "secret",
                "connections": 8,
                "pipeline": 4,
            },
        )()
        equal_priority = type(
            "Server",
            (),
            {
                "name": "alpha",
                "backup": False,
                "priority": 0,
                "host": "alpha.example.test",
                "port": 563,
                "tls_mode": "implicit",
                "username": "user",
                "password": "secret",
                "connections": 3,
                "pipeline": 2,
            },
        )()
        backup = type(
            "Server",
            (),
            {
                "name": "backup",
                "backup": True,
                "priority": 0,
                "host": "backup.example.test",
                "port": 563,
                "tls_mode": "implicit",
                "username": "user",
                "password": "secret",
                "connections": 2,
                "pipeline": 1,
            },
        )()
        provider = type(
            "Provider",
            (),
            {
                "descriptor": type("Descriptor", (), {"kind": "comet_native_usenet"})(),
                "servers_for": staticmethod(
                    lambda _options: (backup, server, equal_priority)
                ),
            },
        )()
        release = type(
            "Release",
            (),
            {
                "locators": (
                    {
                        "kind": "nzb_artifact",
                        "payload": {
                            "artifact_sha256": artifact_sha256,
                            "selection_hint_name": "4f6a9d2c",
                            "selection_hint_size": 33,
                        },
                    },
                )
            },
        )()
        resolution = type(
            "Resolution",
            (),
            {
                "provider": provider,
                "account_partition": b"c" * 32,
                "provider_options": {"source": "instance_pool"},
                "release": release,
            },
        )()
        preparation = PlaybackPreparation(
            preparation_id,
            candidate_id,
            provider_id,
            "comet_native_usenet",
            (locator_id,),
            (0,),
            "stremio",
            "pending",
            None,
            None,
            1,
        )
        prepared = type(
            "Prepared", (), {"preparation": preparation, "resolution": resolution}
        )()
        engine = type(
            "Engine",
            (),
            {
                "catalog_nntp_artifact": AsyncMock(
                    return_value=[
                        _native_catalog_asset(artifact_sha256, 0, "4f6a9d2c", 33)
                    ]
                ),
                "inspect_nntp_postings": AsyncMock(
                    return_value={
                        "version": 1,
                        "artifact_sha256": artifact_sha256,
                        "inspection_state": "provisionally_streamable",
                        "container": "mp4",
                        "duration_millis": 90_500,
                        "inspected_head_bytes": 3,
                        "inspected_tail_bytes": 0,
                    }
                ),
                "open_nntp_session": AsyncMock(
                    return_value=("B" * 22, 33, "c" * 64, None)
                ),
            },
        )()
        with patch(
            "comet.playback.manager.PlaybackPreparationRepository.mark_ready",
            AsyncMock(),
        ) as marked:
            result = await prepare_native_usenet(
                prepared,
                broker,
                object(),
                engine,
                owner_configuration_partition=b"a" * 32,
            )

        self.assertEqual(result, ("B" * 22, 33, "c" * 64))
        engine.catalog_nntp_artifact.assert_awaited_once_with(
            artifact_sha256,
            artifact.nm1,
            artifact.metadata,
            artifact.manifest,
            selection_hint=("4f6a9d2c", 33),
        )
        expected_servers = [
            {
                "provider_configuration_id": "primary",
                "host": "news.example.test",
                "port": 563,
                "tls_mode": "implicit",
                "allow_private": True,
                "username": "user",
                "password": "secret",
                "connections": 8,
                "pipeline": 4,
                "priority": 0,
                "backup": False,
            },
            {
                "provider_configuration_id": "alpha",
                "host": "alpha.example.test",
                "port": 563,
                "tls_mode": "implicit",
                "allow_private": True,
                "username": "user",
                "password": "secret",
                "connections": 3,
                "pipeline": 2,
                "priority": 0,
                "backup": False,
            },
            {
                "provider_configuration_id": "backup",
                "host": "backup.example.test",
                "port": 563,
                "tls_mode": "implicit",
                "allow_private": True,
                "username": "user",
                "password": "secret",
                "connections": 2,
                "pipeline": 1,
                "priority": 0,
                "backup": True,
            },
        ]
        expected_generation = hmac.digest(
            b"c" * 32,
            b"comet-native-provider-set-v1\0"
            + orjson.dumps(expected_servers, option=orjson.OPT_SORT_KEYS),
            "sha256",
        ).hex()
        engine.open_nntp_session.assert_awaited_once_with(
            [
                (1, 11, "first@example.test"),
                (1, 12, "fallback@example.test"),
                (2, 22, "second@example.test"),
            ],
            group="alt.video",
            servers=expected_servers,
            account_partition=b"c" * 32,
            provider_set_generation=expected_generation,
            allow_degraded_playback=True,
        )
        engine.inspect_nntp_postings.assert_awaited_once_with(
            artifact_sha256,
            [
                (1, 11, "first@example.test"),
                (1, 12, "fallback@example.test"),
                (2, 22, "second@example.test"),
            ],
            group="alt.video",
            servers=expected_servers,
            account_partition=b"c" * 32,
            provider_set_generation=expected_generation,
        )
        reversed_generation = hmac.digest(
            b"c" * 32,
            b"comet-native-provider-set-v1\0"
            + orjson.dumps(
                [expected_servers[1], expected_servers[0], expected_servers[2]],
                option=orjson.OPT_SORT_KEYS,
            ),
            "sha256",
        ).hex()
        self.assertNotEqual(expected_generation, reversed_generation)
        marked.assert_awaited_once()
        self.assertEqual(
            marked.await_args.kwargs["target_ref"],
            {
                "source_kind": "session",
                "session_id": "B" * 22,
                "byte_size": 33,
                "session_revision": "c" * 64,
                "selected_asset_id": ANY,
                "relative_path": "4f6a9d2c",
                "native_provider_set_generation": expected_generation,
            },
        )
        original_target = marked.await_args.kwargs["target_ref"]
        ready_preparation = PlaybackPreparation(
            preparation_id,
            candidate_id,
            provider_id,
            "comet_native_usenet",
            (locator_id,),
            (0,),
            "stremio",
            "ready",
            "native",
            original_target,
            1,
        )
        recreated = type(
            "Prepared",
            (),
            {
                "preparation": ready_preparation,
                "resolution": resolution,
            },
        )()
        engine.open_nntp_session.return_value = (
            "C" * 22,
            33,
            "c" * 64,
            "e" * 64,
        )
        with (
            patch(
                "comet.playback.manager.PlaybackPreparationRepository.mark_ready",
                AsyncMock(),
            ) as recreated_ready,
            patch(
                "comet.playback.manager.PlaybackPreparationRepository.mark_failed",
                AsyncMock(),
            ) as recreated_failed,
        ):
            recreated_result = await prepare_native_usenet(
                recreated,
                broker,
                object(),
                engine,
                owner_configuration_partition=b"a" * 32,
            )

        self.assertEqual(recreated_result, ("C" * 22, 33, "c" * 64))
        self.assertEqual(
            recreated_ready.await_args.kwargs["target_ref"]["session_id"],
            "C" * 22,
        )
        self.assertEqual(
            recreated_ready.await_args.kwargs["target_ref"]["strong_asset_revision"],
            "e" * 64,
        )
        recreated_failed.assert_not_awaited()

        engine.open_nntp_session.return_value = ("D" * 22, 33, "d" * 64, None)
        with (
            patch(
                "comet.playback.manager.PlaybackPreparationRepository.mark_ready",
                AsyncMock(),
            ) as changed_ready,
            patch(
                "comet.playback.manager.PlaybackPreparationRepository.mark_failed",
                AsyncMock(),
            ) as changed_failed,
        ):
            with self.assertRaisesRegex(
                ValueError,
                "native media representation changed",
            ):
                await prepare_native_usenet(
                    recreated,
                    broker,
                    object(),
                    engine,
                    owner_configuration_partition=b"a" * 32,
                )

        changed_ready.assert_not_awaited()
        changed_failed.assert_awaited_once_with(
            preparation_id,
            owner_configuration_partition=b"a" * 32,
            provider_account_partition=b"c" * 32,
            code="native_representation_changed",
        )

    async def test_native_episode_preparation_scopes_work_to_the_selected_pack_file(
        self,
    ):
        preparation_id, candidate_id, provider_id, locator_id = (
            str(uuid.uuid4()) for _ in range(4)
        )
        artifact_sha256 = "e" * 64
        artifact = type(
            "Artifact",
            (),
            {
                "nh1": "nh1:" + "1" * 40,
                "nm1": "nm1:" + "f" * 64,
                "metadata": {},
                "manifest": [
                    {
                        "subject": '"Show.S01E01.mkv" yEnc',
                        "groups": ["alt.video"],
                        "postings": [
                            {
                                "number": 1,
                                "bytes": 11,
                                "message_id": "episode-one@example.test",
                            }
                        ],
                    },
                    {
                        "subject": '"Show.S01E02E03.mkv" yEnc',
                        "groups": ["alt.video"],
                        "postings": [
                            {
                                "number": 1,
                                "bytes": 22,
                                "message_id": "episode-two@example.test",
                            }
                        ],
                    },
                    {
                        "subject": '"Show.S01E02.Sample.mkv" yEnc',
                        "groups": ["alt.video"],
                        "postings": [
                            {
                                "number": 1,
                                "bytes": 33,
                                "message_id": "sample@example.test",
                            }
                        ],
                    },
                ],
            },
        )()
        broker = type(
            "Broker",
            (),
            {"resolve_owned_artifact": AsyncMock(return_value=artifact)},
        )()
        server = type(
            "Server",
            (),
            {
                "name": "primary",
                "backup": False,
                "priority": 0,
                "host": "news.example.test",
                "port": 563,
                "tls_mode": "implicit",
                "username": "user",
                "password": "secret",
                "connections": 2,
                "pipeline": 1,
            },
        )()
        provider = type(
            "Provider",
            (),
            {
                "descriptor": type("Descriptor", (), {"kind": "comet_native_usenet"})(),
                "servers_for": staticmethod(lambda _options: (server,)),
            },
        )()
        release = type(
            "Release",
            (),
            {
                "locators": (
                    {
                        "kind": "nzb_artifact",
                        "payload": {"artifact_sha256": artifact_sha256},
                    },
                )
            },
        )()
        resolution = type(
            "Resolution",
            (),
            {
                "provider": provider,
                "account_partition": b"c" * 32,
                "provider_options": {"source": "instance_pool"},
                "release": release,
            },
        )()
        preparation = PlaybackPreparation(
            preparation_id,
            candidate_id,
            provider_id,
            "comet_native_usenet",
            (locator_id,),
            (1, 1, 2),
            "stremio",
            "pending",
            None,
            None,
            1,
        )
        prepared = type(
            "Prepared", (), {"preparation": preparation, "resolution": resolution}
        )()
        evidence = {
            "version": 1,
            "artifact_sha256": artifact_sha256,
            "inspection_state": "provisionally_streamable",
            "container": "matroska",
            "duration_millis": None,
            "inspected_head_bytes": 22,
            "inspected_tail_bytes": 0,
        }
        engine = type(
            "Engine",
            (),
            {
                "catalog_nntp_artifact": AsyncMock(
                    return_value=[
                        _native_catalog_asset(
                            artifact_sha256, 0, "Show.S01E01.mkv", 11
                        ),
                        _native_catalog_asset(
                            artifact_sha256, 1, "Show.S01E02E03.mkv", 22
                        ),
                    ]
                ),
                "inspect_nntp_postings": AsyncMock(return_value=evidence),
                "open_nntp_session": AsyncMock(
                    return_value=("B" * 22, 22, "c" * 64, None)
                ),
            },
        )()

        with patch(
            "comet.playback.manager.PlaybackPreparationRepository.mark_ready",
            AsyncMock(),
        ) as marked:
            await prepare_native_usenet(
                prepared,
                broker,
                object(),
                engine,
                owner_configuration_partition=b"a" * 32,
            )

        selected_postings = [(1, 22, "episode-two@example.test")]
        self.assertEqual(
            engine.inspect_nntp_postings.await_args.args[1], selected_postings
        )
        self.assertEqual(engine.open_nntp_session.await_args.args[0], selected_postings)
        self.assertEqual(
            marked.await_args.kwargs["target_ref"]["relative_path"],
            "Show.S01E02E03.mkv",
        )

    async def test_native_archive_preparation_acquires_only_the_selected_volume_closure(
        self,
    ):
        preparation_id, candidate_id, provider_id, locator_id = (
            str(uuid.uuid4()) for _ in range(4)
        )
        artifact_sha256 = "a" * 64
        volume_identity = "c" * 64
        output_identity = "d" * 64
        set_identity = "e" * 64
        artifact = type(
            "Artifact",
            (),
            {
                "nh1": "nh1:" + "1" * 40,
                "nm1": "nm1:" + "b" * 64,
                "metadata": {"password": "archive-secret"},
                "manifest": [
                    {
                        "subject": '"release.rar" yEnc',
                        "groups": ["alt.video"],
                        "postings": [
                            {
                                "number": 1,
                                "bytes": 100,
                                "message_id": "archive@example.test",
                            }
                        ],
                    },
                    {
                        "subject": '"release.par2" yEnc',
                        "groups": ["alt.video"],
                        "postings": [
                            {
                                "number": 1,
                                "bytes": 10,
                                "message_id": "par2@example.test",
                            }
                        ],
                    },
                ],
            },
        )()
        broker = type(
            "Broker",
            (),
            {"resolve_owned_artifact": AsyncMock(return_value=artifact)},
        )()
        server = type(
            "Server",
            (),
            {
                "name": "primary",
                "backup": False,
                "priority": 0,
                "host": "news.example.test",
                "port": 563,
                "tls_mode": "implicit",
                "username": "user",
                "password": "secret",
                "connections": 2,
                "pipeline": 1,
            },
        )()
        provider = type(
            "Provider",
            (),
            {
                "descriptor": type("Descriptor", (), {"kind": "comet_native_usenet"})(),
                "servers_for": staticmethod(lambda _options: (server,)),
            },
        )()
        release = type(
            "Release",
            (),
            {
                "locators": (
                    {
                        "kind": "nzb_artifact",
                        "payload": {"artifact_sha256": artifact_sha256},
                    },
                )
            },
        )()
        resolution = type(
            "Resolution",
            (),
            {
                "provider": provider,
                "account_partition": b"c" * 32,
                "provider_options": {"source": "instance_pool"},
                "release": release,
            },
        )()
        preparation = PlaybackPreparation(
            preparation_id,
            candidate_id,
            provider_id,
            "comet_native_usenet",
            (locator_id,),
            (0,),
            "stremio",
            "pending",
            None,
            None,
            1,
        )
        prepared = type(
            "Prepared", (), {"preparation": preparation, "resolution": resolution}
        )()
        plan = {
            "set_identity": set_identity,
            "kind": {"layout": "single_archive", "format": "rar5"},
            "exact_size": 100,
            "volumes": [
                {
                    "content_identity": volume_identity,
                    "relative_path": "release.rar",
                    "number": 0,
                    "exact_size": 100,
                }
            ],
        }
        member = _nested_archive_member(
            set_identity, "payload.tar.gz", "Movie.2026.mkv", exact_size=42
        )
        evidence = {
            "version": 1,
            "materialization_identity": output_identity,
            "source_identity": output_identity,
            "inspection_state": "provisionally_streamable",
            "container": "matroska",
            "duration_millis": None,
            "inspected_head_bytes": 42,
            "inspected_tail_bytes": 0,
        }
        engine = type(
            "Engine",
            (),
            {
                "catalog_nntp_artifact": AsyncMock(
                    return_value=[
                        _native_catalog_asset(
                            artifact_sha256,
                            0,
                            "release.rar",
                            100,
                            kind="archive",
                        ),
                        _native_catalog_asset(
                            artifact_sha256,
                            1,
                            "release.par2",
                            10,
                            kind="par2",
                        ),
                    ]
                ),
                "materialize_nntp_postings": AsyncMock(
                    return_value=(volume_identity, 100, "9" * 64)
                ),
                "open_nntp_session": AsyncMock(
                    return_value=("S" * 22, 100, "7" * 64, None)
                ),
                "catalog_session_archive_volumes": AsyncMock(
                    side_effect=EngineArchiveError(
                        "archive_volume_gap", retryable=False
                    )
                ),
                "stats": AsyncMock(
                    return_value={
                        "requests_active": 1,
                        "request_workers": 32,
                        "nntp_preparation_slots": 8,
                    }
                ),
                "plan_archive_volumes": AsyncMock(return_value=plan),
                "catalog_stored_archive_volumes": AsyncMock(
                    side_effect=EngineArchiveError(
                        "archive_header_incomplete", retryable=False
                    )
                ),
                "catalog_nested_archive_volumes": AsyncMock(
                    return_value=(plan, [member])
                ),
                "extract_nested_archive_volume_set": AsyncMock(
                    return_value=(output_identity, 42, "f" * 64)
                ),
                "inspect_materialization": AsyncMock(return_value=evidence),
            },
        )()

        with (
            patch(
                "comet.playback.manager.PlaybackPreparationRepository.mark_ready",
                AsyncMock(),
            ) as marked,
            patch(
                "comet.playback.manager.MaterializedArtifactRepository."
                "register_for_preparation",
                AsyncMock(),
            ) as registered,
            patch(
                "comet.playback.manager.MaterializedArtifactRepository."
                "acquire_publication_lease",
                AsyncMock(
                    return_value=type(
                        "PublicationLease",
                        (),
                        {"close": AsyncMock()},
                    )()
                ),
            ) as acquired,
            patch(
                "comet.playback.manager.MaterializedArtifactRepository."
                "retain_for_preparation",
                AsyncMock(),
            ) as retained,
        ):
            result = await prepare_native_usenet(
                prepared,
                broker,
                object(),
                engine,
                owner_configuration_partition=b"a" * 32,
            )

        self.assertEqual(result, (output_identity, 42, output_identity))
        self.assertEqual(
            registered.await_args.kwargs["artifacts"][0].artifact_sha256,
            output_identity,
        )
        self.assertEqual(acquired.await_count, 2)
        self.assertEqual(registered.await_count, 2)
        retained.assert_awaited_once_with(
            preparation_id,
            owner_configuration_partition=b"a" * 32,
            artifact_sha256s=(output_identity,),
        )
        engine.materialize_nntp_postings.assert_awaited_once()
        self.assertEqual(
            engine.materialize_nntp_postings.await_args.args[0],
            [(1, 100, "archive@example.test")],
        )
        engine.plan_archive_volumes.assert_awaited_once_with(
            [(volume_identity, "release.rar", 100)]
        )
        engine.catalog_nested_archive_volumes.assert_awaited_once_with(
            [(volume_identity, "release.rar", 100)],
            passphrase="archive-secret",
        )
        engine.extract_nested_archive_volume_set.assert_awaited_once_with(
            [(volume_identity, "release.rar", 100)],
            42,
            ("payload.tar.gz", "Movie.2026.mkv"),
            passphrase="archive-secret",
        )
        engine.inspect_materialization.assert_awaited_once_with(
            output_identity,
            42,
        )
        self.assertEqual(
            marked.await_args.kwargs["target_ref"],
            {
                "source_kind": "raw_composite",
                "raw_composite_id": output_identity,
                "byte_size": 42,
                "asset_revision": output_identity,
                "strong_asset_revision": "f" * 64,
                "selected_asset_id": member["member_id"],
                "relative_path": "payload.tar.gz!/Movie.2026.mkv",
                "archive_set_identity": set_identity,
                "native_provider_set_generation": ANY,
            },
        )

    async def test_par2_repair_uses_cryptographic_source_mapping_to_choose_the_closure(
        self,
    ):
        unrelated_recovery_identity = "9" * 64
        recovery_identity = "a" * 64
        complete_identity = "b" * 64
        repaired_identity = "c" * 64
        set_id = "d" * 32
        right_file_id = "1" * 32
        wrong_file_id = "2" * 32
        catalog_files = [
            {
                "file_id": right_file_id,
                "relative_path": "right.rar",
                "exact_size": 50,
                "full_md5": "3" * 32,
                "first_16k_md5": "4" * 32,
                "slice_count": 1,
            },
            {
                "file_id": wrong_file_id,
                "relative_path": "wrong.rar",
                "exact_size": 500,
                "full_md5": "5" * 32,
                "first_16k_md5": "6" * 32,
                "slice_count": 1,
            },
        ]
        engine = type(
            "Engine",
            (),
            {
                "materialize_nntp_postings": AsyncMock(
                    side_effect=[
                        (unrelated_recovery_identity, 10, "7" * 64),
                        (recovery_identity, 10, "8" * 64),
                    ]
                ),
                "discover_par2_sets": AsyncMock(
                    side_effect=lambda volumes: (
                        [
                            {
                                "set_id": "0" * 32,
                                "slice_size": 1024,
                                "files": [
                                    {
                                        "file_id": "0" * 32,
                                        "relative_path": "unrelated.rar",
                                        "exact_size": 60,
                                        "full_md5": "7" * 32,
                                        "first_16k_md5": "8" * 32,
                                        "slice_count": 1,
                                    }
                                ],
                                "recovery_exponents": [],
                                "volume_content_identities": [
                                    unrelated_recovery_identity
                                ],
                            }
                        ]
                        if volumes[0][0] == unrelated_recovery_identity
                        else [
                            {
                                "set_id": set_id,
                                "slice_size": 1024,
                                "files": catalog_files,
                                "recovery_exponents": [],
                                "volume_content_identities": [recovery_identity],
                            }
                        ]
                    )
                ),
                "map_par2_sources": AsyncMock(
                    side_effect=[
                        EngineArchiveError(
                            "par2_source_unmatched",
                            retryable=False,
                        ),
                        {
                            "version": 1,
                            "set_id": set_id,
                            "slice_size": 1024,
                            "mappings": [
                                {
                                    "content_identity": complete_identity,
                                    "file_id": right_file_id,
                                    "relative_path": "right.rar",
                                    "exact_size": 50,
                                    "slice_count": 1,
                                }
                            ],
                        },
                    ]
                ),
                "repair_par2": AsyncMock(
                    return_value={
                        "version": 1,
                        "set_id": set_id,
                        "file_id": right_file_id,
                        "relative_path": "right.rar",
                        "identity": repaired_identity,
                        "byte_size": 50,
                        "asset_revision": "f" * 64,
                    }
                ),
            },
        )()
        unrelated_par2_asset = UsenetAsset(
            b"p" * 32,
            0,
            "unrelated.par2",
            10,
            kind="par2",
        )
        par2_asset = UsenetAsset(
            b"q" * 32,
            1,
            "release.par2",
            10,
            kind="par2",
        )

        group, volumes, artifacts = await _repair_archive_closure(
            engine,
            [
                {
                    "groups": ["alt.video"],
                    "postings": [
                        {
                            "number": 1,
                            "bytes": 10,
                            "message_id": "unrelated-par2@example.test",
                        }
                    ],
                },
                {
                    "groups": ["alt.video"],
                    "postings": [
                        {
                            "number": 1,
                            "bytes": 10,
                            "message_id": "par2@example.test",
                        }
                    ],
                },
            ],
            (unrelated_par2_asset, par2_asset),
            (),
            [(complete_identity, "opaque.bin", 50)],
            EngineNntpError("nntp_article_missing"),
            (0,),
            engine_servers=[{"provider_configuration_id": "primary"}],
            account_partition=b"a" * 32,
            provider_set_generation="e" * 64,
        )

        self.assertEqual(group.selection_path, "right")
        self.assertEqual(volumes, [(complete_identity, "right.rar", 50)])
        self.assertEqual(artifacts, [])
        self.assertEqual(engine.map_par2_sources.await_count, 2)
        self.assertEqual(
            engine.map_par2_sources.await_args_list[0].args,
            (
                [(unrelated_recovery_identity, "unrelated.par2", 10)],
                [(complete_identity, "opaque.bin", 50)],
            ),
        )
        self.assertEqual(
            engine.map_par2_sources.await_args_list[0].kwargs,
            {"recovery_set_id": "0" * 32},
        )
        self.assertEqual(
            engine.map_par2_sources.await_args_list[1].args,
            (
                [(recovery_identity, "release.par2", 10)],
                [(complete_identity, "opaque.bin", 50)],
            ),
        )
        self.assertEqual(
            engine.map_par2_sources.await_args_list[1].kwargs,
            {"recovery_set_id": set_id},
        )
        engine.repair_par2.assert_not_awaited()

    async def test_par2_repair_accepts_opaque_archive_names_only_after_checksum_mapping(
        self,
    ):
        recovery_identity = "a" * 64
        repaired_identity = "b" * 64
        set_id = "c" * 32
        file_id = "d" * 32
        engine = type(
            "Engine",
            (),
            {
                "materialize_nntp_postings": AsyncMock(
                    return_value=(recovery_identity, 10, "e" * 64)
                ),
                "discover_par2_sets": AsyncMock(
                    return_value=[
                        {
                            "set_id": set_id,
                            "slice_size": 4,
                            "files": [
                                {
                                    "file_id": file_id,
                                    "relative_path": "release.rar",
                                    "exact_size": 8,
                                    "full_md5": "1" * 32,
                                    "first_16k_md5": "2" * 32,
                                    "slice_count": 2,
                                }
                            ],
                            "recovery_exponents": [0],
                            "volume_content_identities": [recovery_identity],
                        }
                    ]
                ),
                "repair_par2": AsyncMock(
                    return_value={
                        "version": 1,
                        "set_id": set_id,
                        "file_id": file_id,
                        "relative_path": "release.rar",
                        "identity": repaired_identity,
                        "byte_size": 8,
                        "asset_revision": "f" * 64,
                        "partial_source_mapped": True,
                    }
                ),
            },
        )()
        partial_asset = UsenetAsset(
            b"o" * 32,
            0,
            "4f6a9d2c",
            8,
            kind="archive",
        )
        par2_asset = UsenetAsset(
            b"p" * 32,
            1,
            "unrelated-sidecar-name.par2",
            10,
            kind="par2",
        )
        manifest = [
            {
                "groups": ["future/hierarchy"],
                "postings": [
                    {
                        "number": 1,
                        "bytes": 8,
                        "message_id": "partial@example.test",
                    }
                ],
            },
            {
                "groups": ["future/hierarchy"],
                "postings": [
                    {
                        "number": 1,
                        "bytes": 10,
                        "message_id": "par2@example.test",
                    }
                ],
            },
        ]

        group, volumes, artifacts = await _repair_archive_closure(
            engine,
            manifest,
            (par2_asset,),
            (partial_asset,),
            [],
            EngineNntpError("nntp_article_missing"),
            (0,),
            engine_servers=[{"provider_configuration_id": "primary"}],
            account_partition=b"a" * 32,
            provider_set_generation="0" * 64,
        )

        self.assertEqual(group.selection_path, "release")
        self.assertEqual(volumes, [(repaired_identity, "release.rar", 8)])
        self.assertEqual(
            [artifact.artifact_sha256 for artifact in artifacts],
            [repaired_identity],
        )
        engine.repair_par2.assert_awaited_once()
        self.assertEqual(
            engine.repair_par2.await_args.kwargs["partial_sources"],
            [
                (
                    [(1, 8, "partial@example.test")],
                    "future/hierarchy",
                )
            ],
        )
        engine.repair_par2.return_value = {
            **engine.repair_par2.return_value,
            "partial_source_mapped": False,
        }
        with self.assertRaises(EngineArchiveError) as raised:
            await _repair_archive_closure(
                engine,
                manifest,
                (par2_asset,),
                (partial_asset,),
                [],
                EngineNntpError("nntp_article_missing"),
                (0,),
                engine_servers=[{"provider_configuration_id": "primary"}],
                account_partition=b"a" * 32,
                provider_set_generation="0" * 64,
            )
        self.assertEqual(raised.exception.code, "par2_source_unmatched")

    async def test_par2_repair_delegates_availability_to_the_native_engine(
        self,
    ):
        recovery_identity = "a" * 64
        set_id = "b" * 32
        file_ids = ("c" * 32, "d" * 32)
        engine = type(
            "Engine",
            (),
            {
                "materialize_nntp_postings": AsyncMock(
                    return_value=(recovery_identity, 10, "d" * 64)
                ),
                "discover_par2_sets": AsyncMock(
                    return_value=[
                        {
                            "set_id": set_id,
                            "slice_size": 5 * 1024 * 1024,
                            "files": [
                                {
                                    "file_id": file_ids[0],
                                    "relative_path": "release.rar",
                                    "exact_size": 100,
                                    "full_md5": "1" * 32,
                                    "first_16k_md5": "2" * 32,
                                    "slice_count": 1,
                                },
                                {
                                    "file_id": file_ids[1],
                                    "relative_path": "release.r00",
                                    "exact_size": 200,
                                    "full_md5": "3" * 32,
                                    "first_16k_md5": "4" * 32,
                                    "slice_count": 1,
                                },
                            ],
                            "recovery_exponents": [0],
                            "volume_content_identities": [recovery_identity],
                        }
                    ]
                ),
                "repair_par2": AsyncMock(
                    side_effect=EngineArchiveError(
                        "repair_insufficient", retryable=False
                    )
                ),
            },
        )()
        partial_assets = (
            UsenetAsset(
                b"n" * 32,
                0,
                "obfuscated-one",
                100,
                kind="archive",
            ),
            UsenetAsset(
                b"o" * 32,
                1,
                "obfuscated-two",
                200,
                kind="archive",
            ),
        )
        par2_asset = UsenetAsset(
            b"p" * 32,
            2,
            "release.par2",
            10,
            kind="par2",
        )
        manifest = [
            {
                "groups": ["alt.video"],
                "postings": [
                    {
                        "number": 1,
                        "bytes": 100,
                        "message_id": "missing@example.test",
                    }
                ],
            },
            {
                "groups": ["alt.video"],
                "postings": [
                    {
                        "number": 1,
                        "bytes": 200,
                        "message_id": "also-missing@example.test",
                    }
                ],
            },
            {
                "groups": ["alt.video"],
                "postings": [
                    {
                        "number": 1,
                        "bytes": 10,
                        "message_id": "par2@example.test",
                    }
                ],
            },
        ]

        with self.assertRaises(EngineArchiveError) as raised:
            await _repair_archive_closure(
                engine,
                manifest,
                (par2_asset,),
                partial_assets,
                [],
                EngineNntpError("nntp_article_missing"),
                (0,),
                engine_servers=[{"provider_configuration_id": "primary"}],
                account_partition=b"a" * 32,
                provider_set_generation="e" * 64,
            )

        self.assertEqual(raised.exception.code, "repair_insufficient")
        engine.repair_par2.assert_awaited_once()

    async def test_par2_repair_skips_proven_insufficient_recovery_ranks(self):
        set_id = "b" * 32
        file_id = "c" * 32
        identities = ("1" * 64, "2" * 64, "3" * 64)
        par2_assets = tuple(
            UsenetAsset(
                bytes([index]) * 32,
                index,
                f"recovery-{index}",
                100,
                kind="par2",
            )
            for index in range(1, 4)
        )

        def discover(volumes):
            return [
                {
                    "set_id": set_id,
                    "slice_size": 4,
                    "files": [
                        {
                            "file_id": file_id,
                            "relative_path": "release.rar",
                            "exact_size": 8,
                            "full_md5": "4" * 32,
                            "first_16k_md5": "5" * 32,
                            "slice_count": 2,
                        }
                    ],
                    "recovery_exponents": list(range(max(0, len(volumes) - 1))),
                    "volume_content_identities": [volume[0] for volume in volumes],
                }
            ]

        engine = type(
            "Engine",
            (),
            {
                "materialize_nntp_postings": AsyncMock(
                    side_effect=[(identity, 100, "d" * 64) for identity in identities]
                ),
                "discover_par2_sets": AsyncMock(side_effect=discover),
                "repair_par2": AsyncMock(
                    side_effect=EngineArchiveError(
                        "repair_insufficient",
                        retryable=False,
                        required_recovery_blocks=3,
                    )
                ),
            },
        )()
        partial_asset = UsenetAsset(
            b"a" * 32,
            0,
            "release.rar",
            8,
            kind="archive",
        )
        manifest = [
            {
                "groups": ["alt.video"],
                "postings": [
                    {
                        "number": 1,
                        "bytes": 8,
                        "message_id": "missing@example.test",
                    }
                ],
            },
            *[
                {
                    "groups": ["alt.video"],
                    "postings": [
                        {
                            "number": 1,
                            "bytes": 100,
                            "message_id": f"recovery-{index}@example.test",
                        }
                    ],
                }
                for index in range(1, 4)
            ],
        ]

        with self.assertRaises(EngineArchiveError) as raised:
            await _repair_archive_closure(
                engine,
                manifest,
                par2_assets,
                (partial_asset,),
                [],
                EngineNntpError("nntp_article_missing"),
                (0,),
                engine_servers=[{"provider_configuration_id": "primary"}],
                account_partition=b"a" * 32,
                provider_set_generation="e" * 64,
            )

        self.assertEqual(raised.exception.code, "repair_insufficient")
        self.assertEqual(engine.materialize_nntp_postings.await_count, 3)
        engine.repair_par2.assert_awaited_once()

    def test_par2_recovery_capacity_uses_the_packet_structural_minimum(self):
        assets = tuple(
            UsenetAsset(bytes([index]) * 32, index, str(index), size, kind="par2")
            for index, size in enumerate((71, 72, 143), 1)
        )

        self.assertEqual(_maximum_par2_recovery_blocks(assets, 4), 2)

    async def test_par2_repair_publishes_a_probe_verified_direct_video(self):
        unrelated_recovery_identity = "9" * 64
        recovery_identity = "a" * 64
        repaired_identity = "b" * 64
        set_id = "c" * 32
        file_id = "d" * 32
        unrelated_set_id = "a" * 32
        unrelated_file_id = "b" * 32
        inspection = {
            "version": 1,
            "materialization_identity": repaired_identity,
            "source_identity": "e" * 64,
            "inspection_state": "provisionally_streamable",
            "container": "matroska",
            "duration_millis": None,
            "inspected_head_bytes": 8,
            "inspected_tail_bytes": 0,
        }
        engine = type(
            "Engine",
            (),
            {
                "materialize_nntp_postings": AsyncMock(
                    side_effect=[
                        (unrelated_recovery_identity, 10, "7" * 64),
                        (recovery_identity, 10, "8" * 64),
                    ]
                ),
                "discover_par2_sets": AsyncMock(
                    side_effect=lambda volumes: (
                        [
                            {
                                "set_id": unrelated_set_id,
                                "slice_size": 4,
                                "files": [
                                    {
                                        "file_id": unrelated_file_id,
                                        "relative_path": "Small.mkv",
                                        "exact_size": 4,
                                        "full_md5": "1" * 32,
                                        "first_16k_md5": "2" * 32,
                                        "slice_count": 1,
                                    }
                                ],
                                "recovery_exponents": [0],
                                "volume_content_identities": [
                                    unrelated_recovery_identity
                                ],
                            }
                        ]
                        if volumes[0][0] == unrelated_recovery_identity
                        else [
                            {
                                "set_id": set_id,
                                "slice_size": 4,
                                "files": [
                                    {
                                        "file_id": file_id,
                                        "relative_path": "4f6a9d2c",
                                        "exact_size": 8,
                                        "full_md5": "1" * 32,
                                        "first_16k_md5": "2" * 32,
                                        "slice_count": 2,
                                    }
                                ],
                                "recovery_exponents": [0],
                                "volume_content_identities": [recovery_identity],
                            }
                        ]
                    )
                ),
                "repair_par2": AsyncMock(
                    return_value={
                        "version": 1,
                        "set_id": set_id,
                        "file_id": file_id,
                        "relative_path": "4f6a9d2c",
                        "identity": repaired_identity,
                        "byte_size": 8,
                        "asset_revision": "f" * 64,
                    }
                ),
                "inspect_materialization": AsyncMock(return_value=inspection),
            },
        )()
        failed = UsenetAsset(b"v" * 32, 0, "4f6a9d2c", 8)
        unrelated_par2_asset = UsenetAsset(
            b"p" * 32,
            1,
            "unrelated.par2",
            10,
            kind="par2",
        )
        par2_asset = UsenetAsset(
            b"q" * 32,
            2,
            "release.par2",
            10,
            kind="par2",
        )
        manifest = [
            {
                "groups": ["alt.video"],
                "postings": [
                    {
                        "number": 1,
                        "bytes": 8,
                        "message_id": "video@example.test",
                    }
                ],
            },
            {
                "groups": ["alt.video"],
                "postings": [
                    {
                        "number": 1,
                        "bytes": 10,
                        "message_id": "unrelated-par2@example.test",
                    }
                ],
            },
            {
                "groups": ["alt.video"],
                "postings": [
                    {
                        "number": 1,
                        "bytes": 10,
                        "message_id": "par2@example.test",
                    }
                ],
            },
        ]

        with patch("comet.playback.manager.log.info") as logged:
            identity, size, revision, target = await _repair_direct_asset(
                engine,
                manifest,
                (unrelated_par2_asset, par2_asset),
                failed,
                EngineNntpError("nntp_article_missing"),
                (0,),
                engine_servers=[{"provider_configuration_id": "primary"}],
                account_partition=b"a" * 32,
                provider_set_generation="f" * 64,
            )

        self.assertEqual(
            (identity, size, revision), (repaired_identity, 8, repaired_identity)
        )
        self.assertEqual(target["relative_path"], "4f6a9d2c")
        self.assertEqual(target["raw_composite_id"], repaired_identity)
        self.assertEqual(target["strong_asset_revision"], "f" * 64)
        self.assertNotIn("par2_repair", target)
        engine.repair_par2.assert_awaited_once_with(
            [(recovery_identity, "release.par2", 10)],
            [],
            file_id,
            partial_sources=[
                (
                    [(1, 8, "video@example.test")],
                    "alt.video",
                )
            ],
            account_partition=b"a" * 32,
            provider_set_generation="f" * 64,
            recovery_set_id=set_id,
        )
        engine.inspect_materialization.assert_awaited_once_with(
            repaired_identity,
            8,
        )
        self.assertEqual(
            [call.args[0] for call in logged.call_args_list],
            [
                "usenet.par2_recovery.started",
                "usenet.par2_recovery.selected",
                "usenet.par2_recovery.selected",
                "usenet.par2_repair.started",
            ],
        )
        self.assertEqual(
            logged.call_args_list[3].kwargs,
            {
                "operation": "par2_repair_direct",
                "item_count": 1,
                "transferred_bytes": 10,
                "candidate_count": 0,
                "requested_count": 1,
            },
        )

    async def test_par2_repair_tries_the_next_movie_after_container_mismatch(self):
        recovery_identity = "a" * 64
        first_identity = "b" * 64
        second_identity = "c" * 64
        set_id = "d" * 32
        first_file_id = "1" * 32
        second_file_id = "2" * 32
        inspection = {
            "version": 1,
            "materialization_identity": second_identity,
            "source_identity": "e" * 64,
            "inspection_state": "provisionally_streamable",
            "container": "matroska",
            "duration_millis": None,
            "inspected_head_bytes": 8,
            "inspected_tail_bytes": 0,
        }
        engine = type(
            "Engine",
            (),
            {
                "materialize_nntp_postings": AsyncMock(
                    return_value=(recovery_identity, 10, "8" * 64)
                ),
                "discover_par2_sets": AsyncMock(
                    return_value=[
                        {
                            "set_id": set_id,
                            "slice_size": 4,
                            "files": [
                                {
                                    "file_id": first_file_id,
                                    "relative_path": "Largest.mkv",
                                    "exact_size": 12,
                                    "full_md5": "3" * 32,
                                    "first_16k_md5": "4" * 32,
                                    "slice_count": 3,
                                },
                                {
                                    "file_id": second_file_id,
                                    "relative_path": "Movie.mkv",
                                    "exact_size": 8,
                                    "full_md5": "5" * 32,
                                    "first_16k_md5": "6" * 32,
                                    "slice_count": 2,
                                },
                            ],
                            "recovery_exponents": [0],
                            "volume_content_identities": [recovery_identity],
                        }
                    ]
                ),
                "repair_par2": AsyncMock(
                    side_effect=[
                        {
                            "version": 1,
                            "set_id": set_id,
                            "file_id": first_file_id,
                            "relative_path": "Largest.mkv",
                            "identity": first_identity,
                            "byte_size": 12,
                            "asset_revision": "7" * 64,
                        },
                        {
                            "version": 1,
                            "set_id": set_id,
                            "file_id": second_file_id,
                            "relative_path": "Movie.mkv",
                            "identity": second_identity,
                            "byte_size": 8,
                            "asset_revision": "8" * 64,
                        },
                    ]
                ),
                "inspect_materialization": AsyncMock(
                    side_effect=[
                        EngineArchiveError(
                            "container_signature_mismatch",
                            retryable=False,
                        ),
                        inspection,
                    ]
                ),
            },
        )()
        failed = UsenetAsset(b"v" * 32, 0, "obfuscated.mkv", 12)
        par2_asset = UsenetAsset(
            b"p" * 32,
            1,
            "release.par2",
            10,
            kind="par2",
        )
        manifest = [
            {
                "groups": ["alt.video"],
                "postings": [
                    {
                        "number": 1,
                        "bytes": 12,
                        "message_id": "video@example.test",
                    }
                ],
            },
            {
                "groups": ["alt.video"],
                "postings": [
                    {
                        "number": 1,
                        "bytes": 10,
                        "message_id": "par2@example.test",
                    }
                ],
            },
        ]

        identity, size, revision, target = await _repair_direct_asset(
            engine,
            manifest,
            (par2_asset,),
            failed,
            EngineNntpError("nntp_article_missing"),
            (0,),
            engine_servers=[{"provider_configuration_id": "primary"}],
            account_partition=b"a" * 32,
            provider_set_generation="f" * 64,
        )

        self.assertEqual(
            (identity, size, revision),
            (second_identity, 8, second_identity),
        )
        self.assertEqual(target["relative_path"], "Movie.mkv")
        self.assertEqual(target["strong_asset_revision"], "8" * 64)
        self.assertNotIn("par2_repair", target)
        self.assertEqual(engine.repair_par2.await_count, 2)
        self.assertEqual(
            engine.repair_par2.await_args_list[1].args,
            (
                [(recovery_identity, "release.par2", 10)],
                [(first_identity, "Largest.mkv", 12)],
                second_file_id,
            ),
        )
        self.assertEqual(engine.inspect_materialization.await_count, 2)

    async def test_par2_repair_expands_then_rolls_the_recovery_window(self):
        index_identity = "a" * 64
        small_identity = "b" * 64
        large_identity = "c" * 64
        repaired_identity = "d" * 64
        set_id = "1" * 32
        file_id = "2" * 32
        assets = (
            UsenetAsset(b"i" * 32, 1, "release.par2", 1, kind="par2"),
            UsenetAsset(b"s" * 32, 2, "release.vol00+01.par2", 40, kind="par2"),
            UsenetAsset(b"l" * 32, 3, "release.vol01+02.par2", 80, kind="par2"),
        )
        identities = (index_identity, small_identity, large_identity)
        manifest = [
            {
                "groups": ["alt.video"],
                "postings": [
                    {
                        "number": 1,
                        "bytes": 8,
                        "message_id": "video@example.test",
                    }
                ],
            },
            *[
                {
                    "groups": ["alt.video"],
                    "postings": [
                        {
                            "number": 1,
                            "bytes": asset.declared_bytes,
                            "message_id": f"par2-{asset.file_index}@example.test",
                        }
                    ],
                }
                for asset in assets
            ],
        ]

        def discover(volumes):
            volume_identities = sorted(volume[0] for volume in volumes)
            return [
                {
                    "set_id": set_id,
                    "slice_size": 4,
                    "files": [
                        {
                            "file_id": file_id,
                            "relative_path": "Movie.mkv",
                            "exact_size": 8,
                            "full_md5": "3" * 32,
                            "first_16k_md5": "4" * 32,
                            "slice_count": 2,
                        }
                    ],
                    "recovery_exponents": (
                        [] if volume_identities == [index_identity] else [0]
                    ),
                    "volume_content_identities": volume_identities,
                }
            ]

        engine = type(
            "Engine",
            (),
            {
                "materialize_nntp_postings": AsyncMock(
                    side_effect=[
                        (identity, asset.declared_bytes, "e" * 64)
                        for identity, asset in zip(identities, assets, strict=True)
                    ]
                ),
                "discover_par2_sets": AsyncMock(side_effect=discover),
                "repair_par2": AsyncMock(
                    side_effect=[
                        EngineArchiveError("repair_insufficient", retryable=False),
                        {
                            "version": 1,
                            "set_id": set_id,
                            "file_id": file_id,
                            "relative_path": "Movie.mkv",
                            "identity": repaired_identity,
                            "byte_size": 8,
                            "asset_revision": "f" * 64,
                        },
                    ]
                ),
                "inspect_materialization": AsyncMock(return_value={}),
            },
        )()

        with (
            patch("comet.playback.manager.MAX_USENET_LOGICAL_BYTES", 100),
            patch("comet.playback.manager.log.info"),
        ):
            result = await _repair_direct_asset(
                engine,
                manifest,
                assets,
                UsenetAsset(b"v" * 32, 0, "Movie.mkv", 8),
                EngineNntpError("nntp_article_missing"),
                (0,),
                engine_servers=[{"provider_configuration_id": "primary"}],
                account_partition=b"a" * 32,
                provider_set_generation="f" * 64,
            )

        self.assertEqual(result[:3], (repaired_identity, 8, repaired_identity))
        self.assertEqual(
            [call.args[0] for call in engine.repair_par2.await_args_list],
            [
                [
                    (index_identity, "release.par2", 1),
                    (small_identity, "release.vol00+01.par2", 40),
                ],
                [
                    (index_identity, "release.par2", 1),
                    (large_identity, "release.vol01+02.par2", 80),
                ],
            ],
        )

    async def test_native_archive_preparation_retains_mapped_target_closure(self):
        preparation_id, candidate_id, provider_id, locator_id = (
            str(uuid.uuid4()) for _ in range(4)
        )
        artifact_sha256 = "a" * 64
        recovery_identity = "b" * 64
        repaired_identity = "c" * 64
        output_identity = "d" * 64
        archive_set_identity = "e" * 64
        par2_set_id = "1" * 32
        source_file_id = "2" * 32
        artifact = type(
            "Artifact",
            (),
            {
                "nh1": "nh1:" + "1" * 40,
                "nm1": "nm1:" + "f" * 64,
                "metadata": {},
                "manifest": [
                    {
                        "subject": '"release.rar" yEnc',
                        "groups": ["alt.video"],
                        "postings": [
                            {
                                "number": 1,
                                "bytes": 100,
                                "message_id": "missing@example.test",
                            }
                        ],
                    },
                    {
                        "subject": '"release.par2" yEnc',
                        "groups": ["alt.video"],
                        "postings": [
                            {
                                "number": 1,
                                "bytes": 10,
                                "message_id": "par2@example.test",
                            }
                        ],
                    },
                    {
                        "subject": '"4f6a9d2c" yEnc',
                        "groups": ["alt.video"],
                        "postings": [
                            {
                                "number": 1,
                                "bytes": 100,
                                "message_id": "opaque@example.test",
                            }
                        ],
                    },
                ],
            },
        )()
        broker = type(
            "Broker",
            (),
            {"resolve_owned_artifact": AsyncMock(return_value=artifact)},
        )()
        server = type(
            "Server",
            (),
            {
                "name": "primary",
                "backup": False,
                "priority": 0,
                "host": "news.example.test",
                "port": 563,
                "tls_mode": "implicit",
                "username": "user",
                "password": "secret",
                "connections": 2,
                "pipeline": 1,
            },
        )()
        provider = type(
            "Provider",
            (),
            {
                "descriptor": type("Descriptor", (), {"kind": "comet_native_usenet"})(),
                "servers_for": staticmethod(lambda _options: (server,)),
            },
        )()
        prepared = type(
            "Prepared",
            (),
            {
                "resolution": type(
                    "Resolution",
                    (),
                    {
                        "provider": provider,
                        "account_partition": b"c" * 32,
                        "provider_options": {"source": "instance_pool"},
                        "release": type(
                            "Release",
                            (),
                            {
                                "locators": (
                                    {
                                        "kind": "nzb_artifact",
                                        "payload": {"artifact_sha256": artifact_sha256},
                                    },
                                )
                            },
                        )(),
                    },
                )(),
                "preparation": PlaybackPreparation(
                    preparation_id,
                    candidate_id,
                    provider_id,
                    "comet_native_usenet",
                    (locator_id,),
                    (0,),
                    "stremio",
                    "pending",
                    None,
                    None,
                    1,
                ),
            },
        )()
        archive_plan = {
            "set_identity": archive_set_identity,
            "kind": {"layout": "single_archive", "format": "rar5"},
            "exact_size": 100,
            "volumes": [
                {
                    "content_identity": repaired_identity,
                    "relative_path": "release.rar",
                    "number": 0,
                    "exact_size": 100,
                }
            ],
        }
        member = _archive_member(archive_set_identity, "Movie.2026.mkv", exact_size=42)
        inspection = {
            "version": 1,
            "raw_composite_id": output_identity,
            "source_identity": output_identity,
            "inspection_state": "provisionally_streamable",
            "container": "matroska",
            "duration_millis": None,
            "inspected_head_bytes": 42,
            "inspected_tail_bytes": 0,
        }
        engine = type(
            "Engine",
            (),
            {
                "catalog_nntp_artifact": AsyncMock(
                    return_value=[
                        _native_catalog_asset(
                            artifact_sha256,
                            0,
                            "release.rar",
                            100,
                            kind="archive",
                        ),
                        _native_catalog_asset(
                            artifact_sha256,
                            1,
                            "release.par2",
                            10,
                            kind="par2",
                        ),
                        _native_catalog_asset(
                            artifact_sha256,
                            2,
                            "opaque.rar",
                            99,
                            kind="archive",
                        ),
                    ]
                ),
                "materialize_nntp_postings": AsyncMock(
                    side_effect=[
                        EngineNntpError("nntp_article_missing"),
                        (repaired_identity, 100, "7" * 64),
                        (recovery_identity, 10, "8" * 64),
                    ]
                ),
                "open_nntp_session": AsyncMock(
                    side_effect=EngineNntpError("nntp_article_missing")
                ),
                "stats": AsyncMock(
                    return_value={
                        "requests_active": 1,
                        "request_workers": 32,
                        "nntp_preparation_slots": 8,
                    }
                ),
                "discover_par2_sets": AsyncMock(
                    return_value=[
                        {
                            "set_id": par2_set_id,
                            "slice_size": 1024,
                            "files": [
                                {
                                    "file_id": source_file_id,
                                    "relative_path": "release.rar",
                                    "exact_size": 100,
                                    "full_md5": "3" * 32,
                                    "first_16k_md5": "4" * 32,
                                    "slice_count": 1,
                                }
                            ],
                            "recovery_exponents": [0],
                            "volume_content_identities": [recovery_identity],
                        }
                    ]
                ),
                "map_par2_sources": AsyncMock(
                    return_value={
                        "version": 1,
                        "set_id": par2_set_id,
                        "slice_size": 1024,
                        "mappings": [
                            {
                                "content_identity": repaired_identity,
                                "file_id": source_file_id,
                                "relative_path": "release.rar",
                                "exact_size": 100,
                                "slice_count": 1,
                            }
                        ],
                    }
                ),
                "repair_par2": AsyncMock(
                    return_value={
                        "version": 1,
                        "set_id": par2_set_id,
                        "file_id": source_file_id,
                        "relative_path": "release.rar",
                        "identity": repaired_identity,
                        "byte_size": 100,
                        "asset_revision": "9" * 64,
                        "partial_source_mapped": True,
                    }
                ),
                "plan_archive_volumes": AsyncMock(return_value=archive_plan),
                "catalog_stored_archive_volumes": AsyncMock(
                    return_value=(archive_plan, [member])
                ),
                "open_stored_archive_member": AsyncMock(
                    return_value=(archive_plan, output_identity, 42, "f" * 64)
                ),
                "inspect_raw_composite": AsyncMock(return_value=inspection),
            },
        )()

        with (
            patch(
                "comet.playback.manager.PlaybackPreparationRepository.mark_ready",
                AsyncMock(),
            ) as marked,
            patch(
                "comet.playback.manager.MaterializedArtifactRepository."
                "register_for_preparation",
                AsyncMock(),
            ) as registered,
            patch(
                "comet.playback.manager.MaterializedArtifactRepository."
                "acquire_publication_lease",
                AsyncMock(
                    return_value=type(
                        "PublicationLease",
                        (),
                        {"close": AsyncMock()},
                    )()
                ),
            ) as acquired,
            patch(
                "comet.playback.manager.MaterializedArtifactRepository."
                "retain_for_preparation",
                AsyncMock(),
            ) as retained,
        ):
            result = await prepare_native_usenet(
                prepared,
                broker,
                object(),
                engine,
                owner_configuration_partition=b"a" * 32,
            )

        self.assertEqual(result, (output_identity, 42, "f" * 64))
        self.assertEqual(acquired.await_count, 3)
        self.assertEqual(registered.await_count, 1)
        self.assertEqual(
            registered.await_args.kwargs["artifacts"][0].artifact_sha256,
            repaired_identity,
        )
        retained.assert_awaited_once_with(
            preparation_id,
            owner_configuration_partition=b"a" * 32,
            artifact_sha256s=(repaired_identity,),
        )
        engine.map_par2_sources.assert_awaited_once_with(
            [(recovery_identity, "release.par2", 10)],
            [(repaired_identity, "opaque.rar", 100)],
            recovery_set_id=par2_set_id,
        )
        engine.repair_par2.assert_not_awaited()
        self.assertNotIn("par2_repair", marked.await_args.kwargs["target_ref"])

    async def test_nzbdav_poll_promotes_only_webdav_validated_movie_files(self):
        preparation_id, candidate_id, provider_id, locator_id, ledger_id = (
            str(uuid.uuid4()) for _ in range(5)
        )
        artifact_sha256 = "a" * 64
        status = type(
            "Status",
            (),
            {"readiness": Readiness.REQUIRES_PREPARE, "code": None},
        )()
        job = type(
            "Job",
            (),
            {
                "status": status,
                "verified_name": "comet-" + artifact_sha256,
                "observed": True,
            },
        )()
        selected = type(
            "Selected", (), {"relative_path": "video.mkv", "byte_size": 42}
        )()
        provider = type(
            "Provider",
            (),
            {
                "descriptor": type("Descriptor", (), {"kind": "nzbdav"})(),
                "poll_artifact": AsyncMock(return_value=job),
                "completed_file": AsyncMock(return_value=selected),
            },
        )()
        release = type(
            "Release",
            (),
            {
                "locators": (
                    {
                        "kind": "real_nzb",
                        "payload": {
                            "adapter_configuration_id": "origin",
                            "remote_guid": "opaque-guid",
                        },
                    },
                )
            },
        )()
        resolution = type(
            "Resolution",
            (),
            {
                "provider": provider,
                "account_partition": b"c" * 32,
                "provider_options": {"value": "safe"},
                "release": release,
            },
        )()
        preparation = PlaybackPreparation(
            preparation_id,
            candidate_id,
            provider_id,
            "nzbdav",
            (locator_id,),
            (0,),
            "stremio",
            "pending",
            "cloud",
            {
                "provider_preparation_id": ledger_id,
                "remote_job_id": "job-id",
                "artifact_sha256": artifact_sha256,
                "category": "movies",
            },
            1,
        )
        prepared = type(
            "Prepared", (), {"preparation": preparation, "resolution": resolution}
        )()
        with (
            patch(
                "comet.playback.manager.PlaybackPreparationRepository.mark_ready",
                AsyncMock(),
            ) as marked,
            patch(
                "comet.playback.manager.ProviderPreparationRepository.record_poll",
                AsyncMock(),
            ) as record_poll,
            patch(
                "comet.playback.manager.ProviderPreparationRepository."
                "clear_sab_absence",
                AsyncMock(return_value=True),
            ) as clear_absence,
            patch(
                "comet.playback.manager.ProviderPreparationRepository."
                "record_selected_file",
                AsyncMock(),
            ) as record_file,
        ):
            result = await poll_nzbdav(
                prepared, object(), owner_configuration_partition=b"a" * 32
            )

        self.assertEqual(result, "ready")
        provider.poll_artifact.assert_awaited_once_with(
            {"value": "safe"},
            "job-id",
            artifact_sha256,
            "movies",
        )
        provider.completed_file.assert_awaited_once_with(
            {"value": "safe"},
            "comet-" + artifact_sha256,
            "movies",
            (0,),
        )
        record_poll.assert_awaited_once_with(
            ledger_id,
            owner_configuration_partition=b"a" * 32,
        )
        clear_absence.assert_awaited_once_with(
            ledger_id,
            owner_configuration_partition=b"a" * 32,
        )
        record_file.assert_awaited_once_with(
            ledger_id,
            owner_configuration_partition=b"a" * 32,
            remote_id="job-id",
            remote_hash=artifact_sha256,
            file_index=0,
            file_size=42,
            locked_link="video.mkv",
        )
        self.assertEqual(
            marked.await_args.kwargs["target_ref"],
            {
                "provider_preparation_id": ledger_id,
                "verified_name": "comet-" + artifact_sha256,
                "relative_path": "video.mkv",
                "byte_size": 42,
                "category": "movies",
            },
        )
        provider.poll_artifact.return_value = type(
            "Job",
            (),
            {
                "status": type(
                    "Status",
                    (),
                    {
                        "readiness": Readiness.TERMINAL_FAILURE,
                        "code": "nzbdav_credentials_rejected",
                    },
                )(),
                "verified_name": None,
                "observed": False,
            },
        )()
        with (
            patch(
                "comet.playback.manager.ProviderPreparationRepository.record_poll",
                AsyncMock(),
            ),
            patch(
                "comet.playback.manager.ProviderPreparationRepository."
                "record_terminal_status",
                AsyncMock(),
            ) as terminal,
            patch(
                "comet.playback.manager.PlaybackPreparationRepository.mark_failed",
                AsyncMock(),
            ) as failed,
            self.assertRaisesRegex(
                RuntimeError,
                "nzbdav_credentials_rejected",
            ),
        ):
            await poll_nzbdav(
                prepared,
                object(),
                owner_configuration_partition=b"a" * 32,
            )

        terminal.assert_awaited_once_with(
            ledger_id,
            owner_configuration_partition=b"a" * 32,
            status="credentials_rejected",
        )
        failed.assert_awaited_once_with(
            preparation_id,
            owner_configuration_partition=b"a" * 32,
            provider_account_partition=b"c" * 32,
            code="nzbdav_credentials_rejected",
        )
        provider.poll_artifact.return_value = type(
            "Job",
            (),
            {
                "status": type(
                    "Status",
                    (),
                    {
                        "readiness": Readiness.TERMINAL_FAILURE,
                        "code": "remote_item_missing",
                    },
                )(),
                "verified_name": None,
                "observed": False,
            },
        )()
        with (
            patch(
                "comet.playback.manager.ProviderPreparationRepository.record_poll",
                AsyncMock(),
            ),
            patch(
                "comet.playback.manager.ProviderPreparationRepository."
                "record_sab_absence",
                AsyncMock(side_effect=["pending", "terminal"]),
            ) as absence,
            patch(
                "comet.playback.manager.PlaybackPreparationRepository.mark_failed",
                AsyncMock(),
            ) as missing_failed,
        ):
            first_missing = await poll_nzbdav(
                prepared,
                object(),
                owner_configuration_partition=b"a" * 32,
            )
            second_missing = await poll_nzbdav(
                prepared,
                object(),
                owner_configuration_partition=b"a" * 32,
            )

        self.assertEqual((first_missing, second_missing), ("pending", "failed"))
        self.assertEqual(absence.await_count, 2)
        missing_failed.assert_awaited_once_with(
            preparation_id,
            owner_configuration_partition=b"a" * 32,
            provider_account_partition=b"c" * 32,
            code="remote_item_missing",
        )
        provider.poll_artifact.return_value = job
        provider.completed_file.side_effect = RuntimeError("unexpected_provider_error")
        with (
            patch(
                "comet.playback.manager.ProviderPreparationRepository.record_poll",
                AsyncMock(),
            ),
            patch(
                "comet.playback.manager.ProviderPreparationRepository."
                "clear_sab_absence",
                AsyncMock(return_value=True),
            ),
            self.assertRaisesRegex(RuntimeError, "unexpected_provider_error"),
        ):
            await poll_nzbdav(
                prepared,
                object(),
                owner_configuration_partition=b"a" * 32,
            )

    async def test__nzbdav_download_url_is_reconstructed_only_from_a_ready_record(
        self,
    ):
        preparation_id, candidate_id, provider_id, locator_id = (
            str(uuid.uuid4()) for _ in range(4)
        )
        provider = type(
            "Provider",
            (),
            {
                "descriptor": type("Descriptor", (), {"kind": "nzbdav"})(),
                "direct_download_url": staticmethod(
                    lambda *_args: "https://bridge.example/content/file.mkv"
                ),
            },
        )()
        release = type("Release", (), {"title": "Example"})()
        resolution = type(
            "Resolution",
            (),
            {
                "provider": provider,
                "account_partition": b"c" * 32,
                "provider_options": {"safe": True},
                "release": release,
            },
        )()
        preparation = PlaybackPreparation(
            preparation_id,
            candidate_id,
            provider_id,
            "nzbdav",
            (locator_id,),
            (0,),
            "stremio",
            "ready",
            "cloud",
            {
                "remote_job_id": "job",
                "verified_name": "comet-" + "a" * 64,
                "relative_path": "video.mkv",
                "byte_size": 42,
                "category": "movies",
            },
            1,
        )
        prepared = type(
            "Prepared", (), {"preparation": preparation, "resolution": resolution}
        )()

        self.assertEqual(
            await _nzbdav_download_url(prepared),
            "https://bridge.example/content/file.mkv",
        )
        foreign_metadata = type(
            "Prepared",
            (),
            {
                "preparation": replace(
                    preparation,
                    target_ref={
                        **preparation.target_ref,
                        "byte_size": MAX_SIGNED_BIGINT + 1,
                    },
                ),
                "resolution": resolution,
            },
        )()
        self.assertEqual(
            await _nzbdav_download_url(foreign_metadata),
            "https://bridge.example/content/file.mkv",
        )

    async def test_altmount_preparation_persists_only_the_selected_virtual_path(self):
        preparation_id, candidate_id, provider_id, locator_id = (
            str(uuid.uuid4()) for _ in range(4)
        )
        artifact_sha256 = "a" * 64
        broker = type(
            "Broker",
            (),
            {
                "resolve_owned_artifact": AsyncMock(
                    return_value=type(
                        "Owned",
                        (),
                        {"grant_id": str(uuid.uuid4())},
                    )()
                ),
                "read_owned_artifact": AsyncMock(return_value=b"<nzb/>"),
            },
        )()
        selected = type(
            "Selected",
            (),
            {"virtual_path": "video.mkv"},
        )()
        submission = type(
            "Submission",
            (),
            {
                "virtual_paths": ("video.mkv",),
            },
        )()
        provider = type(
            "Provider",
            (),
            {
                "descriptor": type("Descriptor", (), {"kind": "altmount"})(),
                "category_for": staticmethod(lambda _options: "stremio"),
                "preparation_deadline": staticmethod(lambda: 300),
                "credential_binding": staticmethod(
                    lambda _options: ("https://altmount.example", b"key")
                ),
                "submit_artifact": AsyncMock(return_value=submission),
                "select_file": staticmethod(lambda *_args: selected),
            },
        )()
        release = type(
            "Release",
            (),
            {
                "locators": (
                    {
                        "kind": "nzb_artifact",
                        "locator_id": locator_id,
                        "payload": {"artifact_sha256": artifact_sha256},
                    },
                ),
                "title": "Example",
            },
        )()
        resolution = type(
            "Resolution",
            (),
            {
                "provider": provider,
                "account_partition": b"c" * 32,
                "provider_options": {},
                "release": release,
            },
        )()
        preparation = PlaybackPreparation(
            preparation_id,
            candidate_id,
            provider_id,
            "altmount",
            (locator_id,),
            (0,),
            "stremio",
            "pending",
            None,
            None,
            1,
        )
        prepared = type(
            "Prepared", (), {"preparation": preparation, "resolution": resolution}
        )()
        ledger = ProviderPreparation(str(uuid.uuid4()), "mutation_pending", {}, 0)
        with (
            patch(
                "comet.playback.manager.settings.COMET_CAPABILITY_SECRET",
                ROOT,
            ),
            patch(
                "comet.playback.manager.ProviderPreparationRepository.get_existing",
                AsyncMock(return_value=None),
            ) as get_existing,
            patch(
                "comet.playback.manager.ProviderPreparationRepository.get_or_create",
                AsyncMock(return_value=(ledger, True)),
            ),
            patch(
                "comet.playback.manager.ProviderPreparationRepository."
                "record_altmount_selection",
                AsyncMock(),
            ) as sealed,
            patch(
                "comet.playback.manager.PlaybackPreparationRepository.mark_ready",
                AsyncMock(),
            ) as marked,
        ):
            result = await prepare_altmount(
                prepared, broker, object(), owner_configuration_partition=b"a" * 32
            )
            get_existing.return_value = ProviderPreparation(
                ledger.preparation_id,
                "terminal",
                {
                    "virtual_path": "video.mkv",
                    "status": "selected",
                    "foreign_metadata": object(),
                },
                0,
            )
            repeated = await prepare_altmount(
                prepared,
                broker,
                object(),
                owner_configuration_partition=b"a" * 32,
            )

        self.assertEqual(result, "ready")
        self.assertEqual(repeated, "ready")
        self.assertEqual(marked.await_count, 2)
        sealed.assert_awaited_once()
        broker.read_owned_artifact.assert_awaited_once()

    async def test__altmount_download_url_reconstructs_the_ephemeral_download_key(
        self,
    ):
        preparation_id, candidate_id, provider_id, locator_id = (
            str(uuid.uuid4()) for _ in range(4)
        )
        provider = type(
            "Provider",
            (),
            {
                "descriptor": type("Descriptor", (), {"kind": "altmount"})(),
                "stream_url": staticmethod(
                    lambda _options, _path: (
                        "https://bridge.example/stream?path=video.mkv"
                    )
                ),
            },
        )()
        release = type("Release", (), {"title": "Example"})()
        resolution = type(
            "Resolution",
            (),
            {
                "provider": provider,
                "account_partition": b"c" * 32,
                "provider_options": {},
                "release": release,
            },
        )()
        preparation = PlaybackPreparation(
            preparation_id,
            candidate_id,
            provider_id,
            "altmount",
            (locator_id,),
            (0,),
            "stremio",
            "ready",
            "cloud",
            {
                "virtual_path": "video.mkv",
            },
            1,
        )
        prepared = type(
            "Prepared", (), {"preparation": preparation, "resolution": resolution}
        )()

        self.assertEqual(
            await _altmount_download_url(prepared),
            "https://bridge.example/stream?path=video.mkv",
        )

    async def test_easynews_preparation_redirects_without_probing(self):
        preparation_id, candidate_id, provider_id, locator_id = (
            str(uuid.uuid4()) for _ in range(4)
        )
        locator = {
            "account_configuration_id": provider_id,
            "file_identifier": "file-1",
        }
        provider = type(
            "Provider",
            (),
            {
                "descriptor": type("Descriptor", (), {"kind": "easynews"})(),
                "playback_url": staticmethod(
                    lambda payload: (
                        "https://member:secret@members.easynews.com/dl/"
                        + payload["file_identifier"]
                    )
                ),
            },
        )()
        prepared = type(
            "Prepared",
            (),
            {
                "preparation": PlaybackPreparation(
                    preparation_id,
                    candidate_id,
                    provider_id,
                    "easynews",
                    (locator_id,),
                    (0,),
                    "stremio",
                    "pending",
                    None,
                    None,
                    1,
                ),
                "resolution": type(
                    "Resolution",
                    (),
                    {
                        "provider": provider,
                        "account_partition": b"c" * 32,
                        "release": type(
                            "Release",
                            (),
                            {
                                "locators": (
                                    {"kind": "easynews_http", "payload": locator},
                                )
                            },
                        )(),
                    },
                )(),
            },
        )()

        with patch(
            "comet.playback.manager.PlaybackPreparationRepository.mark_ready",
            AsyncMock(),
        ) as marked:
            result = await prepare_easynews(
                prepared,
                object(),
                owner_configuration_partition=b"a" * 32,
            )

        self.assertEqual(result, "ready")
        marked.assert_awaited_once()
        self.assertEqual(
            marked.await_args.kwargs["target_ref"],
            {"file_identifier": "file-1"},
        )

        prepared.preparation = replace(
            prepared.preparation,
            state="ready",
            target_kind="cloud",
            target_ref={"file_identifier": "file-1"},
        )
        self.assertEqual(
            await _easynews_download_url(prepared),
            "https://member:secret@members.easynews.com/dl/file-1",
        )

    async def test_torbox_poll_resumes_from_the_durable_provider_binding(self):
        preparation_id, candidate_id, provider_id, locator_id, ledger_id = (
            str(uuid.uuid4()) for _ in range(5)
        )
        item = type("Item", (), {"usenet_id": 7})()
        selected = type("File", (), {"file_id": 3, "size": 42})()
        provider = type(
            "Provider",
            (),
            {
                "descriptor": type("Descriptor", (), {"kind": "torbox_usenet"})(),
                "get_item": AsyncMock(return_value=item),
                "status": staticmethod(
                    lambda _item: type("Status", (), {"readiness": Readiness.READY})()
                ),
                "select_file": staticmethod(lambda _item, _selection: selected),
                "request_download": AsyncMock(
                    return_value=TorBoxDownloadTarget(
                        "https://weur.tb-cdn.io/file?token=s&expires=1",
                    )
                ),
            },
        )()
        artifact_sha256 = "a" * 64
        release = type(
            "Release",
            (),
            {
                "title": "Example",
                "locators": (
                    {
                        "kind": "real_nzb",
                        "payload": {
                            "adapter_configuration_id": "nzbgeek",
                            "remote_guid": "release",
                        },
                    },
                ),
            },
        )()
        resolution = type(
            "Resolution",
            (),
            {"provider": provider, "account_partition": b"c" * 32, "release": release},
        )()
        preparation = PlaybackPreparation(
            preparation_id,
            candidate_id,
            provider_id,
            "torbox_usenet",
            (locator_id,),
            (0,),
            "stremio",
            "pending",
            "cloud",
            {
                "provider_preparation_id": ledger_id,
                "usenet_id": 7,
            },
            1,
        )
        prepared = type(
            "Prepared", (), {"preparation": preparation, "resolution": resolution}
        )()
        ledger = ProviderPreparation(
            ledger_id,
            "submitted",
            {
                "remote_id": "7",
                "remote_hash": artifact_sha256,
                "status": "downloaded",
                "ownership": "created",
                "missing_count": 0,
            },
            1,
        )
        with (
            patch(
                "comet.playback.manager.PlaybackPreparationRepository.mark_ready",
                AsyncMock(),
            ) as marked,
            patch(
                "comet.playback.manager.ProviderPreparationRepository.record_poll",
                AsyncMock(return_value=ledger),
            ) as recorded_poll,
            patch(
                "comet.playback.manager.ProviderPreparationRepository."
                "record_selected_file",
                AsyncMock(),
            ) as recorded_file,
        ):
            self.assertEqual(
                await poll_torbox_usenet(
                    prepared, object(), owner_configuration_partition=b"a" * 32
                ),
                "ready",
            )
        recorded_poll.assert_awaited_once_with(
            ledger_id,
            owner_configuration_partition=b"a" * 32,
        )
        recorded_file.assert_awaited_once_with(
            ledger_id,
            owner_configuration_partition=b"a" * 32,
            remote_id="7",
            remote_hash=artifact_sha256,
            file_index=3,
            file_size=42,
            locked_link=None,
        )
        self.assertEqual(
            marked.await_args.kwargs["target_ref"],
            {
                "provider_preparation_id": ledger_id,
                "usenet_id": 7,
                "file_id": 3,
                "byte_size": 42,
            },
        )
        provider.select_file = lambda *_args: (_ for _ in ()).throw(
            RuntimeError("unexpected_provider_error")
        )
        with (
            patch(
                "comet.playback.manager.ProviderPreparationRepository.record_poll",
                AsyncMock(return_value=ledger),
            ),
            patch(
                "comet.playback.manager.ProviderPreparationRepository."
                "record_terminal_status",
                AsyncMock(),
            ) as terminal,
            patch(
                "comet.playback.manager.PlaybackPreparationRepository.mark_failed",
                AsyncMock(),
            ) as failed,
            self.assertRaisesRegex(RuntimeError, "unexpected_provider_error"),
        ):
            await poll_torbox_usenet(
                prepared,
                object(),
                owner_configuration_partition=b"a" * 32,
            )
        terminal.assert_not_awaited()
        failed.assert_not_awaited()

        ready = PlaybackPreparation(
            preparation_id,
            candidate_id,
            provider_id,
            "torbox_usenet",
            (locator_id,),
            (0,),
            "stremio",
            "ready",
            "cloud",
            {
                "provider_preparation_id": ledger_id,
                "usenet_id": 7,
                "file_id": 3,
                "byte_size": 42,
            },
            1,
        )
        self.assertEqual(
            await _torbox_download_url(
                type(
                    "Prepared",
                    (),
                    {"preparation": ready, "resolution": resolution},
                )()
            ),
            "https://weur.tb-cdn.io/file?token=s&expires=1",
        )
        provider.request_download.assert_awaited_once_with(7, file_id=3)
