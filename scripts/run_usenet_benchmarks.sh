#!/usr/bin/env bash
set -euo pipefail

repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if (($# != 1)); then
    echo "usage: run_usenet_benchmarks.sh ABSOLUTE_DEPENDENCY_DIRECTORY" >&2
    exit 2
fi
quality_root="$1"

case "${quality_root}" in
    /*) ;;
    *) echo "quality dependency directory must be absolute" >&2; exit 2 ;;
esac
par2_binary="${quality_root}/par2/bin/par2"
libarchive_library="${quality_root}/libarchive/lib/libarchive.so.13.8.8"

test -x "${par2_binary}"
test -f "${libarchive_library}"

cd "${repository_root}"
uv run python scripts/run_usenet_search_benchmark.py
USENET_PAR2_BINARY="${par2_binary}" \
USENET_LIBARCHIVE_LIBRARY="${libarchive_library}" \
    cargo test \
        --release \
        --locked \
        --manifest-path native/usenet-engine/Cargo.toml \
        --features quality-gates \
        quality_benchmark_ \
        -- \
        --ignored \
        --nocapture \
        --test-threads=1
