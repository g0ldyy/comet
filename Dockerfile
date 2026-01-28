FROM ghcr.io/astral-sh/uv:python3.11-alpine
LABEL name="Comet" \
      description="Stremio's fastest torrent/debrid search add-on." \
      url="https://github.com/g0ldyy/comet"

RUN apk add --no-cache gcc python3-dev musl-dev linux-headers git make

WORKDIR /app

ARG DATABASE_PATH
ARG COMET_COMMIT_HASH
ARG COMET_BUILD_DATE
ARG COMET_BRANCH

COPY pyproject.toml .

ENV UV_HTTP_TIMEOUT=300 \
    COMET_COMMIT_HASH=${COMET_COMMIT_HASH} \
    COMET_BUILD_DATE=${COMET_BUILD_DATE} \
    COMET_BRANCH=${COMET_BRANCH}
RUN uv sync

COPY . .

ENTRYPOINT ["uv", "run", "python", "-m", "comet.main"]
