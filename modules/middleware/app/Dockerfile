# syntax=docker/dockerfile:1

FROM python:3.11-alpine AS builder

RUN apk add --no-cache --virtual .build-deps \
    gcc \
    musl-dev \
    libffi-dev \
    postgresql-dev \
    cargo \
    make

WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM python:3.11-alpine AS runtime

RUN apk add --no-cache \
    libpq \
    libffi \
    sqlite-libs \
    tzdata \
    wget

WORKDIR /app
COPY --from=builder /install /usr/local
COPY . /app

RUN addgroup -S omnicore && adduser -S -G omnicore omnicore \
    && mkdir -p /app/data \
    && chown -R omnicore:omnicore /app

USER omnicore
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATABASE_PATH=/app/data/middleware.db

EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=12 \
    CMD wget -q -O /dev/null http://127.0.0.1:8000/healthz || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--loop", "uvloop"]
