#!/bin/sh
# Stop hook — best effort by contract: the control plane tears the container
# down either way.
set -eu
: "${LEMONSEED_MODULE_DIR:?LEMONSEED_MODULE_DIR is required}"
printf '%s stopped\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$LEMONSEED_MODULE_DIR/stop.log"
