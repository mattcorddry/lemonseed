# The Lemonseed MCP gateway container image (M9 P3).
#
# An in-tree, Nix-built single-container bundle of the four MCP services the
# plan named — fetch, ddg_search, memory, time — as ONE FastMCP streamable-HTTP
# app on :7860/mcp. Upstream `docker/mcp-gateway` cannot be used here: it is a
# supervisor that spawns each server as its own sibling container and needs
# docker.sock, which breaks the module contract's one-container-per-module
# rule (MODULES.md §5) — verified empirically before this was written.
#
# The appliance has no registry of its own: production loads the tar once at
# boot (nixos/modules/mcp-gateway-image.nix) and the data plane satisfies the
# pin from the local tag; QEMU bakes it in via the same module. The pin's
# digest therefore documents the image config ID instead of a registry
# manifest digest — `docker load` tars carry RepoTags but no RepoDigests,
# exactly like the pullImage tarballs the QEMU profile already uses.
{
  lib,
  python3Packages,
  dockerTools,
}:

let
  # The official MCP SDK's FastMCP (mcp.server.fastmcp) + uvicorn for the
  # streamable-HTTP transport; httpx backs the fetch and ddg_search tools.
  # nixpkgs pins mcp 1.26; server.py uses the 1.x API — migrating to SDK 2.x
  # (renamed MCPServer) means touching server.py, not just this expression.
  pythonEnv = python3Packages.python.withPackages (
    ps: with ps; [
      mcp
      httpx
      uvicorn
    ]
  );

  serverSrc = lib.cleanSource ./.;
in

dockerTools.buildLayeredImage {
  name = "lemonseed/mcp-gateway";
  tag = "0.1.0";

  contents = [ pythonEnv ];

  config = {
    Env = [
      "MCP_DATA_DIR=/data"
      "PYTHONUNBUFFERED=1"
    ];
    Cmd = [
      "${pythonEnv}/bin/python"
      "${serverSrc}/server.py"
    ];
    ExposedPorts = {
      "7860/tcp" = { };
    };
    Volumes = {
      "/data" = { };
    };
  };
}
