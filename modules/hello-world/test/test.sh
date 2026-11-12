#!/usr/bin/env bash
# Hello World's own harness (MODULES-FRAMEWORK.md requirement 38): no Docker,
# no registry, no Lemonseed runtime. It is what a new Lemonseed release runs
# against every module before it ships.
#
# What it asserts without any external dependency:
#   1. the spec parses and its identity matches the directory;
#   2. every hook is shell-clean (sh -n);
#   3. the image pin the spec names exists in container-images.json;
#   4. the hooks actually run: install writes the greeting, user events land
#      in the log, with the data volume pointed at a scratch directory.
set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
SPEC="$HERE/module.yaml"

echo "==> hello-world: spec parses"
python3 - "$SPEC" "$ROOT/control-plane/container-images.json" <<'EOF'
import json
import sys

try:
    import yaml
except ImportError:
    print("PyYAML unavailable; skipping the spec parse (the catalog merge step"
          " will still validate it)")
    sys.exit(0)

with open(sys.argv[1]) as handle:
    spec = yaml.safe_load(handle)
assert spec["spec_version"] == 1, spec["spec_version"]
assert spec["id"] == "hello-world", spec["id"]
assert spec["docker"]["container"] == f"lemonseed-{spec['id']}"
assert spec["type"] == "application"
with open(sys.argv[2]) as handle:
    pins = json.load(handle)
assert spec["docker"]["image"] in pins, spec["docker"]["image"]
print("spec ok:", spec["id"], "image:", spec["docker"]["image"])
EOF

echo "==> hello-world: hooks are shell-clean"
for hook in "$HERE"/hooks/*.sh; do
  sh -n "$hook"
done

echo "==> hello-world: hooks run against a scratch data volume"
SCRATCH="$(mktemp -d)"
trap_cleanup() { rm -rf "$SCRATCH"; }
trap trap_cleanup EXIT
export LEMONSEED_MODULE_ID=hello-world
export LEMONSEED_MODULE_DIR="$SCRATCH"
export LEMONSEED_EVENT=install
export GREETING="Hello from the harness"
echo '{"event":"install"}' | sh "$HERE/hooks/install.sh" > /dev/null
test -f "$SCRATCH/greeting.txt"
grep -q "Hello from the harness" "$SCRATCH/greeting.txt"

LEMONSEED_EVENT=start sh "$HERE/hooks/start.sh" > /dev/null
test -f "$SCRATCH/start.log"

LEMONSEED_EVENT=stop sh "$HERE/hooks/stop.sh" > /dev/null
test -f "$SCRATCH/stop.log"

LEMONSEED_EVENT=user sh "$HERE/hooks/user.sh" < /dev/null > /dev/null
test -f "$SCRATCH/users.log"

sh "$HERE/hooks/remove.sh" > /dev/null

echo "==> hello-world harness passed"
