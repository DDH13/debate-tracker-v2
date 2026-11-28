# Production image for the API. Build: docker build -t debate-tracker-api .
# Run:   docker run --env-file .env.production -p 8000:8000 debate-tracker-api
# Every setting comes from the environment; see docs/deployment.md.
FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000 \
    WORKERS=2 \
    FORWARDED_ALLOW_IPS=127.0.0.1 \
    IMPORT_STAGING_DIR=/data/import-staging

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
COPY scripts ./scripts

# Console import staging holds speaker emails between check and import; mount a volume at
# /data so it survives restarts.
RUN useradd --create-home --uid 10001 app && mkdir -p /data && chown app /data
USER app

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/health', timeout=4)"

CMD exec gunicorn app.main:app -k uvicorn_worker.UvicornWorker \
    -w "$WORKERS" --bind "0.0.0.0:$PORT" \
    --timeout 120 --graceful-timeout 30 --keep-alive 5 \
    --access-logfile - --forwarded-allow-ips "$FORWARDED_ALLOW_IPS"
