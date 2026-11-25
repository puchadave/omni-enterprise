# syntax=docker/dockerfile:1
# Omni-Core Middleware - standalone, preconfigured & optimized for OmniRoute integration

ARG PYTHON_VERSION=3.11
FROM python:${PYTHON_VERSION}-alpine AS builder

RUN apk add --no-cache --virtual .build-deps \
      gcc g++ musl-dev libffi-dev postgresql-dev cargo make linux-headers \
      jpeg-dev zlib-dev libxml2-dev libxslt-dev openssl-dev

WORKDIR /build
# leverage layer caching: deps first
COPY requirements_frozen.txt requirements.txt* ./
RUN set -eux; \
    if [ -s requirements_frozen.txt ]; then REQ=requirements_frozen.txt; elif [ -s requirements.txt ]; then REQ=requirements.txt; else echo "no requirements file"; exit 1; fi; \
    pip install --no-cache-dir --upgrade pip setuptools wheel; \
    pip install --no-cache-dir --prefix=/install -r "$REQ"; \
    /install/bin/python - <<'PY' || pip install --no-cache-dir --prefix=/install uvicorn fastapi uvloop httptools
import importlib
for m in ['uvicorn','fastapi']:
    importlib.import_module(m)
PY

FROM python:${PYTHON_VERSION}-alpine AS runtime

ARG APP_UID=10001
ARG APP_GID=10001
LABEL org.opencontainers.image.title="omnicore-middleware" \
      org.opencontainers.image.description="Omni-Core Middleware standalone (OmniRoute-integrated, preconfigured)" \
      org.opencontainers.image.vendor="OMNI-CORE"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONOPTIMIZE=1 \
    PYTHONPATH=/app \
    TZ=Europe/Berlin \
    DATABASE_PATH=/app/data/middleware.db \
    APP_PORT=8000 \
    UVICORN_HOST=0.0.0.0 \
    UVICORN_PORT=8000 \
    UVICORN_WORKERS=1 \
    UVICORN_LOOP=uvloop \
    UVICORN_HTTP=httptools \
    OMNIROUTE_URL=http://omniroute:20128/v1 \
    OMNIROUTE_BASE_URL=http://omniroute:20128/v1 \
    OMNI_ROUTE_URL=http://omniroute:20128/v1 \
    OMNIROUTE_TIMEOUT=60 \
    OMNIROUTE_RETRY=3 \
    OMNI_MODEL_ADMIN=omni-admin-agent \
    OMNI_MODEL_CONTENT_SEO=omni-content-seo \
    OMNI_MODEL_VISION=omni-vision-photo \
    OMNI_MODEL_VIDEO=omni-video-gen \
    OMNI_MODEL_CRM=omni-crm-support \
    OMNI_MODEL_CRM_GDPR=omni-local-gdpr \
    ENABLE_MARKETING_ENGINE=true \
    ENABLE_SALES_AUTOMATION=true \
    ENABLE_CHANNEL_ADAPTERS=true \
    ENABLE_MATTERHORN_IMPORT=true \
    OMNI_CORE_ENABLED=true \
    OMNI_CORE_SYNC_MODE=api_webhook_only \
    OMNI_CORE_SYNC_IDEMPOTENCY=true \
    OMNI_CORE_CONFLICT_STRATEGY=report_only_no_silent_overwrite

RUN apk add --no-cache \
      bash ca-certificates tzdata wget curl jq \
      libpq libffi sqlite-libs jpeg zlib libxml2 libxslt \
    && cp /usr/share/zoneinfo/Europe/Berlin /etc/localtime \
    && echo "Europe/Berlin" > /etc/timezone \
    && addgroup -S -g ${APP_GID} omnicore \
    && adduser -S -D -H -u ${APP_UID} -G omnicore omnicore \
    && mkdir -p /app /app/data /app/logs /app/media /app/uploads /app/static

WORKDIR /app
COPY --from=builder /install /usr/local
COPY --chown=omnicore:omnicore app/ /app/

RUN python -m compileall -q /app || true \
    && chown -R omnicore:omnicore /app/data /app/logs /app/media /app/uploads /app/static

USER omnicore
VOLUME ["/app/data", "/app/media", "/app/uploads"]
EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=5s --start-period=30s --retries=20 \
  CMD wget -q -O /dev/null http://127.0.0.1:8000/healthz || exit 1

CMD ["sh", "-lc", "exec uvicorn main:app --host ${UVICORN_HOST:-0.0.0.0} --port ${UVICORN_PORT:-8000} --workers ${UVICORN_WORKERS:-1} --loop ${UVICORN_LOOP:-uvloop} --http ${UVICORN_HTTP:-httptools} --proxy-headers --forwarded-allow-ips='*'"]
