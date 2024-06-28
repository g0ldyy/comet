FROM python:3.11-alpine
LABEL name="Comet" \
      description="Stremio's fastest torrent/debrid search add-on." \
      url="https://github.com/g0ldyy/comet"

# This is to prevent Python from buffering stdout and stderr
ENV PYTHONUNBUFFERED=1

# Fix python-alpine gcc
RUN apk add --no-cache \
    gcc \
    musl-dev \
    libffi-dev \
    make

# Install Poetry
RUN pip install poetry

ENV POETRY_NO_INTERACTION=1 \
    POETRY_VIRTUALENVS_IN_PROJECT=1 \
    POETRY_VIRTUALENVS_CREATE=1 \
    POETRY_CACHE_DIR=/tmp/poetry_cache

# Set working directory
WORKDIR /app

# Copy the application code
COPY . ./

RUN poetry install --no-root

ENV VIRTUAL_ENV=/app/.venv
ENV PATH="/app/.venv/bin:$PATH"

CMD ["python", "run.py"]
