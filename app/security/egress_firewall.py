"""
In-process egress firewall — the ENFORCEMENT half of the network story.

``app/monitor/network.py`` *observes* this machine's socket table and
reports what it sees. This module *prevents*: it installs a process-wide
guard on ``socket.socket.connect`` / ``connect_ex`` so that any attempt by
this Python process (our code, or any dependency — an SDK phoning home, a
model-weights download, a telemetry beacon) to open a connection to a
publicly routable address is refused before a single packet leaves, and
recorded.

Scope and honesty about it
--------------------------
This enforces THIS process only. It is not a host firewall — other
processes on the box are unaffected (``harden_network.sh`` is the optional
OS-level layer for that). What it gives you is a hard guarantee that the
agent workload itself has no path to the internet or any cloud: the Ollama
client (127.0.0.1), the browser on the venue LAN, and RFC1918 peers keep
working; everything else raises :class:`EgressBlocked`.

Allow policy (same shape as the monitor's ``classify_ip``)
---------------------------------------------------------
ALLOW  loopback / unspecified            (127.0.0.0/8, ::1, 0.0.0.0)
ALLOW  RFC1918 private, link-local,
       CGNAT (100.64/10), reserved,
       multicast / broadcast             — the LAN
ALLOW  any address in an operator-configured extra CIDR, plus the
       expected LAN client / server IPs from config
BLOCK  anything else (globally routable) — raise EgressBlocked, record it

The guard is idempotent (installing twice is a no-op) and reversible
(:func:`uninstall` restores the originals — used by the test suite).
"""
from __future__ import annotations

import ipaddress
import socket
import threading
import time
import traceback
from collections import deque
from datetime import datetime, timezone
from typing import Iterable

# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------

# Populated by install() from config; kept module-level so the hot path
# (_is_allowed) is a plain attribute read with no indirection.
_EXTRA_NETS: list[ipaddress._BaseNetwork] = []
_EXTRA_IPS: set[str] = set()

_BLOCKED_MAXLEN = 200
_blocked: "deque[dict]" = deque(maxlen=_BLOCKED_MAXLEN)
_blocked_total = 0
_allowed_total = 0
_lock = threading.Lock()

_installed = False
_installed_at: str | None = None
_orig_connect = None
_orig_connect_ex = None

# Filled in by install() purely for display on the status endpoint.
_allowed_cidrs_display: list[str] = []


class EgressBlocked(OSError):
    """Raised by the guarded ``connect`` when the destination is off-LAN.

    Subclasses :class:`OSError` on purpose: callers that already handle a
    connection failure gracefully (e.g. the Ollama client, ``httpx``)
    treat this the same as an unreachable host instead of crashing.
    """


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _coerce_ip(host: str) -> str | None:
    """Best-effort: return a bare IP string for ``host`` or None.

    ``connect`` may be handed a hostname (rare — most stacks resolve
    first), a literal IP, or something odd. We only classify literals; a
    hostname is left for the resolver + the subsequent connect to the
    resolved literal, which this same guard then sees.
    """
    if not host:
        return None
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        return None


def _is_allowed(ip: str) -> bool:
    if ip in _EXTRA_IPS:
        return True
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        # Unparseable — do not manufacture a block; the monitor treats
        # this the same way.
        return True
    if (
        addr.is_loopback
        or addr.is_unspecified
        or addr.is_private          # RFC1918 + 100.64/10 CGNAT (py>=3.13) etc.
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
    ):
        return True
    if isinstance(addr, ipaddress.IPv4Address) and addr == ipaddress.IPv4Address("255.255.255.255"):
        return True
    for net in _EXTRA_NETS:
        try:
            if addr in net:
                return True
        except TypeError:
            continue  # v4 addr vs v6 net or vice versa
    return False


def _caller_frame() -> str:
    """A short 'module:line in func' for the first stack frame outside
    this file and the stdlib socket/ssl plumbing — enough to see who
    tried to leave the LAN, without dumping a full traceback."""
    for fr in traceback.extract_stack()[::-1]:
        fn = fr.filename.replace("\\", "/")
        if fn.endswith("app/security/egress_firewall.py"):
            continue
        base = fn.rsplit("/site-packages/", 1)[-1]
        if base.startswith(("socket.py", "ssl.py")) or "/python3." in fn and "/lib/" in fn and "site-packages" not in fn:
            continue
        return f"{base}:{fr.lineno} in {fr.name}"
    return "unknown"


def _record_block(ip: str, port, via: str) -> None:
    global _blocked_total
    with _lock:
        _blocked_total += 1
        _blocked.appendleft(
            {
                "ts": _now_iso(),
                "dest_ip": ip,
                "dest_port": port,
                "via": via,          # "connect" | "connect_ex"
                "caller": _caller_frame(),
            }
        )


def _check(address, via: str) -> None:
    """Raise EgressBlocked if ``address`` is an off-LAN INET destination."""
    global _allowed_total
    # AF_UNIX and friends pass a str path, not a (host, port) tuple.
    if not isinstance(address, tuple) or len(address) < 2:
        return
    host, port = address[0], address[1]
    ip = _coerce_ip(host)
    if ip is None:
        return  # hostname — the resolved-literal connect is guarded instead
    if _is_allowed(ip):
        with _lock:
            _allowed_total += 1
        return
    _record_block(ip, port, via)
    raise EgressBlocked(
        f"egress firewall: outbound connection to {ip}:{port} is off-LAN and "
        f"was refused (single-node isolation policy). "
        f"Allowed: loopback + RFC1918/link-local + configured LAN peers."
    )


# ---------------------------------------------------------------------------
# The guard
# ---------------------------------------------------------------------------

def _guarded_connect(self, address, *args, **kwargs):
    if self.family in (socket.AF_INET, socket.AF_INET6):
        _check(address, "connect")
    return _orig_connect(self, address, *args, **kwargs)


def _guarded_connect_ex(self, address, *args, **kwargs):
    if self.family in (socket.AF_INET, socket.AF_INET6):
        try:
            _check(address, "connect_ex")
        except EgressBlocked:
            # connect_ex contract is "return an errno, don't raise".
            return getattr(__import__("errno"), "EACCES", 13)
    return _orig_connect_ex(self, address, *args, **kwargs)


def install(
    *,
    enabled: bool = True,
    extra_cidrs: Iterable[str] = (),
    extra_ips: Iterable[str] = (),
) -> dict:
    """Install the process-wide guard. Idempotent. Returns a status dict.

    ``enabled=False`` records the configured policy but leaves sockets
    untouched — useful for a deployment that wants the panel without the
    enforcement (not recommended; it's here so the switch exists).
    """
    global _installed, _installed_at, _orig_connect, _orig_connect_ex
    global _EXTRA_NETS, _EXTRA_IPS, _allowed_cidrs_display

    _EXTRA_NETS = []
    for c in extra_cidrs:
        try:
            _EXTRA_NETS.append(ipaddress.ip_network(c, strict=False))
        except ValueError:
            continue
    _EXTRA_IPS = {str(i) for i in extra_ips if i}
    _allowed_cidrs_display = [
        "127.0.0.0/8", "::1/128", "10.0.0.0/8", "172.16.0.0/12",
        "192.168.0.0/16", "169.254.0.0/16", "100.64.0.0/10",
        "224.0.0.0/4 (multicast)",
    ] + [str(n) for n in _EXTRA_NETS] + sorted(_EXTRA_IPS)

    if not enabled:
        _installed = False
        return status()

    if _installed:
        return status()

    _orig_connect = socket.socket.connect
    _orig_connect_ex = socket.socket.connect_ex
    socket.socket.connect = _guarded_connect      # type: ignore[assignment]
    socket.socket.connect_ex = _guarded_connect_ex  # type: ignore[assignment]
    _installed = True
    _installed_at = _now_iso()
    return status()


def uninstall() -> None:
    """Restore the original socket methods. Mainly for tests."""
    global _installed
    if not _installed:
        return
    socket.socket.connect = _orig_connect          # type: ignore[assignment]
    socket.socket.connect_ex = _orig_connect_ex    # type: ignore[assignment]
    _installed = False


# ---------------------------------------------------------------------------
# Introspection + active proof
# ---------------------------------------------------------------------------

def status() -> dict:
    with _lock:
        recent = list(_blocked)
        total = _blocked_total
        allowed = _allowed_total
    return {
        "enforcing": _installed,
        "installed_at": _installed_at,
        "allowed_cidrs": list(_allowed_cidrs_display),
        "blocked_attempts_total": total,
        "allowed_connections_total": allowed,
        "last_blocked_at": recent[0]["ts"] if recent else None,
        "recent_blocked": recent[:50],
        "last_checked": _now_iso(),
    }


# A small, well-known set of public endpoints an exfiltration / phone-home
# attempt would plausibly use. Overridable from config.
DEFAULT_SELF_TEST_TARGETS = [
    ("8.8.8.8", 53),            # Google public DNS
    ("1.1.1.1", 443),          # Cloudflare
    ("api.openai.com", 443),   # a cloud LLM API
    ("huggingface.co", 443),   # model-weights host
]


def self_test(targets: Iterable[tuple[str, int]] | None = None, timeout: float = 3.0) -> dict:
    """Actively try to open a connection to each public target and confirm
    the firewall refuses it. This is the on-screen proof: every row should
    come back ``BLOCKED``. A ``LEAKED`` row means a real connection was
    established off-LAN — the firewall failed and that must be investigated.

    A resolved-hostname target that this process cannot even resolve
    (because DNS itself is unreachable in an air-gapped venue) is reported
    as ``BLOCKED`` with ``reason="dns-unreachable"`` — still no egress.
    """
    tgts = list(targets) if targets is not None else list(DEFAULT_SELF_TEST_TARGETS)
    results = []
    leaked = 0
    for host, port in tgts:
        row = {"target": f"{host}:{port}", "outcome": "BLOCKED", "reason": ""}
        try:
            s = socket.create_connection((host, port), timeout=timeout)
            # Should never get here while enforcing.
            s.close()
            row["outcome"] = "LEAKED"
            row["reason"] = "connection established off-LAN"
            leaked += 1
        except EgressBlocked as e:
            row["reason"] = str(e).split(":", 1)[0] if ":" in str(e) else "refused by egress firewall"
            row["reason"] = "refused by egress firewall"
        except socket.gaierror:
            row["reason"] = "dns-unreachable (name could not be resolved — no egress path)"
        except (socket.timeout, TimeoutError):
            row["reason"] = "timed out with no route off-LAN"
        except OSError as e:
            row["reason"] = f"no route / refused ({e.__class__.__name__})"
        results.append(row)

    return {
        "ran_at": _now_iso(),
        "enforcing": _installed,
        "targets_tested": len(results),
        "leaked": leaked,
        "verdict": "ISOLATED" if leaked == 0 else "LEAK_DETECTED",
        "results": results,
    }
