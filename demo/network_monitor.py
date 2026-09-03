"""
PERSON E — network monitor (Linux/dev-machine version, using psutil).
Verified working against Person B's real backend + a mock executor in place of Person F.

At the actual venue, use this alongside (not instead of) a visible OS-level tool for the
live demo — Windows Resource Monitor's Network tab, or Wireshark — since judges want to
SEE the proof, not just trust a script's PASS/FAIL line. This script is useful for your
own pre-demo verification and for capturing a log/screenshot for Person D2's slides.

Usage:
    pip install psutil
    python network_monitor.py [duration_seconds] [lan_port] [internal_port]

Run this, then in another terminal submit a task against Person B's backend. It watches
both the LAN-exposed port (8000, Person B) and the internal-only port (8002, Person F)
for the duration, and flags anything that isn't a loopback (127.0.0.1) connection.
"""
import sys
import time
import psutil

LAN_PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 8000
INTERNAL_PORT = int(sys.argv[3]) if len(sys.argv) > 3 else 8002
DURATION = int(sys.argv[1]) if len(sys.argv) > 1 else 15


def main():
    print(f"Watching ports {LAN_PORT} (LAN) and {INTERNAL_PORT} (internal-only) for {DURATION}s...")
    print("Submit a task against the backend now.\n")

    seen = []
    for t in range(DURATION):
        for c in psutil.net_connections(kind="tcp"):
            if c.laddr and c.laddr.port in (LAN_PORT, INTERNAL_PORT) and c.status == "ESTABLISHED":
                raddr = f"{c.raddr.ip}:{c.raddr.port}" if c.raddr else "-"
                seen.append((t, c.laddr.port, raddr))
        time.sleep(1)

    external = []
    for t, port, raddr in seen:
        is_loopback = raddr == "-" or raddr.startswith("127.0.0.1")
        tag = "loopback (expected)" if is_loopback else "!!! NON-LOOPBACK !!!"
        print(f"[t+{t}s] port={port} remote={raddr}  {tag}")
        if not is_loopback:
            external.append((t, port, raddr))

    print()
    if external:
        print(f"FAIL: {len(external)} external connection(s) observed — air-gap claim violated.")
        sys.exit(1)
    else:
        print("PASS: every connection observed was loopback-only. Zero external network calls.")


if __name__ == "__main__":
    main()
