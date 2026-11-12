#!/bin/sh
# Stop hook — runs when the module is disabled or before a remove. Best effort
# by contract: the control plane tears the container down either way, so this
# is the module's last word, not its last stand.
set -eu
: "${LEMONSEED_MODULE_DIR:?LEMONSEED_MODULE_DIR is required}"
printf '%s stopped\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$LEMONSEED_MODULE_DIR/stop.log"
