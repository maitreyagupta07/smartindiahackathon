"""
Network-security status (Person E's monitor) exposed through the existing
single FastAPI app — no new service, no new port.

  GET /api/network-status            current/global sweep of this machine's
                                     own outbound TCP connections.
  GET /api/network-status/{task_id}  the record accumulated for one task
                                     (sampled at that task's start and end).

Both return clean JSON with: status, external_connections, external_ips,
policy_violations, last_checked (and task_id for the task-specific one).
"""
from fastapi import APIRouter, HTTPException

from ..monitor import network

router = APIRouter()


@router.get("/api/network-status")
async def network_status():
    """Live application-level check. Independent OS-level tools (Wireshark /
    Resource Monitor) remain the packet-level proof — this is the app's own
    self-report, and it does not attribute individual packets to task IDs."""
    return network.sample()


@router.get("/api/network-status/{task_id}")
async def task_network_status(task_id: str):
    record = await network.get_task_record(task_id)
    if record is None:
        raise HTTPException(
            status_code=404,
            detail="no network-security record for this task_id (unknown, or it ran before this build)",
        )
    return record
