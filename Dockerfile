FROM python:3.11
LABEL name="Comet" \
      description="Comet Strmeio Addon" \
      url="https://github.com/g0ldyy/comet"

# This is to prevent Python from buffering stdout and stderr
ENV PYTHONUNBUFFERED=1

# Install Poetry
RUN pip install poetry==1.8.3

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

EXPOSE 8000

#CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
CMD ["python", "run.py"]