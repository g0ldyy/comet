FROM ghcr.io/astral-sh/uv:python3.11-alpine
LABEL name="Comet" \
      description="Stremio's fastest torrent/debrid search add-on." \
      url="https://github.com/g0ldyy/comet"

RUN apk add --no-cache gcc python3-dev musl-dev linux-headers git make

WORKDIR /app

ARG DATABASE_PATH

COPY pyproject.toml .

RUN uv sync

COPY . .

ENTRYPOINT ["uv", "run", "python", "-m", "comet.main"]
