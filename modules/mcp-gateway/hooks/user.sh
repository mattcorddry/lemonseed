#!/bin/sh
# User-CRUD observer: stdin carries the JSON payload from usermgmt's observer
# list. The gateway has no per-user state today; append for auditability.
set -eu
: "${LEMONSEED_MODULE_DIR:?LEMONSEED_MODULE_DIR is required}"
printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$(cat)" >> "$LEMONSEED_MODULE_DIR/users.log"
