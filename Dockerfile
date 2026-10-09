# syntax=docker/dockerfile:1
# Stage 1: resolve uv.lock into a virtual environment, then install the project itself.
# Based on the same Docker Hub image as the runtime stage rather than ghcr.io/astral-sh/uv:
# ghcr.io pulls are blocked on the build machine (stale registry credential; gate U6.0), so the
# image depends on Docker Hub only. uv is installed with pip, pinned to the version used locally.
FROM python:3.12-slim-bookworm AS builder
# PIP_*: pip runs once, as root, only to install uv; silence its root-user and version-check
# notices (cosmetic build noise; this stage is discarded).
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=0 \
    PIP_ROOT_USER_ACTION=ignore PIP_DISABLE_PIP_VERSION_CHECK=1
RUN pip install --no-cache-dir uv==0.12.22
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project
COPY README.md LICENSE ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

# Stage 2: a slim runtime without uv or build tools, running as a non-root user.
FROM python:3.12-slim-bookworm AS runtime
RUN groupadd --system waive \
    && useradd --system --gid waive --home-dir /app --shell /usr/sbin/nologin waive
WORKDIR /app
COPY --from=builder --chown=waive:waive /app/.venv /app/.venv
# CA certificate for Managed PostgreSQL (sslmode=verify-full), from
# https://docs.nebius.com/postgresql/databases/connect.md
ADD --chown=waive:waive https://storage.eu-north1.nebius.cloud/msp-certs/ca.pem /app/certs/msp-ca.pem
RUN mkdir -p /app/var && chown waive:waive /app/var
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
USER waive
EXPOSE 8000
# --no-access-log: request paths carry capability tokens. --proxy-headers: the managed HTTPS
# front end terminates TLS, so the app must trust X-Forwarded-* for scheme and client address.
CMD ["uvicorn", "waive.web.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*", "--no-access-log"]
