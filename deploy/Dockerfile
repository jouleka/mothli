# Mothli hosted mode (ADR 0041 part 9). Build from the repository root:
#   docker build -f deploy/Dockerfile --target backend -t mothli/backend .
#   docker build -f deploy/Dockerfile --target proxy   -t mothli/proxy .
# The compose file does both. No secret is read or written here: every setting arrives at run
# time from the server's environment files.

# ---- Python dependencies, from the lock file, without dev tools or the tests ----------------
FROM ghcr.io/astral-sh/uv:0.11.24@sha256:99ea34acedc870ba4ad11a1f540a1c04267c9f30aadc465a94406f52dfda2c36 AS uv

FROM python:3.12.16-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df AS python-deps
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /build
COPY backend/pyproject.toml backend/uv.lock ./
# The project itself is not installed: the app runs from /app/backend as `python -m ...`.
RUN uv sync --frozen --no-install-project --no-editable

# ---- The frontend, built to static files for the proxy --------------------------------------
FROM node:22.23.4-bookworm-slim@sha256:5ec23b7c622b00547753bc6ffd78085dd868a68aeb41c8e0a1418f033e13d96d AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --ignore-scripts --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# ---- The backend image: one image, run as the web process and as the worker -----------------
FROM python:3.12.16-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df AS backend
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DEPLOYMENT_MODE=hosted \
    HOSTED_STORAGE_BACKEND=filesystem \
    HOSTED_STORAGE_FILESYSTEM_ROOT=/var/lib/mothli/books
RUN groupadd --system --gid 10001 mothli \
 && useradd --system --uid 10001 --gid mothli --home-dir /nonexistent --shell /usr/sbin/nologin mothli \
 && mkdir -p /var/lib/mothli/books \
 && chown mothli:mothli /var/lib/mothli/books \
 && chmod 700 /var/lib/mothli/books
COPY --from=python-deps /opt/venv /opt/venv
WORKDIR /app/backend
# Only the application package and the one deployment script: no tests, no fixtures, no .env.
COPY backend/app ./app
COPY backend/scripts/hosted_roles.py ./scripts/hosted_roles.py
# The public pages written out at build time, filled in at each request (app/hosted/public_pages.py).
COPY --from=frontend /build/prerendered/public-pages.json ./public/public-pages.json
USER mothli:mothli
EXPOSE 8000
# The compose file sets the command per process; this default is the web process.
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", \
     "--no-access-log", "--no-server-header", "--timeout-keep-alive", "75"]

# ---- The proxy: Caddy with the built frontend in a layer -------------------------------------
FROM caddy:2.11.9-alpine@sha256:003a97808e781aa9f7e75cc2f43d34d461099031faf5e20faefb9f61126836f0 AS proxy
COPY deploy/Caddyfile /etc/caddy/Caddyfile
COPY --from=frontend /build/dist /srv/mothli
USER 65534:65534
EXPOSE 8080
