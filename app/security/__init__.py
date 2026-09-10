"""Runtime security enforcement for the single-node Workbench.

Currently one module: :mod:`app.security.egress_firewall`, an in-process
outbound-connection guard that makes "this process cannot reach anything
off the local LAN" an enforced property rather than an observed one.
"""
