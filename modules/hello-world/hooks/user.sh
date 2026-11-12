#!/bin/sh
# User-CRUD observer (MODULES.md §7): stdin carries the JSON payload the
# control plane read from usermgmt's observer list. Appending the raw line is
# enough to prove the wiring; a real module would parse it.
set -eu
: "${LEMONSEED_MODULE_DIR:?LEMONSEED_MODULE_DIR is required}"
printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$(cat)" >> "$LEMONSEED_MODULE_DIR/users.log"
