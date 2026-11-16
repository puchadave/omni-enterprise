# syntax=docker/dockerfile:1
# OmniRoute Gateway - standalone, preconfigured & optimized for OMNI-CORE e-commerce

FROM node:22-alpine

ARG OMNIROUTE_VERSION=latest
LABEL org.opencontainers.image.title="omniroute-gateway" \
      org.opencontainers.image.description="OmniRoute AI Gateway (OpenAI-compatible, preconfigured for OMNI-CORE)" \
      org.opencontainers.image.vendor="OMNI-CORE"

ENV TZ=Europe/Berlin \
    NODE_ENV=production \
    OMNIROUTE_SERVER_HOST=0.0.0.0 \
    OMNIROUTE_PORT=20128 \
    REQUIRE_API_KEY=true \
    OMNIROUTE_TIMEOUT_MS=1200 \
    OMNIROUTE_FAILOVER_TIMEOUT_MS=1400 \
    OMNIROUTE_RETRY_429=true \
    OMNIROUTE_RETRY_502=true \
    OMNIROUTE_RETRY_503=true \
    OMNIROUTE_CACHE_RESPONSES=true \
    OMNIROUTE_CACHE_TTL_SECONDS=300 \
    OMNIROUTE_LOCAL_ONLY=false

RUN apk add --no-cache bash curl jq ca-certificates tzdata openssl \
    && cp /usr/share/zoneinfo/Europe/Berlin /etc/localtime \
    && echo "Europe/Berlin" > /etc/timezone \
    && npm config set prefix /usr/local \
    && npm install -g omniroute@${OMNIROUTE_VERSION} \
    && npm cache clean --force \
    && mkdir -p /root/.omniroute /opt/omniroute /data /var/log/omniroute

COPY config/config.json /root/.omniroute/config.json
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh \
    && chmod 700 /root/.omniroute

EXPOSE 20128
VOLUME ["/root/.omniroute", "/data"]

HEALTHCHECK --interval=10s --timeout=5s --start-period=20s --retries=20 \
  CMD sh -lc 'curl -fsS -H "Authorization: Bearer ${OMNIROUTE_API_KEY}" http://127.0.0.1:${OMNIROUTE_PORT:-20128}/v1/models >/dev/null || curl -fsS http://127.0.0.1:${OMNIROUTE_PORT:-20128}/v1/models >/dev/null'

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["omniroute"]
