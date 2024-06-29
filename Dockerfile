FROM python:3.11-alpine
LABEL name="Comet" \
      description="Stremio's fastest torrent/debrid search add-on." \
      url="https://github.com/g0ldyy/comet"

WORKDIR /app

ARG DATABASE_PATH

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    POETRY_NO_INTERACTION=1 \
    POETRY_HOME='/usr/local' \
    FASTAPI_HOST=0.0.0.0 \
    FASTAPI_PORT=8000 \
    FASTAPI_WORKERS=1 \
    DATABASE_PATH=comet.db \
    FORCE_COLOR=1 \
    TERM=xterm-256color

RUN pip install poetry
COPY . .
RUN poetry install --no-cache --no-root --without dev

ENTRYPOINT ["poetry", "run", "python", "-m", "comet.main"]
