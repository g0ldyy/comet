#!/usr/bin/env bash
set -euo pipefail

engine_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/../native/usenet-engine" && pwd)"
fuzz_seconds="${USENET_FUZZ_SECONDS:-60}"
fuzz_toolchain="${USENET_FUZZ_TOOLCHAIN:-nightly-2025-10-24}"
if (($# != 0)); then
    echo "usage: run_usenet_fuzz.sh" >&2
    exit 2
fi
fuzz_workspace="$(mktemp -d)"

cleanup() {
    find "${fuzz_workspace}" -depth -delete
}
trap cleanup EXIT

case "${fuzz_seconds}" in
    *[!0-9]* | "")
        echo "USENET_FUZZ_SECONDS must be an integer between 1 and 3600" >&2
        exit 2
        ;;
esac
fuzz_seconds=$((10#${fuzz_seconds}))
if ((fuzz_seconds < 1 || fuzz_seconds > 3600)); then
    echo "USENET_FUZZ_SECONDS must be an integer between 1 and 3600" >&2
    exit 2
fi

mkdir -p "${fuzz_workspace}/artifacts"

for target in nzb_parse yenc_decode nntp_protocol archive_detect par2_parse; do
    corpus="${fuzz_workspace}/${target}"
    log="${fuzz_workspace}/${target}.log"
    cp -R "${engine_dir}/fuzz/corpus/${target}" "${corpus}"
    if (
        cd "${engine_dir}"
        cargo "+${fuzz_toolchain}" fuzz run "${target}" "${corpus}" -- \
            "-max_total_time=${fuzz_seconds}" \
            -max_len=1048576 \
            -rss_limit_mb=2048 \
            -timeout=10 \
            "-artifact_prefix=${fuzz_workspace}/artifacts/" \
            -print_final_stats=1 \
            -verbosity=0
    ) >"${log}" 2>&1; then
        grep -E "^INFO: (Running|seed corpus)|^stat::" "${log}" || true
    else
        cat "${log}" >&2
        exit 1
    fi
done
