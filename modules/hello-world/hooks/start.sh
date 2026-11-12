#!/bin/sh
# Start hook — runs whenever the module's container is brought up, including
# the first boot after install (install implies enable). The log line is the
# observable proof that the hook fired without a browser.
set -eu
: "${LEMONSEED_MODULE_DIR:?LEMONSEED_MODULE_DIR is required}"
printf '%s started\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$LEMONSEED_MODULE_DIR/start.log"
