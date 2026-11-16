# Stage 1: Build dependencies
FROM python:3.11-alpine AS builder

RUN apk add --no-cache \
    gcc \
    musl-dev \
    libffi-dev \
    openssl-dev \
    make \
    g++

WORKDIR /build

COPY requirements.txt .

# Install dependencies into a local site-packages directory
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# Stage 2: Runtime
FROM python:3.11-alpine

RUN apk add --no-cache \
    sqlite-libs \
    libffi \
    openssl \
    tzdata

WORKDIR /app

# Copy installed dependencies from builder
COPY --from=builder /install /usr/local

# Create a non-root user for security
RUN adduser -D omnicore
USER omnicore

# Copy application code
COPY --chown=omnicore:omnicore omni_core/ .

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATABASE_PATH=/app/middleware.db

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
