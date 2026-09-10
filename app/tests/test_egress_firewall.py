"""
Egress firewall (app/security/egress_firewall.py) — the enforcement layer.

These are pure unit tests: no Ollama, no Docker, no real internet needed.
They install the guard, assert that off-LAN connects are refused and
LAN/loopback connects are not, exercise the /api endpoints, then always
uninstall the guard so the rest of the suite runs with normal sockets.

Run with: pytest app/tests/test_egress_firewall.py
"""
import socket

import pytest
from httpx import ASGITransport, AsyncClient

from app.security import egress_firewall as ef


@pytest.fixture()
def guard():
    ef.install(enabled=True, extra_cidrs=(), extra_ips=("203.0.113.7",))
    try:
        yield
    finally:
        ef.uninstall()


def test_off_lan_connect_is_blocked(guard):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    with pytest.raises(ef.EgressBlocked):
        s.connect(("8.8.8.8", 53))
    s.close()
    assert ef.status()["blocked_attempts_total"] >= 1
    assert ef.status()["recent_blocked"][0]["dest_ip"] == "8.8.8.8"


def test_loopback_and_private_are_allowed_by_policy(guard):
    # We don't need anything listening — a blocked destination raises
    # EgressBlocked, an allowed one raises ConnectionRefused/timeout or
    # succeeds. Only EgressBlocked means "the policy refused it".
    for host in ("127.0.0.1", "10.1.2.3", "192.168.50.50", "169.254.1.1"):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.2)
        try:
            s.connect((host, 9))
        except ef.EgressBlocked:
            pytest.fail(f"{host} should be allowed by the LAN policy")
        except OSError:
            pass  # refused / timed out — fine, the guard let it through
        finally:
            s.close()


def test_configured_extra_ip_is_allowed(guard):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.2)
    try:
        s.connect(("203.0.113.7", 9))
    except ef.EgressBlocked:
        pytest.fail("explicitly allow-listed IP must not be blocked")
    except OSError:
        pass
    finally:
        s.close()


def test_connect_ex_returns_errno_not_raises(guard):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    rc = s.connect_ex(("1.1.1.1", 443))
    s.close()
    assert rc != 0  # non-zero errno, and no exception escaped


def test_self_test_reports_isolated(guard):
    out = ef.self_test(targets=[("8.8.8.8", 53), ("1.1.1.1", 443)], timeout=1.0)
    assert out["verdict"] == "ISOLATED"
    assert out["leaked"] == 0
    assert all(r["outcome"] == "BLOCKED" for r in out["results"])


@pytest.mark.asyncio
async def test_api_status_and_self_test_endpoints(guard):
    from app.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.get("/api/egress-firewall")
        assert r.status_code == 200
        assert r.json()["enforcing"] is True
        assert "allowed_cidrs" in r.json()

        r = await ac.post("/api/egress-firewall/self-test")
        assert r.status_code == 200
        assert r.json()["verdict"] == "ISOLATED"
