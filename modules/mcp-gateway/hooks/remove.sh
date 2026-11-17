#!/bin/sh
# Remove hook. The control plane has already stopped and removed the
# container; the data volume's fate is the Preserve data checkbox.
set -eu
: "${LEMONSEED_MODULE_DIR:?LEMONSEED_MODULE_DIR is required}"
echo "mcp-gateway: remove hook ran (memory graph left at $LEMONSEED_MODULE_DIR/memory.jsonl)"
