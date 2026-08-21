#!/usr/bin/env bash
# Installer status splash for the Lemonseed USB installer.
# Intended for tty4 (see nixos/installer/modules/console.nix) so we do not
# race getty on tty1.
set -euo pipefail

TTY="${1:-/dev/tty4}"

addresses() {
  ip -4 -o addr show up scope global 2>/dev/null \
    | awk '{print $4}' \
    | cut -d/ -f1 \
    || true
}

# Reasonable geometry when the VT has no controlling job yet.
stty cols 80 rows 24 <"${TTY}" 2>/dev/null || true

while true; do
  addrs="$(addresses | sed '/^$/d' | sed 's/^/    /')"
  if [[ -z "${addrs}" ]]; then
    addrs="    (waiting for network…)"
  fi

  {
    printf '\033[2J\033[H'
    cat <<EOF
Lemonseed installer
-------------------

Open this address on your phone or laptop
(same Wi-Fi / Ethernet network):

    http://lemonseed.local/

If that name does not work, try an IP below
in your browser:

${addrs}

Leave this USB stick plugged in until the
web installer tells you to remove it.

Debug shell: Ctrl+Alt+F2 (root)
Return here: Ctrl+Alt+F4
EOF
  } >"${TTY}" 2>/dev/null || true

  sleep 5
done
