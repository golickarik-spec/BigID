# syntax=docker/dockerfile:1

# --- Stage 1: build -------------------------------------------------------
# Chainguard's -dev variant has pip + a shell. Both stages share the same
# Chainguard Python build, so the venv created here is ABI-compatible with the
# distroless runtime below.
FROM cgr.dev/chainguard/python:latest-dev AS build

WORKDIR /app
ENV PATH="/app/venv/bin:$PATH"

# Self-contained virtualenv we can copy wholesale into the runtime image.
RUN python -m venv /app/venv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# --- Stage 2: runtime -----------------------------------------------------
# Chainguard distroless Python: no shell/package manager, runs as non-root
# (uid 65532), continuously rebuilt to carry ~zero known CVEs so the strict
# HIGH/CRITICAL scan gate stays green.
FROM cgr.dev/chainguard/python:latest

WORKDIR /app
ENV PATH="/app/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PORT=8000

COPY --from=build /app/venv /app/venv
COPY app /app/app

EXPOSE 8000

# `python` resolves to the venv interpreter via PATH; uvicorn binds 0.0.0.0 so
# kubernetes probes can reach it.
ENTRYPOINT ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
