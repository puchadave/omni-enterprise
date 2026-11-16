FROM node:22-alpine

ARG OMNIROUTE_VERSION=latest
LABEL org.opencontainers.image.title="omniroute-gateway-allinone" \
      org.opencontainers.image.description="OmniRoute AI gateway container with all configs and API keys preconfigured" \
      org.opencontainers.image.version="${OMNIROUTE_VERSION}"

RUN apk add --no-cache curl wget openssl tini ca-certificates \
 && npm install -g omniroute@${OMNIROUTE_VERSION} \
 && npm cache clean --force \
 && mkdir -p /root/.omniroute /data /opt/omniroute

ENV NODE_ENV=production \
    OMNIROUTE_HOST=0.0.0.0 \
    OMNIROUTE_PORT=20128 \
    OMNIROUTE_CONFIG_DIR=/root/.omniroute \
    OMNIROUTE_DATA_DIR=/data

# Bundle all configs into the image (self-contained). Entrypoint seeds volumes on first start.
COPY config/config.json /opt/omniroute/config.json
COPY config/omnicore-auto-combos.json /opt/omniroute/omnicore-auto-combos.json
COPY config/endpoints.json /opt/omniroute/endpoints.json
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

EXPOSE 20128

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=5 \
  CMD sh -lc 'curl -fsS -H "Authorization: Bearer ${OMNIROUTE_API_KEY}" http://127.0.0.1:${OMNIROUTE_PORT:-20128}/v1/models >/dev/null || curl -fsS http://127.0.0.1:${OMNIROUTE_PORT:-20128}/v1/models >/dev/null'

ENTRYPOINT ["/sbin/tini", "--", "/usr/local/bin/docker-entrypoint.sh"]
CMD ["omniroute"]
