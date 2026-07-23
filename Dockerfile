FROM ghcr.io/astral-sh/uv:python3.13-alpine AS builder

RUN apk add --no-cache gcc python3-dev musl-dev linux-headers git make

WORKDIR /app

COPY pyproject.toml uv.lock ./

ARG TARGETPLATFORM
RUN --mount=type=cache,target=/root/.cache/uv,id=uv-${TARGETPLATFORM},sharing=locked uv sync --frozen --no-install-project

FROM python:3.13-alpine AS runtime

LABEL name="Comet" \
      description="Stremio's fastest torrent/debrid search add-on." \
      url="https://github.com/g0ldyy/comet"

RUN apk add --no-cache libgcc libstdc++ tzdata mimalloc2

WORKDIR /app

RUN addgroup -S comet && \
    adduser -S -D -H -G comet comet && \
    mkdir -p /app/data && \
    chown comet:comet /app/data

COPY --from=builder --chown=comet:comet /app/.venv /app/.venv
COPY --chown=comet:comet comet ./comet

ENV TZ=UTC \
    PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONMALLOC=malloc \
    LD_PRELOAD=/usr/lib/libmimalloc.so.2

ARG COMET_COMMIT_HASH
ARG COMET_BUILD_DATE
ARG COMET_BRANCH

ENV COMET_COMMIT_HASH=${COMET_COMMIT_HASH} \
    COMET_BUILD_DATE=${COMET_BUILD_DATE} \
    COMET_BRANCH=${COMET_BRANCH}

USER comet

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD wget -qO- "http://127.0.0.1:${FASTAPI_PORT:-8000}/health" >/dev/null || exit 1

ENTRYPOINT ["python", "-m", "comet.main"]
