from pathlib import Path


def test_runtime_image_copies_the_native_engine_without_a_rust_toolchain():
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")

    assert (
        "RUN cargo build --release --manifest-path native/usenet-engine/Cargo.toml"
        in dockerfile
    )
    runtime = dockerfile.split("FROM python:3.13-slim-trixie AS runtime", 1)[1]
    assert (
        "COPY --from=builder /app/native/usenet-engine/target/release/usenet-engine /app/native/usenet-engine"
        in runtime
    )
    assert "COPY --from=par2-tool /opt/par2/bin/par2 /app/bin/par2" in runtime
    assert "/usr/share/doc/par2cmdline-turbo" in runtime
    assert "/usr/local/cargo" not in runtime
    assert "/usr/local/rustup" not in runtime


def test_par2_image_stage_performs_a_build_time_exact_version_check():
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")

    par2_stage = dockerfile.split("FROM python:3.13-slim-trixie AS par2-tool", 1)[1]
    par2_stage = par2_stage.split("FROM python:3.13-slim-trixie AS builder", 1)[0]
    assert '--arch "${TARGETARCH}"' in par2_stage
    assert (
        'test "$(/opt/par2/bin/par2 -V)" = "par2cmdline-turbo version 1.4.0"'
        in par2_stage
    )
