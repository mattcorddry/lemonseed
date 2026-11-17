#!/bin/sh
# Start hook — runs whenever the container comes up, including first boot after
# install. The log line is the observable proof that the hook fired.
set -eu
: "${LEMONSEED_MODULE_DIR:?LEMONSEED_MODULE_DIR is required}"
printf '%s started\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$LEMONSEED_MODULE_DIR/start.log"
