# Person E — Audit, Network Proof & Demo Logistics

Back-loaded work (Part 3) — most of this can't start for real until Person B's backend
(this repo's `/backend`) is up and reachable. Track progress here; this is not code.

## 1. LAN reachability (build step 1)
- [ ] Confirm `main.py` binds `0.0.0.0` (already done in `/backend/main.py`), not `localhost`.
- [ ] Confirm laptop firewall allows inbound on port 8000.
- [ ] Test from the actual Mac, on the actual venue WiFi if possible, not just home WiFi.
- [ ] Check for client isolation on venue WiFi (common at conferences/hackathons) — if present,
      switch to a personal mobile hotspot. Test the hotspot fallback *before* demo day.

## 2. Network monitor (build step 2)
- [ ] Choose: Windows Resource Monitor (Network tab, built-in) or Wireshark (more detail).
- [ ] Confirm it clearly shows zero bytes to any non-LAN IP during a full task run.

## 3. "Disconnect the internet" moment (build step 3)
- [ ] Identify what gets unplugged/disabled (router WAN uplink, or hotspot's internet sharing)
      while keeping laptop<->Mac WiFi alive.
- [ ] Pre-test this at least once well before demo day — run a real task with internet down,
      confirm it still completes normally.

## 4. Backup video (build step 4)
- [ ] Record a full working run-through a few days before the event.

## 5. Worst-case resource test (build step 5) — do this once the full stack is integrated
- [ ] Trigger a code-execution task and a model-generation task simultaneously.
- [ ] Confirm neither chokes (watch CPU/RAM — Docker + vector DB + inference all contending).

## 6. Demo script + rehearsal (build step 6)
Map every line of the script to Part 5's checklist:
- [ ] Two different task types → two different models, shown visibly
- [ ] Full agentic task: scanned report → findings → real .docx out, unattended
- [ ] Coding task that actually executes + gets verified
- [ ] Image/scanned-doc understanding example
- [ ] Network monitor at zero throughout, incl. live disconnect
- [ ] Two simultaneous text-task users, overlapping timestamps visible on Person D1's dashboard
- [ ] Visible queueing shown for simultaneous vision tasks
- [ ] Synced with Person D2's slide deck timing
- [ ] Full rehearsal run at least twice, timed

## Notes / screenshots for Person D2
(Drop terminal output, timestamp comparisons, and monitor screenshots here as they're captured —
D2 pulls from this folder for the pitch deck per Part 3.)

---

## Verified so far (sandbox run, with a mock standing in for Person F — re-run once F is real)

**Parallelism proof — real captured data:**
Two tasks submitted at the literal same moment both showed:
`started_at: 2026-09-02T21:21:55Z`, `completed_at: 2026-09-02T21:21:58Z` — genuinely overlapping,
not sequential. This is what Person D1's dashboard should visibly render on two task cards.

**Network proof — real captured data (`network_monitor.py`):**
```
[t+1s] port=8002 remote=127.0.0.1:38050  loopback (expected)
[t+2s] port=8002 remote=127.0.0.1:38050  loopback (expected)
[t+3s] port=8002 remote=127.0.0.1:38050  loopback (expected)

PASS: every connection observed was loopback-only. Zero external network calls.
```
This is on a dev machine (no real LAN, no real Mac) — it proves the *code* doesn't reach
outside localhost. It does NOT yet prove the *venue WiFi* setup works (build step 1) — that
needs the real hardware and a second physical machine, which is still open.

**Still genuinely untested (needs real hardware/venue, can't be done from a sandbox):**
- [ ] Reachability from a second physical machine over real WiFi
- [ ] Client isolation check at the actual venue
- [ ] Physical internet-disconnect test
- [ ] Backup video recording
- [ ] Worst-case resource test (needs Person C's and Person A/F's real services, not mocks)
- [ ] Full rehearsal timing
