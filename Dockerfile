# StockFlow production image (linux/amd64 in CI; see docs/ARCHITECTURE.md section 10.2).
#
# Reproducibility: both base images are pinned by tag AND digest, the Python dependencies are installed
# from uv.lock only (`uv sync --frozen`, runtime group, no project install), and only app/ +
# pyproject.toml + uv.lock reach the build context (allow-list .dockerignore).
# Dependabot (docker ecosystem) proposes digest refreshes; Trivy gates every build.
#
# Build args are metadata only. Never pass a secret as ARG/ENV.

# -- uv binary (pinned version + digest of the multi-arch index) ---------------------------------------
FROM ghcr.io/astral-sh/uv:0.13.0@sha256:cdc6093146eb3ff6a40107b38f008b789e050e77ad87865e381d9917da55a168 AS uv

# -- BASE IMAGE: the single line the demo `bad_container` scenario patches ------------------
FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1 AS base

# -- builder: resolve the locked runtime dependencies into /opt/venv ----------------------------------
FROM base AS builder
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON_PREFERENCE=only-system \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=0 \
    UV_NO_CACHE=1
WORKDIR /build
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# -- runtime: no compilers, no uv, no pip, non-root ---------------------------------------------------
FROM base AS runtime
LABEL org.opencontainers.image.title="stockflow" \
      org.opencontainers.image.description="StockFlow inventory and order demo (FastAPI)" \
      org.opencontainers.image.licenses="UNLICENSED"

# Remove the package installer from the final image (nothing installs at runtime), strip every setuid/setgid
# bit (mount, su, passwd, ... are never needed by this app and are privilege-escalation helpers), and add the
# unprivileged user.
RUN python -m pip uninstall -y pip \
    && find / -xdev -perm /6000 -type f -exec chmod a-s {} + \
    && groupadd --system --gid 10001 stockflow \
    && useradd --uid 10001 --gid 10001 --no-log-init --no-create-home --home-dir /nonexistent \
        --shell /usr/sbin/nologin stockflow

COPY --from=builder /opt/venv /opt/venv
WORKDIR /app
# Code stays root-owned and read-only for the runtime user.
COPY app/ ./app/

ARG APP_VERSION=0.0.0-dev
ARG GIT_SHA=unknown
ARG BUILD_TIME=
# The environment name shown in the UI and /api/version. Production images keep the default; the PR preview
# build passes preview-pr-<n> so a preview never reports itself as production.
ARG DEPLOY_ENVIRONMENT=production
ENV APP_VERSION=${APP_VERSION} \
    GIT_SHA=${GIT_SHA} \
    BUILD_TIME=${BUILD_TIME} \
    DEPLOY_ENVIRONMENT=${DEPLOY_ENVIRONMENT} \
    PORT=8000 \
    PATH=/opt/venv/bin:${PATH} \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
LABEL org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.revision="${GIT_SHA}" \
      org.opencontainers.image.created="${BUILD_TIME}"

USER 10001:10001
EXPOSE 8000

# Liveness via the Python stdlib (no curl in the image); honours $PORT.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('PORT', '8000'), timeout=3)"]

# Exec form: python is PID 1 and receives SIGTERM, so uvicorn shuts down gracefully. Logs go to stdout/stderr.
CMD ["python", "-m", "app"]
