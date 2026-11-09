"""A tiny FastAPI service that echoes the client's originating IP address.

Endpoints:
  GET /        -> the client's originating IP (JSON)
  GET /health  -> liveness probe
  GET /ready   -> readiness probe

Originating-IP resolution
-------------------------
Behind a proxy/load-balancer/Ingress, the TCP peer the app sees is an internal
hop, not the real client. The real client is carried in the ``X-Forwarded-For``
header as a comma-separated chain ``client, proxy1, proxy2``. We take the
left-most entry as the originating client and fall back to the socket peer when
no header is present.

SECURITY CAVEAT: ``X-Forwarded-For`` is client-supplied and therefore spoofable.
Only trust it when every hop in front of this app is a proxy you control that
appends (rather than reflects) the header. See README "IP resolution".
"""

from __future__ import annotations

import os

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI(title="ipapp", version="0.1.0")

# Header carrying the proxy chain. Overridable for odd infra, but the default
# is the universal standard.
FORWARDED_FOR_HEADER = os.environ.get("FORWARDED_FOR_HEADER", "x-forwarded-for")


def client_ip(request: Request) -> tuple[str, str]:
    """Return ``(ip, source)`` for the request.

    ``source`` is ``"x-forwarded-for"`` when the header supplied the address, or
    ``"socket"`` when we fell back to the raw TCP peer.
    """
    forwarded = request.headers.get(FORWARDED_FOR_HEADER)
    if forwarded:
        # Left-most entry is the originating client.
        first = forwarded.split(",")[0].strip()
        if first:
            return first, "x-forwarded-for"

    peer = request.client.host if request.client else "unknown"
    return peer, "socket"


@app.get("/")
def root(request: Request) -> JSONResponse:
    ip, source = client_ip(request)
    return JSONResponse({"ip": ip, "source": source})


@app.get("/health")
def health() -> JSONResponse:
    """Liveness: the process is up and serving."""
    # DEMO: intentionally returns "healthy" instead of "ok" to make test_health
    # fail, so the CI AI-diagnosis step has a real failure to analyse.
    return JSONResponse({"status": "healthy"})


@app.get("/ready")
def ready() -> JSONResponse:
    """Readiness: the app is ready to receive traffic."""
    return JSONResponse({"status": "ready"})
