"""
Network-security status (Person E's monitor) exposed through the existing
single FastAPI app — no new service, no new port.

  GET /api/network-status            current/global sweep of this machine's
                                     own outbound TCP connections.
  GET /api/network-status/{task_id}  the record accumulated for one task
                                     (sampled at that task's start and end).

Both return clean JSON with: status, external_connections, external_ips,
policy_violations, last_checked (and task_id for the task-specific one).

  GET  /api/egress-firewall            current state of the in-process
                                       egress firewall (the ENFORCEMENT
                                       layer): whether it is enforcing, the
                                       allow-list, and a live log of
                                       blocked off-LAN connection attempts.
  POST /api/egress-firewall/self-test  actively try to open a connection to
                                       a set of public endpoints and prove
                                       every one is refused. This is the
                                       on-screen isolation proof.
"""
from fastapi import APIRouter, Depends, HTTPException

from .auth import current_admin
from ..monitor import network
from ..security import egress_firewall
from ..storage.config import EGRESS_FIREWALL

router = APIRouter()


@router.get("/api/network-status")
async def network_status(_admin: str = Depends(current_admin)):
    """Live application-level check. Independent OS-level tools (Wireshark /
    Resource Monitor) remain the packet-level proof — this is the app's own
    self-report, and it does not attribute individual packets to task IDs."""
    return network.sample()


@router.get("/api/network-status/{task_id}")
async def task_network_status(task_id: str, _admin: str = Depends(current_admin)):
    record = await network.get_task_record(task_id)
    if record is None:
        raise HTTPException(
            status_code=404,
            detail="no network-security record for this task_id (unknown, or it ran before this build)",
        )
    return record


@router.get("/api/egress-firewall")
async def egress_firewall_status():
    """State of the in-process egress firewall — the enforcement layer that
    refuses (not just observes) any outbound connection from this process
    to a publicly routable address. Includes a live ring buffer of the
    most recent blocked attempts (destination, port, and the caller that
    tried to leave the LAN)."""
    return egress_firewall.status()


@router.post("/api/egress-firewall/self-test")
async def egress_firewall_self_test():
    """Actively attempt to reach a set of well-known public endpoints
    (DNS resolvers, a cloud LLM API, a model-weights host) and confirm the
    firewall refuses every one. ``verdict`` is ``ISOLATED`` when nothing
    leaked. Runs the real connection attempts in a worker thread so the
    event loop is never blocked by the socket timeouts."""
    import anyio

    targets = EGRESS_FIREWALL["self_test_targets"] or None
    return await anyio.to_thread.run_sync(egress_firewall.self_test, targets)
