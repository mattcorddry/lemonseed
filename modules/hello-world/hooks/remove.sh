#!/bin/sh
# Remove hook. The control plane has already stopped and removed the
# container; the data volume's fate is the Preserve data checkbox, not this
# script's business.
set -eu
: "${LEMONSEED_MODULE_DIR:?LEMONSEED_MODULE_DIR is required}"
echo "hello-world: remove hook ran (data volume: $LEMONSEED_MODULE_DIR)"
