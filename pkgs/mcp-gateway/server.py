# Lemonseed MCP gateway server — one process, four toolsets (M9 P3).
#
# The module contract is a single container per module (MODULES.md §5), so the
# "gateway" here is not Docker's supervisor of the same name (that one spawns
# each server as its own sibling container and needs docker.sock — verified
# empirically 2026-11; see BACKLOG for that topology). This is a FastMCP
# HTTP app bundling the four services the plan named, all implementable
# in-process:
#
#   fetch       — GET a URL, return readable text (HTML stripped)
#   ddg_search  — DuckDuckGo "lite" endpoint, parsed results
#   memory      — file-backed knowledge graph (JSONL under /data)
#   time        — current time in a named IANA zone
#
# The transport is MCP streamable-HTTP on :7860 at /mcp — the exact shape
# LiteLLM's mcp_servers entry ({name, url, transport: http}) consumes.
import json
import os
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

DATA_DIR = Path(os.environ.get("MCP_DATA_DIR", "/data"))
MEMORY_PATH = DATA_DIR / "memory.jsonl"

PORT = int(os.environ.get("MCP_PORT", "7860"))

mcp = FastMCP(
    name="lemonseed-mcp-gateway",
    # uvicorn binds 0.0.0.0 (main()), so tell the SDK: a localhost host here
    # silently turns on DNS-rebinding protection with a localhost-only Host
    # allowlist, which 421s every container-to-container caller (LiteLLM's
    # Host header is the lemon-network DNS name). Keep the protection ON but
    # allow exactly the hosts that may call this endpoint.
    host="0.0.0.0",
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[
            "localhost:*",
            "127.0.0.1:*",
            "lemonseed-mcp-gateway:*",  # the litellm container's route in
        ],
    ),
    instructions=(
        "Lemonseed bundled tools: fetch, ddg_search, memory (knowledge "
        "graph), time."
    ),
)

# ---------------------------------------------------------------- fetch -----


class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "head", "title"}

    def __init__(self) -> None:
        super().__init__()
        self._skip_depth = 0
        self.chunks: list[str] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        if tag in self.SKIP:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0 and (text := data.strip()):
            self.chunks.append(text)


@mcp.tool()
def fetch(url: str, max_chars: int = 8000) -> str:
    """Fetch a URL over HTTP(S) and return the readable text content."""
    import httpx

    with httpx.Client(timeout=20, follow_redirects=True) as client:
        response = client.get(url)
        response.raise_for_status()
    body = response.text
    extractor = _TextExtractor()
    try:
        extractor.feed(body)
    except Exception:  # noqa: BLE001 — non-HTML bodies are returned verbatim
        return body[:max_chars]
    text = "\n".join(extractor.chunks) if extractor.chunks else body
    return text[:max_chars]


# ----------------------------------------------------------- ddg_search -----


@mcp.tool()
def ddg_search(query: str, max_results: int = 5) -> list[dict[str, str]]:
    """Search DuckDuckGo (lite endpoint); returns [{title, url, snippet}]."""
    import httpx

    with httpx.Client(timeout=20) as client:
        response = client.post(
            "https://lite.duckduckgo.com/lite/",
            data={"q": query},
            headers={"User-Agent": "Mozilla/5.0 (lemonseed-mcp-gateway)"},
        )
        response.raise_for_status()
    from html.parser import HTMLParser

    results: list[dict[str, str]] = []

    class Lite(HTMLParser):
        mode = None
        href = ""

        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            a = dict(attrs or {})  # type: ignore[arg-type]
            if tag == "a" and a.get("class") == "result-link":
                self.mode, self.href = "title", a.get("href") or ""
            elif tag == "td" and a.get("class") == "result-snippet":
                self.mode = "snippet"

        def handle_data(self, data: str) -> None:
            if not self.mode or not data.strip():
                return
            if self.mode == "title":
                results.append({"title": data.strip(), "url": self.href, "snippet": ""})
                self.mode = "done"
            elif self.mode == "snippet" and results:
                results[-1]["snippet"] += data.strip()

    Lite().feed(response.text)
    return results[:max_results]


# -------------------------------------------------------------- memory ------
# File-backed knowledge graph: entities, relations and observations in one
# JSONL file under the module's data volume (persists across restarts).


def _memory_read() -> list[dict]:
    if not MEMORY_PATH.exists():
        return []
    rows = []
    for line in MEMORY_PATH.read_text().splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def _memory_write(rows: list[dict]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = MEMORY_PATH.with_suffix(".tmp")
    tmp.write_text("".join(json.dumps(r) + "\n" for r in rows))
    tmp.replace(MEMORY_PATH)


@mcp.tool()
def memory_create_entities(entities: list[dict[str, str]]) -> str:
    """Create entities ({"name": ..., "entityType": ...}); skips duplicates."""
    rows = _memory_read()
    known = {r["name"] for r in rows if r.get("type") == "entity"}
    added = 0
    for entity in entities:
        name = entity.get("name", "")
        if name and name not in known:
            rows.append({"type": "entity", **entity})
            known.add(name)
            added += 1
    _memory_write(rows)
    return f"created {added} entities"


@mcp.tool()
def memory_add_observations(entity_name: str, observations: list[str]) -> str:
    """Append observations to an existing entity; creates it if missing."""
    rows = _memory_read()
    found = False
    for row in rows:
        if row.get("type") == "entity" and row["name"] == entity_name:
            found = True
            obs = set(row.setdefault("observations", []))
            obs.update(observations)
            row["observations"] = sorted(obs)
    if not found:
        rows.append(
            {
                "type": "entity",
                "name": entity_name,
                "entityType": "unknown",
                "observations": list(observations),
            }
        )
    _memory_write(rows)
    return f"{len(observations)} observations on {entity_name}"


@mcp.tool()
def memory_create_relations(relations: list[dict[str, str]]) -> str:
    """Create relations ({"from": A, "to": B, "relationType": ...})."""
    rows = _memory_read()
    known = {
        (r["from"], r["to"], r.get("relationType", ""))
        for r in rows
        if r.get("type") == "relation"
    }
    added = 0
    for relation in relations:
        key = (relation.get("from", ""), relation.get("to", ""), relation.get("relationType", ""))
        if all(key) and key not in known:
            rows.append({"type": "relation", **relation})
            known.add(key)
            added += 1
    _memory_write(rows)
    return f"created {added} relations"


@mcp.tool()
def memory_read_graph() -> dict:
    """Return the whole knowledge graph (entities and relations)."""
    return {"rows": _memory_read()}


# ---------------------------------------------------------------- time ------


@mcp.tool()
def get_current_time(zone: str = "UTC") -> dict:
    """Current date/time in an IANA zone (default UTC), ISO-8601."""
    from zoneinfo import ZoneInfo

    now = datetime.now(ZoneInfo(zone))
    return {
        "timezone": zone,
        "datetime": now.isoformat(),
        "utc_offset": str(now.utcoffset()),
        "is_dst": bool(now.dst()),
    }


# --------------------------------------------------------------- main -------


def main() -> None:  # pragma: no cover — process entrypoint
    import uvicorn
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route

    def healthz(request):  # type: ignore[no-untyped-def]
        return PlainTextResponse("ok")

    # FastMCP's app owns the streamable-HTTP session manager's lifespan, so it
    # must be the thing that runs — a bare wrapper would skip its startup. The
    # control plane's probe accepts only 2xx/3xx on GET; streamable HTTP
    # answers a bare GET with 4xx, so /healthz rides in the same app.
    app = mcp.streamable_http_app()
    app.router.routes.append(Route("/healthz", healthz))
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level=os.environ.get("MCP_LOG_LEVEL", "info"))


if __name__ == "__main__":
    main()
