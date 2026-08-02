#!/usr/bin/env bash
set -euo pipefail

repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if (($# > 1)); then
    echo "usage: run_usenet_stress.sh [SECONDS: 5..900]" >&2
    exit 2
fi
seconds="${1:-120}"

case "${seconds}" in
    *[!0-9]* | "")
        echo "usage: run_usenet_stress.sh [SECONDS: 5..900]" >&2
        exit 2
        ;;
esac
seconds=$((10#${seconds}))
if ((seconds < 5 || seconds > 900)); then
    echo "stress duration must be between 5 and 900 seconds" >&2
    exit 2
fi

cd "${repository_root}"
USENET_STRESS_SECONDS="${seconds}" \
    cargo test \
        --release \
        --locked \
        --manifest-path native/usenet-engine/Cargo.toml \
        --features quality-gates \
        quality_stress_native_lifecycle_and_resource_plateaus \
        -- \
        --ignored \
        --nocapture \
        --test-threads=1
