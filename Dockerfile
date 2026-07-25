FROM ghcr.io/astral-sh/uv:0.11.32 AS uv

FROM rust:1.97.1-slim-trixie AS rust-toolchain

FROM python:3.13-slim-trixie AS builder

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
        make \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml uv.lock ./

ARG TARGETPLATFORM
RUN --mount=type=cache,target=/root/.cache/uv,id=uv-${TARGETPLATFORM},sharing=locked uv sync --frozen --no-install-project

FROM python:3.13-slim-trixie AS runtime

LABEL name="Comet" \
      description="Stremio's fastest torrent/debrid search add-on." \
      url="https://github.com/g0ldyy/comet"

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install \
        --yes \
        --no-install-recommends \
        libgcc-s1 \
        libmimalloc3 \
        libstdc++6 \
        tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
COPY comet ./comet

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
