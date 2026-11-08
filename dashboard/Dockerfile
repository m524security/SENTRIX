# =============================================================================
# SENTRIX Dashboard — reproducible build, non-root runtime
# =============================================================================
# Audit fixes:
#   • COPY package-lock.json + `npm ci` — the previous `COPY package.json` then
#     `npm install` ignored the lock file entirely, defeating the reproducibility
#     the lock file exists for. `npm ci` fails loudly on lock/manifest drift
#     (and immediately caught a real drift: yaml@2.9.0 missing from the lock).
#   • nginx-unprivileged — the final process runs as uid 101 on :8080, so no
#     capabilities are needed at all (compose previously granted
#     NET_BIND_SERVICE/SETUID/SETGID just to let stock nginx bind :80).
#   • No heredoc COPY — that requires an explicit `# syntax=docker/dockerfile:1.4`
#     frontend directive and breaks the build without one.
#
# react-scripts lives in devDependencies (CRA ships it in `dependencies`, which
# is wrong — it is a build tool). This multi-stage build means the whole
# webpack/jest toolchain and its known CVEs stay in the builder; only static
# HTML/CSS/JS reaches the runtime image. See SECURITY.md.
# =============================================================================

# ---- build ------------------------------------------------------------------
FROM node:20-alpine AS build
WORKDIR /app

# Lock file first: `npm ci` installs the exact resolved tree and fails if
# package.json and package-lock.json disagree.
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY . .
# CI=false so CRA lint warnings don't fail the production build.
RUN CI=false npm run build

# ---- runtime ----------------------------------------------------------------
# nginx-unprivileged: same nginx, runs as a non-root user out of the box.
FROM nginxinc/nginx-unprivileged:alpine

# Briefly root to place files and write config — /etc/nginx/conf.d is root-owned,
# so this must happen before dropping back to 101.
USER root

COPY --from=build /app/build /usr/share/nginx/html

# SPA fallback so client-side routing works, plus browser-hardening headers for
# the static assets nginx serves directly (the API sets its own — see api/main.py).
RUN printf '%s\n' \
  'server {' \
  '  listen 8080;' \
  '  server_name _;' \
  '  add_header X-Content-Type-Options "nosniff" always;' \
  '  add_header X-Frame-Options "DENY" always;' \
  '  add_header Referrer-Policy "no-referrer" always;' \
  '  add_header Permissions-Policy "camera=(), microphone=(), geolocation=()" always;' \
  '  location / {' \
  '    root /usr/share/nginx/html;' \
  '    try_files $uri /index.html;' \
  '  }' \
  '}' \
  > /etc/nginx/conf.d/default.conf \
  && chown -R 101:101 /usr/share/nginx/html /etc/nginx/conf.d

# Drop privileges for the actual runtime.
USER 101
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=4s --start-period=10s --retries=3 \
  CMD wget -q --spider http://127.0.0.1:8080/ || exit 1
