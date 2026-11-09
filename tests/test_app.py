"""Unit tests for the ipapp service."""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_root_uses_leftmost_forwarded_for():
    resp = client.get("/", headers={"X-Forwarded-For": "1.2.3.4, 10.0.0.1, 10.0.0.2"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ip"] == "1.2.3.4"
    assert body["source"] == "x-forwarded-for"


def test_root_trims_whitespace_in_forwarded_for():
    resp = client.get("/", headers={"X-Forwarded-For": "  5.6.7.8  "})
    assert resp.status_code == 200
    assert resp.json()["ip"] == "5.6.7.8"


def test_root_falls_back_to_socket_when_no_header():
    resp = client.get("/")
    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "socket"
    # TestClient presents a non-empty peer host.
    assert body["ip"]


def test_health():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_ready():
    resp = client.get("/ready")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ready"}
