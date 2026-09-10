#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# harden_network.sh — OPTIONAL host-level egress lockdown (Option 2 / the
# outer layer). The always-on protection is the in-process egress firewall
# in app/security/egress_firewall.py, which needs no privileges and is what
# the admin UI proves. THIS script goes further: it tells the whole
# operating system to drop every outbound packet that is not headed for
# loopback or an RFC1918 LAN, so *no* process on the box — not just this
# app — can reach the internet.
#
# It is deliberately small. Run it on the deployment host, as root, once:
#
#     sudo ./harden_network.sh apply      # install the rules
#     sudo ./harden_network.sh status     # show what's active
#     sudo ./harden_network.sh revert     # remove the rules
#     ./harden_network.sh dry-run         # print rules, change nothing
#
# Allowed egress: lo, 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16,
# 169.254.0.0/16, 224.0.0.0/4 (multicast), plus DHCP. Everything else DROP.
# ---------------------------------------------------------------------------
set -euo pipefail

LAN_NETS=(10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 169.254.0.0/16 224.0.0.0/4)
ACTION="${1:-dry-run}"
OS="$(uname -s)"

_linux_rules() {
  cat <<EOF
# nftables — inet filter, output hook
table inet lan_only {
  chain output {
    type filter hook output priority 0; policy drop;
    oif "lo" accept
    ct state established,related accept
    udp dport 67-68 accept
$(for n in "${LAN_NETS[@]}"; do echo "    ip daddr $n accept"; done)
    ip6 daddr fe80::/10 accept
    ip6 daddr ff00::/8 accept
    # everything else: dropped by policy
  }
}
EOF
}

_macos_rules() {
  cat <<EOF
# pf — /etc/pf.anchor/lan_only  (load with: anchor "lan_only" in pf.conf)
set block-policy drop
pass out quick on lo0 all
pass out quick proto udp from any to any port { 67, 68 }
$(for n in "${LAN_NETS[@]}"; do echo "pass out quick from any to $n"; done)
pass out quick inet6 from any to fe80::/10
block out log all
EOF
}

case "$ACTION" in
  dry-run)
    echo "# OS detected: $OS  (dry-run — nothing will change)"
    if [ "$OS" = "Linux" ]; then _linux_rules; else _macos_rules; fi
    ;;
  apply)
    [ "$(id -u)" -eq 0 ] || { echo "must be root"; exit 1; }
    if [ "$OS" = "Linux" ]; then
      command -v nft >/dev/null || { echo "nftables (nft) not installed"; exit 1; }
      _linux_rules | nft -f -
      echo "[ok] nftables lan_only table installed — non-LAN egress is now dropped host-wide."
      echo "     revert with: sudo $0 revert"
    else
      _macos_rules > /etc/pf.anchor.lan_only
      grep -q 'anchor "lan_only"' /etc/pf.conf || {
        printf '\nanchor "lan_only"\nload anchor "lan_only" from "/etc/pf.anchor.lan_only"\n' >> /etc/pf.conf
      }
      pfctl -f /etc/pf.conf && pfctl -e 2>/dev/null || true
      echo "[ok] pf lan_only anchor loaded — non-LAN egress is now dropped host-wide."
      echo "     revert with: sudo $0 revert"
    fi
    ;;
  revert)
    [ "$(id -u)" -eq 0 ] || { echo "must be root"; exit 1; }
    if [ "$OS" = "Linux" ]; then
      nft delete table inet lan_only 2>/dev/null && echo "[ok] removed nftables lan_only table." || echo "[..] no lan_only table present."
    else
      sed -i '' -e '/anchor "lan_only"/d' -e '/load anchor "lan_only"/d' /etc/pf.conf 2>/dev/null || true
      rm -f /etc/pf.anchor.lan_only
      pfctl -f /etc/pf.conf 2>/dev/null || true
      echo "[ok] removed pf lan_only anchor."
    fi
    ;;
  status)
    if [ "$OS" = "Linux" ]; then nft list table inet lan_only 2>/dev/null || echo "lan_only table not installed"; \
    else pfctl -a lan_only -sr 2>/dev/null || echo "lan_only anchor not loaded"; fi
    ;;
  *)
    echo "usage: $0 {apply|revert|status|dry-run}"; exit 1;;
esac
