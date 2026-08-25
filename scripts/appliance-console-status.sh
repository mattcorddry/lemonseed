#!/usr/bin/env bash
# Appliance status splash (tty4) — URL + IPs while the portal comes up.
set -euo pipefail

TTY="${1:-/dev/tty4}"

addresses() {
  local iface addr
  for iface in $(ip -o link show up 2>/dev/null | awk -F': ' '{print $2}'); do
    case "$iface" in
      eth*|en*|wlan*|wl*) ;;
      *) continue ;;
    esac
    while read -r addr; do
      [[ -n "$addr" && "$addr" != 169.254.* ]] && printf '%s\n' "$addr"
    done < <(ip -4 -o addr show dev "$iface" scope global 2>/dev/null | awk '{print $4}' | cut -d/ -f1)
  done
}

portal_url() {
  local name
  name="$(uname -n 2>/dev/null || true)"
  if [[ -z "${name}" || "${name}" == "localhost" ]]; then
    name="lemonseed"
  fi
  printf 'http://%s.local/' "${name}"
}

stty cols 80 rows 24 <"${TTY}" 2>/dev/null || true

while true; do
  url="$(portal_url)"
  addrs="$(addresses | sed '/^$/d' | sed 's/^/    /')"
  if [[ -z "${addrs}" ]]; then
    addrs="    (waiting for network…)"
  fi

  if [[ -e /etc/appliance-setup-complete ]]; then
    mode="Appliance is set up"
    hint="Open the address below for the Lemonseed portal."
  else
    mode="First-time setup"
    hint="Open the address below to finish setting up this appliance."
  fi

  {
    printf '\033[2J\033[H'
    cat <<EOF
Lemonseed
---------

${mode}

${hint}

    ${url}

If that name does not work, try:

${addrs}

Debug shell: Ctrl+Alt+F2 (root)
Return here: Ctrl+Alt+F4
EOF
  } >"${TTY}" 2>/dev/null || true

  sleep 5
done
