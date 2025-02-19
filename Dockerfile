FROM ghcr.io/astral-sh/uv:python3.11-alpine
LABEL name="Comet" \
      description="Stremio's fastest torrent/debrid search add-on." \
      url="https://github.com/g0ldyy/comet"

WORKDIR /app

ARG DATABASE_PATH

COPY pyproject.toml .

RUN uv sync

COPY . .

ENTRYPOINT ["uv", "run", "python", "-m", "comet.main"]
