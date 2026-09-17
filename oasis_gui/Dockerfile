# Headless Oasis console - built for a small box (2 vCPU / 3GB).
#
# Two stages on purpose: the admin UI is React + Ant Design, which needs Node to
# build. Doing that here means the deployed image never has to: the server just
# pulls the finished image. The runtime stage stays Python-only.

# ---------------------------------------------------------------- frontend ---
FROM node:22-alpine AS frontend
WORKDIR /ui
# Copy the manifests first so a source-only change does not re-run npm ci.
COPY webui/package.json webui/package-lock.json* ./
RUN npm ci || npm install
COPY webui/ ./
RUN npm run build

# ----------------------------------------------------------------- runtime ---
# The official Playwright image already carries chromium and every system
# library it needs (fonts, libnss, mesa for WebGL). Building those by hand is
# where headless-browser images usually go wrong.
FROM mcr.microsoft.com/playwright/python:v1.62.0-noble

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    HOME=/home/runner

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Only what the service path needs - app.py (the PyQt window) stays out, which
# is what keeps this image usable without a display.
COPY service.py entrypoint.sh ./
COPY core ./core

# The built admin UI, where service.py looks for it by default.
COPY --from=frontend /ui/dist ./webui_dist

RUN chmod +x entrypoint.sh \
    && useradd --create-home --uid 10001 runner \
    && mkdir -p /data \
    && chown -R runner:runner /data /app

VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=10s --start-period=60s --retries=3 \
    CMD python3 -c "import json,urllib.request; \
print(json.load(urllib.request.urlopen('http://127.0.0.1:8080/health',timeout=5))['ok'])" \
    || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]
