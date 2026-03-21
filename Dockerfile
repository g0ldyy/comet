FROM ghcr.io/astral-sh/uv:python3.13-alpine
LABEL name="Comet" \
      description="Stremio's fastest torrent/debrid search add-on." \
      url="https://github.com/g0ldyy/comet"

RUN apk add --no-cache \
    gcc \
    python3-dev \
    musl-dev \
    linux-headers \
    git \
    make \
    tzdata \
    mimalloc2 \
    sqlite-libs

WORKDIR /app

COPY pyproject.toml uv.lock ./

ENV TZ=UTC \
    UV_HTTP_TIMEOUT=300 \
    PYTHONMALLOC=malloc \
    LD_PRELOAD=/usr/lib/libmimalloc.so.2
ARG TARGETPLATFORM
RUN --mount=type=cache,target=/root/.cache/uv,id=uv-${TARGETPLATFORM},sharing=locked uv sync --frozen

COPY . .

ARG DATABASE_PATH
ARG COMET_COMMIT_HASH
ARG COMET_BUILD_DATE
ARG COMET_BRANCH

ENV COMET_COMMIT_HASH=${COMET_COMMIT_HASH} \
    COMET_BUILD_DATE=${COMET_BUILD_DATE} \
    COMET_BRANCH=${COMET_BRANCH}

ENTRYPOINT ["uv", "run", "python", "-m", "comet.main"]
