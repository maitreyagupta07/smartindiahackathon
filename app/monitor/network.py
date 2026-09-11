"""
Person E's network monitor — adapted to the single-node architecture.

Same idea as ``demo/network_monitor.py`` (a psutil sweep of THIS PROCESS's
own live TCP connections, classified against a no-external-egress policy),
re-expressed as a library the running FastAPI app can query on demand
instead of a standalone 30-second CLI watch. Wireshark / Resource Monitor
stay the independent, packet-level verification on Person A's laptop; this
is only the app's own application-level self-report.

Scoped to THIS PROCESS (``psutil.Process(os.getpid())``), matching exactly
what the egress firewall (app/security/egress_firewall.py) enforces —
earlier versions swept every socket on the whole machine
(``psutil.net_connections``), which meant an unrelated browser tab, OS
telemetry, or another app on the same laptop showed up here as a false
"policy violation" even though the AI workload itself made no such call.
That is a real difference: this file's job is to prove THIS APPLICATION is
air-gapped, not to audit the whole machine — the egress firewall's own
blocked-attempt log is the enforcement proof for exactly this process, and
this sweep is now its honest, matching read-only witness.

Per established remote endpoint, the classification kept from the CLI tool:

  LOCAL       loopback (127.0.0.0/8, ::1) — this is also where Ollama on
              127.0.0.1:11434 lands, so Ollama traffic is always LOCAL.
  LAN_CLIENT  RFC1918 / link-local / the configured expected client(s) and
              this machine's own LAN address — expected, never a violation.
  EXTERNAL    a publicly routable address — should never appear.
  VIOLATION   an EXTERNAL endpoint (policy: the AI workload makes no calls
              off this machine except to the LAN client).

The obsolete pre-merge service ports 8001/8002 are NOT referenced anywhere
here — those processes don't exist in the single-node build.

Task correlation (``record_task_window`` / ``get_task_record``) is
application-level only and keyed by ``task_id`` in a lock-guarded dict, so
concurrent tasks each keep their own record — there is deliberately no
"current task" global. Packets are not claimed to carry task IDs; a task's
record is the monitor sampled at that task's start and end, tagged with its
id and timestamps.
"""
import asyncio
import ipaddress
import os
from datetime import datetime, timezone

try:  # psutil is already a dependency (see requirements.txt); guard anyway
    import psutil
except Exception:  # pragma: no cover - environment without psutil
    psutil = None

from ..storage.config import NETWORK_MONITOR

_EXPECTED_CLIENT_IPS = set(NETWORK_MONITOR.get("expected_client_ips") or [])
_EXPECTED_LAN_SERVER_IP = NETWORK_MONITOR.get("expected_lan_server_ip")

LOCAL = "LOCAL"
LAN_CLIENT = "LAN_CLIENT"
EXTERNAL = "EXTERNAL"
VIOLATION = "VIOLATION"

_MAX_TASK_RECORDS = 500
_TASK_NET: dict[str, dict] = {}
_TASK_NET_LOCK = asyncio.Lock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def classify_ip(ip: str) -> str:
    """LOCAL / LAN_CLIENT / EXTERNAL for one remote IP string."""
    if not ip or ip in ("0.0.0.0", "::", "*", "-"):
        return LOCAL
    if ip in _EXPECTED_CLIENT_IPS or (ip and ip == _EXPECTED_LAN_SERVER_IP):
        return LAN_CLIENT
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return LOCAL  # unparseable -> don't manufacture a false positive
    if addr.is_loopback or addr.is_unspecified:
        return LOCAL
    if addr.is_private or addr.is_link_local or addr.is_reserved or addr.is_multicast:
        return LAN_CLIENT
    return EXTERNAL


def _unavailable(reason: str) -> dict:
    return {
        "status": "UNAVAILABLE",
        "external_connections": 0,
        "external_ips": [],
        "policy_violations": 0,
        "categories": {"local": 0, "lan_client": 0, "external": 0, "violation": 0},
        "external_connection_detail": [],
        "last_checked": _now_iso(),
        "monitor_available": False,
        "detail": reason,
    }


def sample() -> dict:
    """
    One live sweep of THIS PROCESS's ESTABLISHED TCP connections (the AI
    workload itself — this FastAPI process, which is also the one the egress
    firewall guards), classified against the allow-policy. Cheap enough to
    call on every request; never raises (a failure is reported as status
    "UNAVAILABLE" with a reason).

    Deliberately process-scoped, not a whole-machine sweep: this same
    Windows laptop also runs a browser, OS services, etc, and those sockets
    have nothing to do with whether the AI workload is air-gapped. A
    machine-wide sweep would show their ordinary internet traffic as a
    "policy violation" here even though this application never made that
    call — a false alarm that undermines the very claim this panel exists
    to prove. Scoping to os.getpid() makes this an honest witness of the
    same boundary the egress firewall enforces.
    """
    if psutil is None:
        return _unavailable("psutil is not installed on this host")
    try:
        this_process = psutil.Process(os.getpid())
        get_conns = getattr(this_process, "net_connections", None) or this_process.connections
        conns = get_conns(kind="inet")
    except (psutil.AccessDenied, PermissionError):
        return _unavailable(
            "insufficient privileges to read this process's socket table "
            "(on Windows this endpoint needs an elevated backend process)"
        )
    except Exception as exc:  # noqa: BLE001
        return _unavailable(f"could not read the socket table: {exc}")

    established = getattr(psutil, "CONN_ESTABLISHED", "ESTABLISHED")
    counts = {LOCAL: 0, LAN_CLIENT: 0, EXTERNAL: 0}
    external_ips: set[str] = set()
    external_detail: list[dict] = []

    for c in conns:
        if c.status != established or not c.raddr:
            continue
        rip = getattr(c.raddr, "ip", None)
        if not rip:
            continue
        kind = classify_ip(rip)
        if kind == EXTERNAL:
            counts[EXTERNAL] += 1
            external_ips.add(rip)
            external_detail.append({
                "remote": f"{rip}:{getattr(c.raddr, 'port', '')}",
                "local_port": getattr(c.laddr, "port", None) if c.laddr else None,
            })
        else:
            counts[kind] += 1

    violations = len(external_ips)
    return {
        # SECURE is an EVIDENCE statement about this sweep, not a permanent
        # guarantee — the frontend phrases it as "N external connections
        # observed", never "zero external calls ever".
        "status": "SECURE" if counts[EXTERNAL] == 0 else "VIOLATIONS_DETECTED",
        "external_connections": counts[EXTERNAL],
        "external_ips": sorted(external_ips),
        "policy_violations": violations,
        "categories": {
            "local": counts[LOCAL],
            "lan_client": counts[LAN_CLIENT],
            "external": counts[EXTERNAL],
            "violation": violations,
        },
        "external_connection_detail": external_detail,
        "last_checked": _now_iso(),
        "monitor_available": True,
    }


async def record_task_window(task_id: str, phase: str) -> None:
    """
    Fold one monitor sample into the per-task record for ``task_id``.
    ``phase`` is "start" or "end". Concurrency-safe: the record is keyed by
    task_id under a lock, so overlapping tasks never clobber each other and
    there is no shared "current task" state. Never raises — a monitoring
    hiccup must not affect task execution.
    """
    if not task_id:
        return
    try:
        snap = sample()
    except Exception:  # noqa: BLE001 - belt-and-braces; sample() already guards
        return

    try:
        async with _TASK_NET_LOCK:
            rec = _TASK_NET.get(task_id) or {
                "task_id": task_id,
                "external_ips": set(),
                "external_connections": 0,
                "started_at": None,
                "ended_at": None,
                "last_checked": None,
                "monitor_available": snap["monitor_available"],
            }
            if phase == "start" and rec["started_at"] is None:
                rec["started_at"] = snap["last_checked"]
            if phase == "end":
                rec["ended_at"] = snap["last_checked"]
            rec["external_ips"].update(snap.get("external_ips") or [])
            rec["external_connections"] = max(
                rec["external_connections"], snap.get("external_connections", 0)
            )
            rec["monitor_available"] = snap["monitor_available"]
            rec["last_checked"] = snap["last_checked"]
            _TASK_NET[task_id] = rec

            # Bound memory — drop the oldest records once over the cap.
            if len(_TASK_NET) > _MAX_TASK_RECORDS:
                for stale in list(_TASK_NET)[: len(_TASK_NET) - _MAX_TASK_RECORDS]:
                    _TASK_NET.pop(stale, None)
    except Exception:  # noqa: BLE001
        return


async def get_task_record(task_id: str) -> dict | None:
    """The network-security record accumulated for one task, or None."""
    async with _TASK_NET_LOCK:
        rec = _TASK_NET.get(task_id)
        if not rec:
            return None
        external_ips = sorted(rec["external_ips"])
        violations = len(external_ips)
        if not rec["monitor_available"]:
            status = "UNAVAILABLE"
        elif violations:
            status = "VIOLATIONS_DETECTED"
        else:
            status = "SECURE"
        return {
            "task_id": task_id,
            "status": status,
            "external_connections": rec["external_connections"],
            "external_ips": external_ips,
            "policy_violations": violations,
            "started_at": rec["started_at"],
            "ended_at": rec["ended_at"],
            "last_checked": rec["last_checked"],
            "monitor_available": rec["monitor_available"],
        }
