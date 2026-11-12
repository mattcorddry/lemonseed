#!/bin/sh
# Install hook — the contract exercise (MODULES.md §7): runs as root, from the
# image path, with the module env file (settings included) and the JSON event
# payload on stdin. The data volume is already provisioned by the control
# plane, so writing into it is safe here.
set -eu
: "${LEMONSEED_MODULE_DIR:?LEMONSEED_MODULE_DIR is required}"
: "${LEMONSEED_MODULE_ID:?LEMONSEED_MODULE_ID is required}"

printf '%s\n' "${GREETING:-Hello}" > "$LEMONSEED_MODULE_DIR/greeting.txt"
echo "hello-world: installed ($LEMONSEED_MODULE_ID), greeting written"
