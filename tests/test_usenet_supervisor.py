import asyncio
import json
import os
import stat
from unittest.mock import AsyncMock, patch

import pytest

from comet.usenet.engine_transport import EngineUnavailable
from comet.usenet.supervisor import EngineSupervisor


def test_engine_supervisor_owns_a_distinct_secure_local_data_directory(tmp_path):
    runtime = tmp_path / "runtime"
    local_data = tmp_path / "local-data"
    supervisor = EngineSupervisor(str(runtime), str(local_data), "/engine")

    supervisor.prepare_runtime_dir()

    assert local_data.is_dir()
    assert local_data.stat().st_mode & 0o777 == 0o700
    assert supervisor.engine_command() == [
        "/engine",
        "--socket",
        str(runtime / "engine.sock"),
        "--local-data-dir",
        str(local_data),
        "--artifact-dir",
        str(local_data),
        "--memory-cache-bytes",
        "268435456",
        "--disk-cache-bytes",
        "2147483648",
        "--minimum-free-disk-bytes",
        "5368709120",
        "--maximum-nntp-connections",
        "32",
        "--spool-max-bytes",
        "107374182400",
        "--archive-jobs",
        "2",
        "--repair-jobs",
        "1",
        "--par2-binary",
        "/app/bin/par2",
        "--libarchive-library",
        "/app/lib/libarchive.so.13",
    ]
    supervisor.descriptor_path.write_text("{}")
    supervisor.close()
    assert not supervisor.descriptor_path.exists()
    assert supervisor._lock_fd is None


def test_engine_supervisor_cannot_prepare_twice_without_releasing_ownership(tmp_path):
    supervisor = EngineSupervisor(
        str(tmp_path / "runtime"),
        str(tmp_path / "local-data"),
        "/engine",
    )
    supervisor.prepare_runtime_dir()

    try:
        with pytest.raises(RuntimeError, match="already prepared"):
            supervisor.prepare_runtime_dir()
    finally:
        supervisor.close()


def test_competing_supervisor_cannot_withdraw_the_live_descriptor(tmp_path):
    runtime = tmp_path / "runtime"
    local_data = tmp_path / "local-data"
    owner = EngineSupervisor(str(runtime), str(local_data), "/engine")
    contender = EngineSupervisor(str(runtime), str(local_data), "/engine")
    owner.prepare_runtime_dir()
    owner.descriptor_path.write_text("live", encoding="utf-8")

    try:
        with pytest.raises(RuntimeError, match="already owns"):
            contender.prepare_runtime_dir()
        assert owner.descriptor_path.read_text(encoding="utf-8") == "live"
        assert contender._lock_fd is None
    finally:
        owner.close()


def test_engine_supervisor_rejects_an_unrepresentable_unix_socket_path(tmp_path):
    runtime = tmp_path / ("x" * 100)
    supervisor = EngineSupervisor(
        str(runtime),
        str(tmp_path / "local-data"),
        "/engine",
    )
    assert len(os.fsencode(supervisor.socket_path)) > 107

    with pytest.raises(ValueError, match="socket path is invalid"):
        supervisor.prepare_runtime_dir()

    assert not runtime.exists()


def test_engine_supervisor_does_not_follow_a_runtime_lock_symlink(tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    victim = tmp_path / "victim"
    victim.write_text("unchanged", encoding="utf-8")
    (runtime / "engine.lock").symlink_to(victim)
    supervisor = EngineSupervisor(
        str(runtime),
        str(tmp_path / "local-data"),
        "/engine",
    )

    with pytest.raises(OSError):
        supervisor.prepare_runtime_dir()

    assert victim.read_text(encoding="utf-8") == "unchanged"
    assert supervisor._lock_fd is None


def test_engine_descriptor_is_privately_and_atomically_published(tmp_path):
    runtime = tmp_path / "runtime"
    supervisor = EngineSupervisor(
        str(runtime),
        str(tmp_path / "local-data"),
        "/engine",
    )
    supervisor.prepare_runtime_dir()
    client = AsyncMock()

    async def scenario():
        with patch(
            "comet.usenet.supervisor.EngineClient",
            return_value=client,
        ):
            await supervisor.publish_descriptor()

    try:
        asyncio.run(scenario())
        payload = json.loads(supervisor.descriptor_path.read_bytes())
        assert payload == {
            "version": 1,
            "socket_path": str(runtime / "engine.sock"),
            "runtime_id": payload["runtime_id"],
            "api_version": 1,
        }
        assert stat.S_IMODE(supervisor.descriptor_path.stat().st_mode) == 0o600
        assert not tuple(runtime.glob(".engine-*.json"))
    finally:
        supervisor.close()


def test_engine_descriptor_temporary_is_removed_after_failed_health(tmp_path):
    runtime = tmp_path / "runtime"
    supervisor = EngineSupervisor(
        str(runtime),
        str(tmp_path / "local-data"),
        "/engine",
    )
    supervisor.prepare_runtime_dir()
    client = AsyncMock()
    client.health.side_effect = EngineUnavailable("unavailable")

    async def scenario():
        with (
            patch(
                "comet.usenet.supervisor.EngineClient",
                return_value=client,
            ),
            pytest.raises(EngineUnavailable),
        ):
            await supervisor.publish_descriptor(timeout=0)

    try:
        asyncio.run(scenario())
        assert not supervisor.descriptor_path.exists()
        assert not tuple(runtime.glob(".engine-*.json"))
    finally:
        supervisor.close()


def test_engine_descriptor_temporary_cannot_replace_an_existing_entry(tmp_path):
    runtime = tmp_path / "runtime"
    supervisor = EngineSupervisor(
        str(runtime),
        str(tmp_path / "local-data"),
        "/engine",
    )
    supervisor.prepare_runtime_dir()
    victim = tmp_path / "victim"
    victim.write_text("unchanged", encoding="utf-8")
    temporary = runtime / ".engine-fixed.json"
    temporary.symlink_to(victim)

    async def scenario():
        with (
            patch("comet.usenet.supervisor.secrets.token_hex", return_value="fixed"),
            pytest.raises(FileExistsError),
        ):
            await supervisor.publish_descriptor()

    try:
        asyncio.run(scenario())
        assert victim.read_text(encoding="utf-8") == "unchanged"
        assert temporary.is_symlink()
    finally:
        temporary.unlink()
        supervisor.close()


def test_parser_only_supervisor_does_not_enable_native_engine_routes(tmp_path):
    supervisor = EngineSupervisor(
        str(tmp_path / "runtime"),
        str(tmp_path / "local-data"),
        "/engine",
        parser_only=True,
    )

    assert supervisor.engine_command()[-1] == "--parser-only"
    assert "--par2-binary" not in supervisor.engine_command()
    assert "--libarchive-library" not in supervisor.engine_command()
    assert "--spool-max-bytes" not in supervisor.engine_command()
    assert "--archive-jobs" not in supervisor.engine_command()
    assert "--repair-jobs" not in supervisor.engine_command()


def test_supervisor_passes_the_shared_artifact_root_separately(tmp_path):
    local_data = tmp_path / "replica-local"
    artifact_data = tmp_path / "shared-artifacts"
    supervisor = EngineSupervisor(
        str(tmp_path / "runtime"),
        str(local_data),
        "/engine",
        artifact_dir=str(artifact_data),
    )

    command = supervisor.engine_command()

    assert command[command.index("--local-data-dir") + 1] == str(local_data)
    assert command[command.index("--artifact-dir") + 1] == str(artifact_data)


def test_supervisor_normalizes_relative_storage_roots_for_native_workers(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    supervisor = EngineSupervisor(
        "runtime",
        "replica-local",
        "/engine",
        artifact_dir="shared-artifacts",
    )

    command = supervisor.engine_command()

    assert command[command.index("--socket") + 1] == str(
        tmp_path / "runtime" / "engine.sock"
    )
    assert command[command.index("--local-data-dir") + 1] == str(
        tmp_path / "replica-local"
    )
    assert command[command.index("--artifact-dir") + 1] == str(
        tmp_path / "shared-artifacts"
    )


def test_native_supervisor_propagates_non_default_local_admission_limits(tmp_path):
    supervisor = EngineSupervisor(
        str(tmp_path / "runtime"),
        str(tmp_path / "local-data"),
        "/engine",
        maximum_spool_bytes=2147483648,
        maximum_archive_jobs=7,
        maximum_repair_jobs=4,
    )

    command = supervisor.engine_command()
    assert command[command.index("--spool-max-bytes") + 1] == "2147483648"
    assert command[command.index("--archive-jobs") + 1] == "7"
    assert command[command.index("--repair-jobs") + 1] == "4"


def test_engine_environment_binds_parent_death_without_forwarding_credentials():
    with (
        patch("comet.usenet.supervisor.os.getpid", return_value=1234),
        patch.dict(
            "comet.usenet.supervisor.os.environ",
            {"SECRET_VALUE": "must-not-cross", "PATH": "/safe/bin"},
            clear=True,
        ),
    ):
        supervisor = EngineSupervisor(
            "/runtime",
            "/data",
            "/engine",
            log_profile="verbose",
            log_format="json",
            no_color=True,
        )
        assert supervisor.next_engine_generation() == 1
        environment = supervisor.engine_environment()

    assert environment == {
        "PATH": "/safe/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "RUST_BACKTRACE": "0",
        "COMET_SUPERVISOR_PID": "1234",
        "LOG_PROFILE": "verbose",
        "LOG_FORMAT": "json",
        "COMET_ENGINE_GENERATION": "1",
        "NO_COLOR": "1",
    }
