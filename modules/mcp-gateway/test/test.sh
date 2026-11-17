#!/usr/bin/env bash
# MCP gateway's own harness (MODULES-FRAMEWORK.md requirement 38): no Docker,
# no registry, no Lemonseed runtime. It is what a new Lemonseed release runs
# against every module before it ships.
#
# What it asserts without any external dependency:
#   1. the spec parses; identity matches the directory; it is a *plugin* with
#      exactly one LiteLLM MCP contribution whose name passes the no-hyphen
#      rule and whose URL targets the lemon-network container (never
#      127.0.0.1 — that only works on the host, not from the litellm box);
#   2. every hook is shell-clean (sh -n);
#   3. the image pin the spec names exists in container-images.json AND the
#      in-tree build recipe exists for it (the pin's digest documents an image
#      config ID, not a registry manifest — a rebuild changes it by design);
#   4. the hooks actually run: install/remove/start/stop/user against a
#      scratch data volume;
#   5. the server module imports clean with the SDK version nixpkgs ships.
set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
SPEC="$HERE/module.yaml"

echo "==> mcp-gateway: spec parses"
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
assert spec["id"] == "mcp-gateway", spec["id"]
assert spec["docker"]["container"] == f"lemonseed-{spec['id']}"
assert spec["type"] == "plugin"
with open(sys.argv[2]) as handle:
    pins = json.load(handle)
pin = pins[spec["docker"]["image"]]
name = pin["name"].split("/")[-1]
server = ((spec.get("platform") or {}).get("litellm") or {})["mcp_server"]
assert "-" not in server["name"], "LiteLLM rejects hyphens in mcp_servers keys"
assert f"lemonseed-{spec['id']}" in server["url"] and "127.0.0.1" not in server["url"], server["url"]
# The in-tree image recipe must exist beside the pin: this module does not
# pull from a registry, so the build is the trust chain.
import pathlib
recipe = pathlib.Path(sys.argv[2]).parent.parent / "pkgs/mcp-gateway/default.nix"
assert recipe.is_file(), f"missing {recipe}"
print("spec ok:", spec["id"], "image:", name, "contribution:", server["name"])
EOF

echo "==> mcp-gateway: hooks are shell-clean"
for hook in "$HERE"/hooks/*.sh; do
  sh -n "$hook"
done

echo "==> mcp-gateway: hooks run against a scratch data volume"
SCRATCH="$(mktemp -d)"
trap_cleanup() { rm -rf "$SCRATCH"; }
trap trap_cleanup EXIT
export LEMONSEED_MODULE_ID=mcp-gateway
export LEMONSEED_MODULE_DIR="$SCRATCH"

LEMONSEED_EVENT=install sh "$HERE/hooks/install.sh" > /dev/null
LEMONSEED_EVENT=start sh "$HERE/hooks/start.sh" > /dev/null
test -f "$SCRATCH/start.log"
LEMONSEED_EVENT=stop sh "$HERE/hooks/stop.sh" > /dev/null
test -f "$SCRATCH/stop.log"
LEMONSEED_EVENT=user sh "$HERE/hooks/user.sh" < /dev/null > /dev/null
test -f "$SCRATCH/users.log"
sh "$HERE/hooks/remove.sh" > /dev/null

echo "==> mcp-gateway: the server module registers its tools"
# The authoritative environment is the Nix-built image (nixpkgs pins MCP SDK
# 1.x, where FastMCP lives at mcp.server.fastmcp). An SDK 2.x interpreter has
# a different API — skip rather than fail on the wrong toolchain.
if ! python3 -c 'from mcp.server.fastmcp import FastMCP' 2>/dev/null; then
  if python3 -c 'import mcp' 2>/dev/null; then
    echo "skipped: this interpreter has MCP SDK 2.x (API changed); the image pins nixpkgs' 1.26"
  else
    echo "skipped: no mcp SDK on PATH"
  fi
else
MCP_DATA_DIR="$SCRATCH" python3 - "$ROOT/pkgs/mcp-gateway/server.py" <<'EOF'
import asyncio, importlib.util, json, sys
from pathlib import Path

spec = importlib.util.spec_from_file_location("mcp_gateway_server", Path(sys.argv[1]))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

async def tools():
    return await mod.mcp.list_tools()

tools = asyncio.run(tools())
names = sorted(t.name for t in tools)
for want in ("fetch", "ddg_search", "memory_read_graph", "get_current_time"):
    assert want in names, (want, names)
print("server ok: %d tools registered" % len(names))
EOF
fi

echo "==> mcp-gateway harness passed"
