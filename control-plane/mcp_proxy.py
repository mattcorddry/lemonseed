"""Admin proxy for LiteLLM's MCP surface (M9 P3 follow-up).

LiteLLM serves the appliance's registered MCP tools two ways: the standard
protocol at /mcp (what Open WebUI's tool-server feature speaks) and a REST
pair under /mcp-rest. Both are bound to 127.0.0.1 with the master key, so
nothing outside the appliance can reach them — this module gives the admin
portal (and the smoke) an authenticated, session-gated window onto that
surface without ever exposing the master key.
"""
import json
import urllib.error
import urllib.request

from litellm import LITELLM_PORT, master_key

_TIMEOUT_SEC = 20


def _request(path: str, payload: dict[str, object] | None = None) -> tuple[int, dict]:
    """One authenticated call to LiteLLM's REST MCP surface."""
    url = f"http://127.0.0.1:{LITELLM_PORT}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {master_key()}",
            "Content-Type": "application/json",
        },
        method="POST" if payload is not None else "GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_SEC) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:300]
        return exc.code, {"error": detail}
    except (urllib.error.URLError, OSError) as exc:
        return 502, {"error": f"litellm unreachable: {str(exc)[:200]}"}


def tools_list() -> tuple[int, dict]:
    """Every tool the rendered config's MCP servers expose, with the server
    ids a follow-up call needs."""
    status, body = _request("/mcp-rest/tools/list")
    return status, {"tools": body.get("tools", []) if isinstance(body, dict) else body}


def tool_call(server_name: str, tool: str, arguments: dict[str, object]) -> tuple[int, dict]:
    """Execute one MCP tool through LiteLLM.

    ``server_name`` is the module's contribution name (e.g. mcp_gateway); the
    REST call needs LiteLLM's opaque server_id, resolved from the listing so
    the caller never sees one.
    """
    status, listing = tools_list()
    if status != 200:
        return status, {"error": f"tool listing failed: {listing}"}
    # The REST surface lists UNPREFIXED names (the /v1/mcp/tools view is the
    # one that prefixes with the server name); match on both.
    full_name = f"{server_name}-{tool}"
    server_id = None
    rest_name = None
    for entry in listing.get("tools", []):
        info = entry.get("mcp_info") or {}
        if info.get("server_name") != server_name:
            continue
        if entry.get("name") in (tool, full_name):
            rest_name = entry["name"]
            server_id = info.get("server_id")
            break
    if not server_id or not rest_name:
        return 404, {"error": f"no tool {full_name!r} is registered"}
    status, body = _request(
        "/mcp-rest/tools/call",
        {"name": rest_name, "server_id": server_id, "arguments": arguments},
    )
    if status != 200:
        return status, {"error": f"tool call failed: {body}"}
    content = body.get("content") or []
    text = "".join(item.get("text", "") for item in content)
    try:
        parsed: dict[str, object] | str = json.loads(text)
    except (ValueError, TypeError):
        parsed = text
    return 200, {"tool": full_name, "result": parsed}
