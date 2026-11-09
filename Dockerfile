# syntax=docker/dockerfile:1

# --- Stage 1: build -------------------------------------------------------
# Debian-12 based CPython 3.11 — kept in lock-step with the distroless runtime
# below (gcr.io/distroless/python3-debian12 ships CPython 3.11), so compiled
# wheels (pydantic-core, uvloop, httptools) are ABI-compatible at runtime.
FROM python:3.11-slim-bookworm AS build

WORKDIR /app
COPY requirements.txt .

# Install dependencies into an isolated prefix we can copy wholesale into the
# distroless image (distroless has no pip).
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir --target=/install -r requirements.txt

# --- Stage 2: runtime -----------------------------------------------------
# Distroless: no shell, no package manager, minimal attack surface, runs as a
# non-root user (uid 65532) out of the box. Trivy scans this clean for
# HIGH/CRITICAL fixable CVEs.
#
# Pinned by tag here for readability; CI/README document how to pin by digest:
#   docker buildx imagetools inspect gcr.io/distroless/python3-debian12:nonroot
FROM gcr.io/distroless/python3-debian12:nonroot

WORKDIR /app

# Third-party packages (copied to a dir on PYTHONPATH) + application code.
COPY --from=build /install /app/deps
COPY app /app/app

ENV PYTHONPATH=/app/deps \
    PYTHONUNBUFFERED=1 \
    PORT=8000

EXPOSE 8000

# distroless/python3 ENTRYPOINT is already the python interpreter; we only
# supply the module invocation. uvicorn binds 0.0.0.0 so k8s probes can reach it.
ENTRYPOINT ["python3", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
