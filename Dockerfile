# The archive server. Nothing here is architecture-specific -- both the base
# image and uv's are multi-arch -- so this builds natively on arm64 (a Mac) and
# amd64 (a NAS or a VPS) without a variant per host.

# --- frontend builder: the browser client, compiled to static assets ---------
# node:24-alpine and not the host's node: this stage only has to run Vite, and
# pinning it here means the image never depends on what a developer's machine
# happens to have installed.
FROM node:25-alpine AS frontend-builder

WORKDIR /app/frontend
# package files first, so a source-only change reuses the install layer.
COPY frontend/package.json frontend/package-lock.json frontend/.npmrc ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# --- builder: resolve the locked dependency set into a venv ------------------
FROM python:3.13-slim-bookworm AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Use the interpreter already in the image (the runtime stage has the same one
# at the same path) instead of letting uv fetch a second, standalone build.
ENV UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_LINK_MODE=copy

WORKDIR /app
COPY pyproject.toml uv.lock ./
# --no-install-project: the app is not imported as an installed package, it is
# run as `python -m aspect.server` from /app. Keeping the project out of the venv
# means this layer depends only on the lockfile, so editing aspect/ never
# re-resolves dependencies.
RUN uv sync --frozen --no-dev --no-install-project

# --- runtime -----------------------------------------------------------------
FROM python:3.13-slim-bookworm

# image.source is what links the published package to this repository on GHCR:
# without it the package shows up in the org with no repo, no README and no
# provenance, and it cannot inherit the repository's visibility.
LABEL org.opencontainers.image.source="https://github.com/ember-and-ink/aspect-archive" \
      org.opencontainers.image.description="Character archive: browses, builds and serves SillyTavern V3 character cards." \
      org.opencontainers.image.licenses="NOASSERTION"

# pngquant compresses card avatars before they are embedded. The vendored
# binary next to aspect/ is a macOS Mach-O one and is excluded by .dockerignore,
# so PngWriter's PATH fallback picks up this one.
RUN apt-get update \
    && apt-get install -y --no-install-recommends pngquant \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 1000 app

WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY aspect/ ./proxy/
# The client's built output only -- no node_modules, no sources. Serving it is
# aspect/server.py's FRONTEND_DIST, and it answers at the root.
COPY --from=frontend-builder /app/frontend/dist ./frontend/dist
# Source fragments only, not the compiled bundles: the server concatenates them
# per request with the user's server URL and tag filter substituted in
# (aspect/userscripts.py). ~60 KB, and it is what makes the userscripts
# installable without a checkout.
COPY userscript/src_jai/ ./userscript/src_jai/
COPY userscript/src_saucepan/ ./userscript/src_saucepan/
COPY pyproject.toml uv.lock README.md ./

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    # Bind all interfaces: 127.0.0.1 would only be reachable from inside the
    # container. This is the one variable the container actually needs -- every
    # writable path already defaults under /app/data, which is the mount.
    ASPECT_HOST=0.0.0.0

# aspect/config.py creates its directories at import time; /app/data is normally
# a mount, but pre-creating it keeps a mountless `docker run` from failing.
#
# uid 1000 is only the DEFAULT. The image must also run under an arbitrary uid
# (`--user 99:100`, which is what unraid's nobody:users appdata needs), so the
# invariant is: everything under /app stays world-readable -- and /app/.venv
# world-executable -- and nothing is written outside /app/data at runtime. COPY
# leaves 755/644, so chown alone does not break that; do not add a `chmod go-r`
# or move any writable path out of /app/data without revisiting this.
RUN mkdir -p /app/data && chown -R app:app /app
USER app

EXPOSE 8000

# urllib rather than curl -- it is already in the interpreter, and installing a
# HTTP client just to poll a local endpoint is a package to keep patched.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).read()"]

CMD ["python", "-m", "aspect.server"]
