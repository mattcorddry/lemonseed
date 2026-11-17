#!/bin/sh
# Install hook — the data volume is already provisioned; nothing to write, but
# the contract wants the run to be observable.
set -eu
: "${LEMONSEED_MODULE_DIR:?LEMONSEED_MODULE_DIR is required}"
: "${LEMONSEED_MODULE_ID:?LEMONSEED_MODULE_ID is required}"

echo "mcp-gateway: installed ($LEMONSEED_MODULE_ID), data volume at $LEMONSEED_MODULE_DIR"
