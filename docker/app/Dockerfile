# One image for dev shell, CI and the trusted worker.
# Base tag MUST match the `playwright==` pin in pyproject.toml (browsers + system deps preinstalled).
FROM mcr.microsoft.com/playwright/python:v1.65.2-noble

# Tesseract for OCR fallback on scanned PDFs (slice B / phase 5).
RUN apt-get update \
 && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng \
 && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_DOWNLOADS=never \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependencies first for layer caching.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-install-project --python /usr/bin/python3.12 --group dev

COPY src ./src
COPY alembic ./alembic
COPY alembic.ini ./
COPY config ./config
RUN uv sync --frozen --python /usr/bin/python3.12 --group dev

RUN mkdir -p /data/raw && chown -R pwuser:pwuser /data

# Run as the non-root user shipped with the Playwright image.
USER pwuser
ENTRYPOINT ["scrapeline"]
CMD ["--help"]
