#!/usr/bin/env bash
set -euo pipefail

repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if (($# != 1)); then
    echo "usage: prepare_usenet_quality_dependencies.sh ABSOLUTE_OUTPUT_DIRECTORY" >&2
    exit 2
fi
quality_root="$1"

case "${quality_root}" in
    /*) ;;
    *) echo "quality dependency directory must be absolute" >&2; exit 2 ;;
esac
if [[ -e "${quality_root}" ]]; then
    echo "quality dependency directory must not already exist" >&2
    exit 2
fi
mkdir -m 0755 "${quality_root}"
complete=0

cleanup() {
    if ((complete == 0)); then
        find "${quality_root}" -depth -delete
    fi
}
trap cleanup EXIT

case "$(uname -m)" in
    x86_64) target_arch="amd64" ;;
    aarch64 | arm64) target_arch="arm64" ;;
    *) echo "unsupported quality architecture: $(uname -m)" >&2; exit 2 ;;
esac

python "${repository_root}/deployment/install_par2.py" \
    --arch "${target_arch}" \
    --output "${quality_root}/par2"
python "${repository_root}/deployment/install_libarchive.py" \
    --output "${quality_root}/libarchive-build"

(
    cd "${quality_root}/libarchive-build/source"
    ./configure \
        --prefix="${quality_root}/libarchive" \
        --disable-static \
        --enable-shared \
        --disable-bsdtar \
        --disable-bsdcat \
        --disable-bsdcpio \
        --disable-bsdunzip \
        --disable-acl \
        --disable-xattr \
        --without-libb2 \
        --without-iconv \
        --without-openssl \
        --without-xml2 \
        --without-expat
    make -j2
    make install-strip
)

test "$("${quality_root}/par2/bin/par2" -V)" = "par2cmdline-turbo version 1.4.0"
python "${repository_root}/deployment/install_libarchive.py" \
    --verify-library "${quality_root}/libarchive/lib/libarchive.so.13.8.8"
find "${quality_root}/libarchive-build" -depth -delete
complete=1
