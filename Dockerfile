FROM ghcr.io/astral-sh/uv:0.11.32 AS uv

FROM node:24-bookworm-slim AS frontend-builder

WORKDIR /app/frontend

COPY frontend/package.json frontend/package-lock.json ./
RUN --mount=type=cache,target=/root/.npm,sharing=locked npm ci
COPY frontend ./
RUN npm run build:assets

FROM rust:1.97.1-slim-trixie AS rust-toolchain

FROM python:3.13-slim-trixie AS par2-tool

ARG TARGETARCH
COPY deployment/build_download.py deployment/install_par2.py /tmp/deployment/
RUN PYTHONPATH=/tmp python -m deployment.install_par2 --arch "${TARGETARCH}" --output /opt/par2 \
    && test "$(/opt/par2/bin/par2 -V)" = "par2cmdline-turbo version 1.4.0"

FROM python:3.13-slim-trixie AS libarchive-tool

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install \
        --yes \
        --no-install-recommends \
        gcc \
        libc6-dev \
        libbz2-dev \
        liblz4-dev \
        liblzma-dev \
        libzstd-dev \
        make \
        zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

COPY deployment/build_download.py deployment/install_libarchive.py /tmp/deployment/
RUN PYTHONPATH=/tmp python -m deployment.install_libarchive --output /opt/libarchive-build \
    && cd /opt/libarchive-build/source \
    && ./configure \
        --prefix=/opt/libarchive \
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
        --without-expat \
    && make -j"$(nproc)" \
    && make install-strip \
    && mkdir -p /opt/libarchive-runtime \
    && cp --dereference /opt/libarchive/lib/libarchive.so.13 /opt/libarchive-runtime/libarchive.so.13 \
    && PYTHONPATH=/tmp python -m deployment.install_libarchive --verify-library /opt/libarchive-runtime/libarchive.so.13 \
    && mkdir -p /opt/libarchive/share/doc \
    && cp -a /opt/libarchive-build/share/doc/libarchive /opt/libarchive/share/doc/

FROM python:3.13-slim-trixie AS python-builder

COPY --from=uv /uv /uvx /bin/
COPY --from=rust-toolchain /usr/local/cargo /usr/local/cargo
COPY --from=rust-toolchain /usr/local/rustup /usr/local/rustup

ENV CARGO_HOME=/usr/local/cargo \
    RUSTUP_HOME=/usr/local/rustup \
    PATH="/usr/local/cargo/bin:$PATH"

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install \
        --yes \
        --no-install-recommends \
        gcc \
        git \
        libc6-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml uv.lock ./

ARG TARGETPLATFORM
RUN --mount=type=cache,target=/root/.cache/uv,id=uv-${TARGETPLATFORM},sharing=locked uv sync --frozen --no-dev --no-install-project

FROM rust-toolchain AS usenet-builder

WORKDIR /app

COPY native/usenet-engine/Cargo.toml native/usenet-engine/Cargo.lock ./native/usenet-engine/
COPY native/usenet-engine/src ./native/usenet-engine/src
RUN cargo build --locked --release --manifest-path native/usenet-engine/Cargo.toml

FROM python:3.13-slim-trixie AS runtime

LABEL name="Comet" \
      description="Stremio's fastest torrent/debrid/usenet search add-on." \
      url="https://github.com/g0ldyy/comet"

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install \
        --yes \
        --no-install-recommends \
        libgcc-s1 \
        libbz2-1.0 \
        liblz4-1 \
        liblzma5 \
        libmimalloc3 \
        libstdc++6 \
        libxxhash0 \
        libzstd1 \
        tzdata \
        zlib1g \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=python-builder /app/.venv /app/.venv
COPY comet ./comet
COPY --from=frontend-builder /app/frontend/dist /app/comet/frontend_dist
COPY --from=usenet-builder /app/native/usenet-engine/target/release/usenet-engine /app/native/usenet-engine
COPY --from=par2-tool /opt/par2/bin/par2 /app/bin/par2
COPY --from=par2-tool /opt/par2/share/doc/par2cmdline-turbo /usr/share/doc/par2cmdline-turbo
COPY --from=libarchive-tool /opt/libarchive-runtime/libarchive.so.13 /app/lib/libarchive.so.13
COPY --from=libarchive-tool /opt/libarchive/share/doc/libarchive /usr/share/doc/libarchive
ENV TZ=UTC \
    PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONMALLOC=malloc \
    LD_PRELOAD=libmimalloc.so.3

ARG COMET_COMMIT_HASH
ARG COMET_BUILD_DATE
ARG COMET_BRANCH

ENV COMET_COMMIT_HASH=${COMET_COMMIT_HASH} \
    COMET_BUILD_DATE=${COMET_BUILD_DATE} \
    COMET_BRANCH=${COMET_BRANCH}

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -m comet.healthcheck

ENTRYPOINT ["python", "-m", "comet.main"]
