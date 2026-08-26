#!/bin/sh
set -e

# network_mode: service:tailscale — no Docker embedded DNS (127.0.0.11).
# Tailscale may also leave resolv.conf empty briefly at boot.
if [ -w /etc/resolv.conf ]; then
  cat >/etc/resolv.conf <<'EOF'
nameserver 1.1.1.1
nameserver 8.8.8.8
options ndots:0
EOF
fi

exec "$@"
