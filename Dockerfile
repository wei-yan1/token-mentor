FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TOKEN_MENTOR_DB=/data/catalog.sqlite3

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m pip install --no-cache-dir '.[api,payments]' \
    && useradd --create-home --uid 10001 appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /app /data

USER appuser
EXPOSE 8080
VOLUME ["/data"]
CMD ["token-mentor", "api", "--host", "0.0.0.0", "--port", "8080", "--db", "/data/catalog.sqlite3"]
