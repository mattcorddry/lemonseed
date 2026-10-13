"""Manifest-driven Open WebUI reconciliation engine (M6.3b, Phase 3).

The appliance's intent for Open WebUI lives in ``owui-policy.json`` (shipped
beside this module), not in Python: adding a rule is a JSON diff, and the
portal renders per-key ownership straight from the manifest. This module is
the engine — a strict loader/validator, resolvers, a *pure* planner (the
entire mode-semantics surface), entity handlers, and the CP-side state store
+ last-known-good (LKG) snapshot.

Layering (proposals/M6/OWUI-DURABLE-CONFIG-M63B.md /
proposals/M6/OWUI-POLICY-ENGINE.md §5):

    load_manifest()  strict; a bad file means keep the last-good manifest and
                     write nothing.
    resolve_setting_value()   const / {"secret": K} / {"derived": name}
    plan()           pure function: (manifest, resolved, observed, state,
                     triggers) -> ordered actions + report entries
    EntityHandler    observe/desired/changes/reserved/apply per kind
    reconcile()      the never-raising entry point the supervisor calls
                     (client + DB I/O live only in this layer and handlers)

Modes: ``initial_setup`` (backfill only while the row is missing; a present
row is never touched again), ``managed`` (we re-assert on cadence), plus the
collection modes ``selective_management`` (own our ids, sweep reserved-name
collisions, never touch foreign entries) and ``full_management`` (mirror the
CP list — but the loop never deletes accounts; DESIGN rule).

State and the LKG config map live in the CP database (``cp_meta`` +
``owui_config_lkg``), deliberately *outside* the OWUI database so they
survive a broken OWUI install (decision D1/D2).
"""

from __future__ import annotations

import copy
import datetime
import hashlib
import json
import re
import secrets as _secrets
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Protocol

import db

POLICY_FILE_NAME = "owui-policy.json"
STATE_META_KEY = "owui_policy_state"

SETTINGS_MODES = frozenset({"managed", "initial_setup"})
COLLECTION_MODES = frozenset({"selective_management", "full_management"})
CADENCES = frozenset({"at_boot", "on_upgrade", "manual"})
GUARD_MODES = frozenset({"advertise", "enforce"})
HTTP_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})
USER_SCOPES = frozenset({"membership", "is_admin"})
# %NAME% tokens the validator accepts. Both names resolve to the CP service
# account's OWUI id (DESIGN uses %CP_OWUI_ID% in guards, the plan doc uses
# %SERVICE_ACCOUNT_ID% in entities; they are the same value on purpose).
PLACEHOLDER_TOKENS = frozenset({"CP_OWUI_ID", "SERVICE_ACCOUNT_ID"})

_DOTTED_KEY_RE = re.compile(r"^[a-z0-9_.]+$")
_API_NAME_RE = re.compile(r"^[a-z0-9_.]+$")
_PLACEHOLDER_RE = re.compile(r"%([A-Za-z_]+)%")
# Hint copy ends up inside a single-quoted nginx string that is itself a JSON
# body, so the allowed charset is chosen to make quoting impossible rather than
# to be pleasant for writers. ``reason`` stays free-form portal prose.
_GUARD_HINT_MAX = 160
_GUARD_HINT_RE = re.compile(r"^[A-Za-z0-9 .,;:()/%+-]{1,160}$")
# Resolved placeholder values (an OWUI id) substituted into an nginx regex.
_GUARD_LITERAL_RE = re.compile(r"^[A-Za-z0-9._~-]{1,128}$")
_VALUE_SPEC_KEYS = frozenset({"secret", "derived", "wrap"})
_ENTRY_KEYS = frozenset({"value", "mode", "cadence", "optional", "reason"})
_GUARD_KEYS = frozenset({"path", "mode", "reason", "hint"})
_TOP_KEYS = frozenset({"version", "owui", "settings", "entities", "guards", "api"})

_MISSING = object()


class PolicyError(Exception):
    """Runtime policy failure (resolution, apply). Never fatal to the module."""


class ManifestError(PolicyError):
    """The manifest on disk is invalid; the engine must refuse to write."""


# --------------------------------------------------------------------------
# Manifest data model
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ValueSpec:
    """One entry's value: const literal, module secret, or derived resolver."""

    kind: str  # "const" | "secret" | "derived"
    value: Any = None
    name: str = ""
    wrap_list: bool = False


@dataclass(frozen=True)
class SettingEntry:
    key: str
    spec: ValueSpec
    mode: str
    cadence: Optional[str]
    optional: bool
    reason: str


@dataclass(frozen=True)
class EntitySpec:
    kind: str
    mode: str
    config: dict[str, Any]
    reason: str


@dataclass(frozen=True)
class GuardSpec:
    method: str
    path: str  # raw, may contain %PLACEHOLDER% tokens
    regex: re.Pattern
    mode: str
    reason: str
    # Short UI copy for the 403 body. Deliberately separate from ``reason``:
    # the portal reason is prose (apostrophes, em dashes) while the hint is
    # interpolated into an nginx single-quoted string, so it carries a strict
    # charset instead of "we promise to be careful".
    hint: str = ""


@dataclass(frozen=True)
class Manifest:
    version: int
    min_version: str
    settings: dict[str, SettingEntry]
    entities: dict[str, EntitySpec]
    guards: tuple[GuardSpec, ...]
    api: dict[str, str]  # logical name -> "METHOD /path"
    policy_hash: str


@dataclass
class ResolverContext:
    """Everything resolvers/handlers need, injected by the caller (modules.py)
    so the engine never imports the module state machine (no cycles)."""

    api_map: dict[str, str] = field(default_factory=dict)
    secrets: dict[str, str] = field(default_factory=dict)
    lemond_base: str = ""
    placeholders: dict[str, str] = field(default_factory=dict)
    # CP identity truth: [{email, name, is_admin, disabled}], built by wiring.
    cp_users: list[dict[str, Any]] = field(default_factory=list)


# --------------------------------------------------------------------------
# Loader + strict validator (§3 / §5.1)
# --------------------------------------------------------------------------


def default_policy_path() -> Path:
    return Path(__file__).with_name(POLICY_FILE_NAME)


def parse_route(raw: str) -> tuple[str, str]:
    """Split ``"METHOD /path"``; raises ValueError on malformed input."""
    if not isinstance(raw, str) or " " not in raw:
        raise ValueError(f"expected 'METHOD /path', got {raw!r}")
    method, _, rest = raw.partition(" ")
    method = method.upper()
    path = rest.strip()
    if method not in HTTP_METHODS or not path.startswith("/"):
        raise ValueError(f"expected 'METHOD /path', got {raw!r}")
    return method, path


def _sub_placeholders_for_regex(text: str) -> str:
    def repl(match: re.Match) -> str:
        token = match.group(1)
        if token not in PLACEHOLDER_TOKENS:
            raise ValueError("unknown placeholder %%%s%%" % token)
        return "[^/]+"

    return _PLACEHOLDER_RE.sub(repl, text)


def _known_placeholders(text: str) -> bool:
    return all(t in PLACEHOLDER_TOKENS for t in _PLACEHOLDER_RE.findall(text))


def _parse_value_spec(raw: Any) -> ValueSpec:
    """const | {"secret": K} | {"derived": N}, optional "wrap": "list"."""
    if isinstance(raw, dict) and (_VALUE_SPEC_KEYS & raw.keys()):
        keys = set(raw)
        if keys - _VALUE_SPEC_KEYS or ("secret" in raw) == ("derived" in raw):
            raise ValueError(
                "value spec must be exactly one of secret/derived"
                f" (plus optional wrap:'list'), got keys {sorted(keys)}"
            )
        wrap = raw.get("wrap")
        if wrap is not None and wrap != "list":
            raise ValueError(f"unsupported wrap {wrap!r}, only 'list'")
        if "secret" in raw:
            name = raw["secret"]
            if not isinstance(name, str) or not name:
                raise ValueError("secret name must be a non-empty string")
            return ValueSpec("secret", name=name, wrap_list=wrap == "list")
        name = raw["derived"]
        if not isinstance(name, str) or not name:
            raise ValueError("derived name must be a non-empty string")
        return ValueSpec("derived", name=name, wrap_list=wrap == "list")
    return ValueSpec("const", value=raw)


def _validate_settings(block: dict, errors: list[str]) -> dict[str, SettingEntry]:
    entries: dict[str, SettingEntry] = {}
    unknown = set(block) - {"entries"}
    if unknown:
        errors.append(f"settings: unknown keys {sorted(unknown)}")
    raw_entries = block.get("entries")
    if not isinstance(raw_entries, dict) or not raw_entries:
        errors.append("settings.entries must be a non-empty object")
        return entries
    for key, entry in raw_entries.items():
        if not isinstance(key, str) or not _DOTTED_KEY_RE.match(key):
            errors.append(f"settings.entries: bad dotted key {key!r}")
            continue
        if not isinstance(entry, dict):
            errors.append(f"{key}: entry must be an object")
            continue
        extra = set(entry) - _ENTRY_KEYS
        if extra:
            errors.append(f"{key}: unknown fields {sorted(extra)}")
        mode = entry.get("mode")
        if mode not in SETTINGS_MODES:
            errors.append(f"{key}: mode must be one of {sorted(SETTINGS_MODES)}")
            continue
        cadence = entry.get("cadence")
        if mode == "managed":
            if cadence not in CADENCES:
                errors.append(f"{key}: managed entries require cadence in {sorted(CADENCES)}")
                continue
        elif cadence is not None:
            errors.append(f"{key}: cadence is only valid for managed entries")
            continue
        optional = entry.get("optional", False)
        if not isinstance(optional, bool):
            errors.append(f"{key}: optional must be a boolean")
        reason = entry.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            errors.append(f"{key}: reason is required (portal tooltip)")
        try:
            spec = _parse_value_spec(entry.get("value"))
        except ValueError as exc:
            errors.append(f"{key}: {exc}")
            continue
        entries[key] = SettingEntry(
            key=key, spec=spec, mode=str(mode), cadence=cadence,
            optional=bool(optional), reason=str(reason or ""),
        )
    return entries


def _validate_entities(
    block: dict, errors: list[str]
) -> dict[str, EntitySpec]:
    entities: dict[str, EntitySpec] = {}
    for kind, cfg in block.items():
        handler = ENTITY_KINDS.get(kind)
        if handler is None:
            # Rule 4: never let an unregistered kind silently no-op.
            errors.append(f"entities: kind {kind!r} is not implemented")
            continue
        if not isinstance(cfg, dict):
            errors.append(f"entities.{kind}: must be an object")
            continue
        reason = cfg.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            errors.append(f"entities.{kind}: reason is required")
            reason = ""
        mode = cfg.get("mode")
        rest = {k: v for k, v in cfg.items() if k not in ("mode", "reason")}
        try:
            cleaned = handler.validate(str(mode), rest)
        except ValueError as exc:
            errors.append(f"entities.{kind}: {exc}")
            continue
        entities[kind] = EntitySpec(
            kind=kind, mode=str(mode), config=cleaned, reason=str(reason)
        )
    return entities


def _validate_guards(raw: Any, errors: list[str]) -> tuple[GuardSpec, ...]:
    guards: list[GuardSpec] = []
    if not isinstance(raw, list):
        errors.append("guards must be an array")
        return tuple()
    for item in raw:
        if not isinstance(item, dict):
            errors.append("guards[] entries must be objects")
            continue
        path = item.get("path")
        try:
            method, path_part = parse_route(str(path or ""))
        except ValueError as exc:
            errors.append(f"guards: {exc}")
            continue
        mode = item.get("mode")
        if mode not in GUARD_MODES:
            errors.append(f"guards {path!r}: mode must be advertise|enforce")
            continue
        reason = item.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            errors.append(f"guards {path!r}: reason is required")
            reason = ""
        extra = set(item) - _GUARD_KEYS
        if extra:
            errors.append(f"guards {path!r}: unknown keys {sorted(extra)}")
        hint = item.get("hint", "")
        if not isinstance(hint, str):
            errors.append(f"guards {path!r}: hint must be a string")
            hint = ""
        elif len(hint) > _GUARD_HINT_MAX:
            errors.append(f"guards {path!r}: hint longer than {_GUARD_HINT_MAX}")
        elif hint and not _GUARD_HINT_RE.match(hint):
            # Fail at load, not at render: the manifest is the place to fix copy.
            errors.append(
                f"guards {path!r}: hint must match {_GUARD_HINT_RE.pattern!r} "
                "(no quotes, backslashes or $ so it is nginx/JSON-body safe)"
            )
        try:
            regex = re.compile(_sub_placeholders_for_regex(path_part))
        except (ValueError, re.error) as exc:
            errors.append(f"guards {path!r}: path is not a valid regex: {exc}")
            continue
        guards.append(
            GuardSpec(method=method, path=path_part, regex=regex,
                      mode=str(mode), reason=str(reason), hint=hint.strip())
        )
    return tuple(guards)


def _validate_api(raw: Any, errors: list[str]) -> dict[str, str]:
    api: dict[str, str] = {}
    if not isinstance(raw, dict) or not raw:
        errors.append("api map must be a non-empty object")
        return api
    for name, route in raw.items():
        if not isinstance(name, str) or not _API_NAME_RE.match(name):
            errors.append(f"api: bad logical name {name!r}")
            continue
        try:
            parse_route(str(route))
        except ValueError as exc:
            errors.append(f"api[{name!r}]: {exc}")
            continue
        api[name] = str(route)
    return api


def load_manifest(path: Optional[Path] = None) -> Manifest:
    """Parse + strictly validate the manifest. Raises ManifestError.

    Unknown keys/modes/cadences/kinds are *rejected*, not ignored — strict
    validation is what makes this file safe to extend over time.
    """
    file_path = Path(path) if path is not None else default_policy_path()
    try:
        raw_bytes = file_path.read_bytes()
    except OSError as exc:
        raise ManifestError(f"cannot read manifest: {exc}") from exc
    try:
        doc = json.loads(raw_bytes.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ManifestError(f"manifest is not valid JSON: {exc}") from exc
    if not isinstance(doc, dict):
        raise ManifestError("manifest must be a JSON object")

    errors: list[str] = []
    unknown = set(doc) - _TOP_KEYS
    if unknown:
        errors.append(f"unknown top-level keys {sorted(unknown)}")

    version = doc.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        errors.append("version must be an integer >= 1")
        version = 0

    owui_block = doc.get("owui")
    min_version = ""
    if not isinstance(owui_block, dict) or not isinstance(owui_block.get("min_version"), str):
        errors.append("owui.min_version is required (string)")
    else:
        min_version = owui_block["min_version"]

    settings_block = doc.get("settings")
    settings = (
        _validate_settings(settings_block, errors)
        if isinstance(settings_block, dict)
        else (errors.append("settings must be an object") or {})
    )
    entities_block = doc.get("entities")
    entities = (
        _validate_entities(entities_block, errors)
        if isinstance(entities_block, dict)
        else (errors.append("entities must be an object") or {})
    )
    guards = _validate_guards(doc.get("guards", []), errors)
    api = _validate_api(doc.get("api", {}), errors)

    if errors:
        raise ManifestError("; ".join(errors))

    digest = hashlib.sha256(raw_bytes).hexdigest()
    return Manifest(
        version=int(version),
        min_version=min_version,
        settings=settings,
        entities=entities,
        guards=guards,
        api=api,
        policy_hash=f"sha256:{digest}",
    )


def version_at_least(installed: str, minimum: str) -> bool:
    """Semver-ish tuple compare ('0.11.0' >= '0.9.0'). False if unparseable."""

    def parts(text: str) -> Optional[tuple[int, ...]]:
        match = re.search(r"[0-9]+(?:\.[0-9]+)*", str(text or ""))
        if not match:
            return None
        return tuple(int(p) for p in match.group(0).split("."))

    a, b = parts(installed), parts(minimum)
    if a is None or b is None:
        return False
    width = max(len(a), len(b))
    return a + (0,) * (width - len(a)) >= b + (0,) * (width - len(b))


# --------------------------------------------------------------------------
# Guard rendering (Phase 4B) — manifest -> nginx include text
# --------------------------------------------------------------------------
#
# The CP owns these paths *intent-wise*; nginx is what makes that intent
# enforceable at the edge. Two properties are load-bearing and tested:
#
# * Only ``mode: "enforce"`` guards emit text, and a guard that stops being
#   enforced must vanish from the file entirely (no commented-out leftovers,
#   no stale rules) — the CP always renders the whole file from the manifest.
# * Nothing from the manifest reaches nginx unfiltered. ``reason`` is portal
#   prose and is never interpolated; ``hint`` carries a strict charset, and
#   placeholder values must look like an id. That makes "a manifest string
#   smuggles an nginx directive" a structural impossibility, not a review
#   finding.
#
# Backslashes are rejected rather than escaped: nginx strips backslashes
# inside quoted config strings, so `\.`/`\?` would silently change meaning.
# Manifest authors use bracket classes (`[.]`, `[?]`) instead.
#
# Why ``$request`` and not ``"$request_method$request_uri"``: nginx's ``if``
# requires its left-hand side to be a *single variable* — concatenating two
# variables in a condition is a config error ("invalid condition"), which is
# caught by ``nginx -t`` but not by any amount of reading. ``$request`` is the
# first line of the request ("POST /path?q HTTP/1.1"), so it carries method and
# path in one variable and both stay in the same pattern; anchoring on
# ``^METHOD:`` and ``HTTP/$``-equivalent is what bounds the match.

GUARD_DENIAL_MARKER = "managed-by-lemonseed"
GUARD_CONF_HEADER = "# managed by the Lemonseed control plane - do not edit\n"
DEFAULT_GUARD_HINT = "This setting is managed by Lemonseed."
# ``$request`` is "METHOD URI PROTOCOL" (space-separated, exactly as it arrived
# on the wire); the URI ends at the next space. (An absolute-form request
# target — "GET http://host/path HTTP/1.1" — would not match ``^METHOD /path``
# and so fails open; LAN clients do not send one.)
GUARD_REQUEST_TAIL = "([?][^ ]*)? HTTP/"


class GuardPending(PolicyError):
    """A guard that cannot render *yet* (its placeholder is not resolved).

    Separated from a real PolicyError on purpose: waiting for the service
    account id is a normal transient state, so it goes to ``pending``, while an
    unsafe hint or a backslash path is an authoring bug that must abort the
    render loudly instead of quietly dropping enforcement.
    """


@dataclass(frozen=True)
class GuardRender:
    """Rendered nginx text plus the truth the portal reports about it.

    ``enforced``/``pending`` are ``"METHOD /path"`` keys in sorted order (so the
    text is byte-stable for a given manifest); ``errors`` carries the matching
    human-readable reason for each pending key.
    """

    text: str
    enforced: tuple[str, ...]
    pending: tuple[str, ...]
    errors: tuple[str, ...]
    sha256: str


def _guard_denial_body(hint: str) -> str:
    """JSON body for the 403. Asserts the hint charset again at render time.

    ``detail`` is not decoration: Open WebUI's client reads the FastAPI
    convention (``error.response.data.detail``) and falls back to its own
    generic "Server connection failed" toast when it is absent, so a body
    without it denies the call invisibly. Bare-metal review of Phase 4B saw
    exactly that. ``hint`` stays for our own diagnostics and tests.
    """

    text = (hint or "").strip() or DEFAULT_GUARD_HINT
    if not _GUARD_HINT_RE.match(text):
        raise PolicyError(f"guard hint is not nginx-safe: {text!r}")
    return (
        '{"detail":"' + text + '","lemonseed":"' + GUARD_DENIAL_MARKER
        + '","hint":"' + text + '"}'
    )


def _resolve_guard_path(raw_path: str, placeholders: dict[str, str]) -> str:
    """Substitute ``%TOKEN%`` with the resolved literal id.

    The validator uses ``[^/]+`` because it only checks the pattern compiles;
    the renderer must use the real id, or a guard on the CP account would
    match *every* account.
    """

    def replace(match: "re.Match[str]") -> str:
        token = match.group(1)
        value = str((placeholders or {}).get(token, "") or "")
        if not _GUARD_LITERAL_RE.match(value):
            raise GuardPending(
                f"placeholder %{token}% is unresolved or not id-shaped: {value!r}"
            )
        return value

    body = _PLACEHOLDER_RE.sub(replace, raw_path)
    if "\\" in body:
        raise PolicyError(
            "guard path may not contain backslashes; use a bracket class"
            " such as [.] or [?] instead"
        )
    if "$" in body:
        # The renderer supplies the end anchor (the request line's " HTTP/"), so
        # a path that ends its own pattern would silently stop matching.
        raise PolicyError(
            "guard path may not contain $ ; the renderer anchors the match"
        )
    return body


def render_guards(
    manifest: Manifest, placeholders: Optional[dict[str, str]] = None
) -> GuardRender:
    """Pure manifest -> nginx text. Raises PolicyError on unsafe input.

    Guards whose placeholders are not resolved yet (service-account id not
    minted) land in ``pending`` and are simply absent from the text, so the file
    is always a complete statement of what is enforced *right now*. Anything
    else that can go wrong raises, because the caller's response to a raise —
    keep the previous file, log, report it — is the correct one for a manifest
    bug, and silently dropping a rule is not.
    """

    enforced: list[str] = []
    pending: list[str] = []
    errors: list[str] = []
    rules: list[str] = []

    for guard in sorted(manifest.guards, key=lambda g: (g.method, g.path)):
        key = guard.method + " " + guard.path
        if guard.mode not in GUARD_MODES:
            raise PolicyError(f"guard {key!r}: unknown mode {guard.mode!r}")
        if guard.mode != "enforce":
            continue
        try:
            path = _resolve_guard_path(guard.path, placeholders or {})
        except GuardPending as exc:
            pending.append(key)
            errors.append(f"{key}: {exc}")
            continue
        body = _guard_denial_body(guard.hint)
        pattern = (
            "^" + guard.method + " " + path + GUARD_REQUEST_TAIL
        )
        # No ``default_type`` here: nginx rejects it inside ``if``. The
        # enclosing location sets it (see gateway.nix, locations "/") so this
        # body arrives as application/json and Open WebUI can parse it; the
        # marker is what our tests and any client integration key on.
        rules.append(
            'if ($request ~ "' + pattern + '") {\n'
            + "    return 403 '" + body + "';\n"
            + "}\n"
        )
        enforced.append(key)

    text = "" if not rules else GUARD_CONF_HEADER + "\n".join(rules)
    return GuardRender(
        text=text,
        enforced=tuple(enforced),
        pending=tuple(pending),
        errors=tuple(errors),
        sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )


# --------------------------------------------------------------------------
# Resolvers (§5.2)
# --------------------------------------------------------------------------


def derive_allowed_endpoints(api_map: dict[str, str]) -> str:
    """Derive ``auth.api_key.allowed_endpoints`` from the manifest api map.

    OWUI (v0.11.0) matches the comma-separated list by exact path or
    ``<entry>/`` prefix. Route templates cut at their first ``{param}``
    segment so parameterized routes become their static prefix.
    """
    prefixes: set[str] = set()
    for route in api_map.values():
        try:
            _method, path = parse_route(route)
        except ValueError:
            continue
        cut = path.split("{", 1)[0].rstrip("/")
        if cut:
            prefixes.add(cut)
    return ",".join(sorted(prefixes))


def _resolve_lemond_base(ctx: ResolverContext) -> str:
    if not ctx.lemond_base:
        raise PolicyError("lemond base url is not available yet")
    return ctx.lemond_base


def _resolve_lemond_api_configs(ctx: ResolverContext) -> dict[str, Any]:
    if not ctx.lemond_base:
        raise PolicyError("lemond base url is not available yet")
    # v0.11.0 api_configs: url -> {"valves": {...}}; day one has no valves.
    return {ctx.lemond_base: {"valves": {}}}


RESOLVERS: dict[str, Callable[[ResolverContext], Any]] = {
    "lemond_base": _resolve_lemond_base,
    "lemond_api_configs": _resolve_lemond_api_configs,
    "api_paths_allow": lambda ctx: derive_allowed_endpoints(ctx.api_map),
}


def resolve_setting_value(
    entry: SettingEntry, ctx: ResolverContext
) -> tuple[Any, Optional[str]]:
    """(value, error). Missing secrets/derived sources = skip-with-drift,
    never a partial write (a None value is never produced silently)."""
    spec = entry.spec
    if spec.kind == "const":
        return copy.deepcopy(spec.value), None
    if spec.kind == "secret":
        value = ctx.secrets.get(spec.name)
        if value is None or value == "":
            return None, f"secret {spec.name} is not available yet"
    else:
        resolver = RESOLVERS.get(spec.name)
        if resolver is None:
            return None, f"unknown derived resolver {spec.name!r}"
        try:
            value = resolver(ctx)
        except PolicyError as exc:
            return None, str(exc)
    if spec.wrap_list:
        value = [value]
    return value, None


# --------------------------------------------------------------------------
# Entity handlers (§5.4) — register a kind with one class + one line
# --------------------------------------------------------------------------


class EntityHandler(Protocol):
    """Per-kind lifecycle. Only ``observe``/``apply`` touch the client."""

    kind: str

    def validate(self, mode: str, config: dict[str, Any]) -> dict[str, Any]:
        """Return the cleaned config or raise ValueError (feeds the loader)."""
        ...

    def observe(
        self, client: Any, spec: EntitySpec, ctx: ResolverContext
    ) -> dict[str, dict]:
        """Ground truth keyed by the kind's ownership key."""
        ...

    def desired(self, spec: EntitySpec, ctx: ResolverContext) -> dict[str, dict]:
        """Intent derived from CP truth, same key space as observe()."""
        ...

    def changes(self, spec: EntitySpec, desired: dict, observed: dict) -> Optional[dict]:
        """Update payload for drifted fields, or None when in sync."""
        ...

    def reserved(self, spec: EntitySpec, observed: dict) -> bool:
        """True when a *foreign* entity collides with reserved naming."""
        ...

    def apply(self, client: Any, action: "Action") -> dict[str, Any]:
        """Execute one create/update/sweep; returns the client result."""
        ...


def _require_keys(kind: str, config: dict, required: dict[str, type], allowed: set[str]) -> None:
    extra = set(config) - allowed
    if extra:
        raise ValueError(f"{kind}: unknown fields {sorted(extra)}")
    for field_name, ftype in required.items():
        if field_name not in config:
            raise ValueError(f"{kind}: {field_name} is required")
        if not isinstance(config[field_name], ftype):
            raise ValueError(f"{kind}: {field_name} must be {ftype.__name__}")


def _response_items(data: Any, wrapper_key: str) -> Optional[list]:
    """Items out of a list-style endpoint body, or None if neither shape fits.

    v0.11.0 quirk: ``users/all`` wraps as ``{"users": [...], "total": N}``
    while ``models/export`` returns a bare list. Accept both so a shape
    migration in either direction cannot masquerade as an outage.
    """
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        items = data.get(wrapper_key)
        if isinstance(items, list):
            return items
    return None


class ModelsHandler:
    """OWUI admin models, owned by CP-chosen id (never server UUIDs)."""

    kind = "models"

    def validate(self, mode: str, config: dict[str, Any]) -> dict[str, Any]:
        _require_keys(
            "models",
            config,
            {"own_by": str, "own_ids": list, "reserved_name_regex": str},
            allowed={"own_by", "own_ids", "reserved_name_regex", "definitions"},
        )
        if mode not in COLLECTION_MODES:
            raise ValueError(f"models: mode must be one of {sorted(COLLECTION_MODES)}")
        if config["own_by"] != "id":
            raise ValueError("models: only own_by 'id' is supported (ids are CP-authored)")
        own_ids = config["own_ids"]
        if not all(isinstance(i, str) and i for i in own_ids):
            raise ValueError("models: own_ids must be a list of non-empty strings")
        try:
            compiled = re.compile(config["reserved_name_regex"], re.IGNORECASE)
        except re.error as exc:
            raise ValueError(f"models: reserved_name_regex does not compile: {exc}") from exc
        definitions = config.get("definitions", {})
        if not isinstance(definitions, dict) or not all(
            k in own_ids and isinstance(v, dict) for k, v in definitions.items()
        ):
            raise ValueError("models: definitions must map own_ids to definition objects")
        cleaned = dict(config)
        cleaned["_name_re"] = compiled
        cleaned.setdefault("definitions", {})
        return cleaned

    def observe(
        self, client: Any, spec: EntitySpec, ctx: ResolverContext
    ) -> dict[str, dict]:
        result = client.call("models.export")
        if not result.get("ok"):
            raise PolicyError(f"models.export failed: {result.get('error')}")
        items = _response_items(result.get("data"), "models")
        if items is None:
            raise PolicyError("models.export: unexpected response shape")
        out: dict[str, dict] = {}
        for item in items:
            if isinstance(item, dict) and item.get("id"):
                out[str(item["id"])] = item
        return out

    def desired(self, spec: EntitySpec, ctx: ResolverContext) -> dict[str, dict]:
        # Day one: definitions ship empty (Phase 5/LiteLLM fills them); the
        # kind still arms ownership + the reserved-name sweep.
        return {
            str(model_id): copy.deepcopy(definition)
            for model_id, definition in spec.config.get("definitions", {}).items()
            if isinstance(definition, dict)
        }

    def changes(self, spec: EntitySpec, desired: dict, observed: dict) -> Optional[dict]:
        payload: dict[str, Any] = {}
        for key, want in desired.items():
            if key in ("id",):
                continue
            if observed.get(key) != want:
                payload[key] = copy.deepcopy(want)
        return payload or None

    def reserved(self, spec: EntitySpec, observed: dict) -> bool:
        # Own ids are never "reserved-name collisions": an owned id carrying a
        # reserved name is *ours* (create/fix path), not a sweep target.
        if str(observed.get("id") or "") in spec.config.get("own_ids", []):
            return False
        name = str(observed.get("name") or "")
        return bool(spec.config["_name_re"].match(name))

    def apply(self, client: Any, action: "Action") -> dict[str, Any]:
        if action.op == "create":
            return client.call("models.create", json_body=action.after)
        if action.op == "update":
            return client.call(
                "models.update", json_body={"id": action.entity_id, **(action.payload or {})}
            )
        if action.op in ("sweep", "delete"):
            return client.call("models.delete", json_body={"id": action.entity_id})
        raise PolicyError(f"models: unsupported op {action.op!r}")


class UsersHandler:
    """OWUI accounts mirroring the CP user list (membership + is_admin).

    Keyed by email (lower-cased) so adoption-by-email is free. The service
    account is filtered out of ``observe`` (never_touch): the planner never
    sees it, and its admin role is re-asserted by sign-in (Phase 1). The
    loop NEVER deletes accounts (DESIGN rule) — deletion lives in the
    Phase-4 portal member handler.
    """

    kind = "users"

    def validate(self, mode: str, config: dict[str, Any]) -> dict[str, Any]:
        _require_keys(
            "users",
            config,
            {"source": str, "scope": list},
            allowed={"source", "scope", "never_touch"},
        )
        if mode != "full_management":
            raise ValueError("users: mode must be full_management")
        if config["source"] != "cp.users":
            raise ValueError(f"users: unsupported source {config['source']!r}")
        scope = config["scope"]
        if not scope or not set(scope) <= USER_SCOPES:
            raise ValueError(f"users: scope entries must come from {sorted(USER_SCOPES)}")
        never_touch = config.get("never_touch", [])
        if not isinstance(never_touch, list) or not all(
            isinstance(t, str) and _known_placeholders(t) and _PLACEHOLDER_RE.search(t)
            for t in never_touch
        ):
            raise ValueError("users: never_touch entries must be %PLACEHOLDER% tokens")
        return dict(config)

    def _protected_ids(self, spec: EntitySpec, ctx: ResolverContext) -> set[str]:
        ids: set[str] = set()
        for token in spec.config.get("never_touch", []):
            for name in _PLACEHOLDER_RE.findall(token):
                value = ctx.placeholders.get(name)
                if value:
                    ids.add(str(value))
        return ids

    def observe(
        self, client: Any, spec: EntitySpec, ctx: ResolverContext
    ) -> dict[str, dict]:
        result = client.call("users.all")
        if not result.get("ok"):
            raise PolicyError(f"users.all failed: {result.get('error')}")
        # v0.11.0 ships UserInfoListResponse: {"users": [...], "total": N}.
        items = _response_items(result.get("data"), "users")
        if items is None:
            raise PolicyError("users.all: unexpected response shape")
        protected = self._protected_ids(spec, ctx)
        out: dict[str, dict] = {}
        for item in items:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            if str(item["id"]) in protected:
                continue
            email = str(item.get("email") or "").lower()
            if email:
                out[email] = item
        return out

    def desired(self, spec: EntitySpec, ctx: ResolverContext) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for user in ctx.cp_users:
            if user.get("disabled"):
                # v0.11.0 has no disable concept in OWUI; disabled members are
                # simply absent from intent (their OWUI account surfaces as
                # drift, deletion stays a portal action).
                continue
            email = str(user.get("email") or "").lower()
            if not email:
                continue
            out[email] = {
                "email": email,
                "name": str(user.get("name") or email.split("@", 1)[0]),
                "is_admin": bool(user.get("is_admin")),
            }
        return out

    def changes(self, spec: EntitySpec, desired: dict, observed: dict) -> Optional[dict]:
        if "is_admin" not in spec.config.get("scope", []):
            return None
        want = "admin" if desired.get("is_admin") else "user"
        # Pending/paused accounts heal at their next sign-in (trusted headers).
        if observed.get("role") in ("pending", "paused"):
            return None
        if observed.get("role") != want:
            return {"role": want}
        return None

    def reserved(self, spec: EntitySpec, observed: dict) -> bool:
        return False

    def apply(self, client: Any, action: "Action") -> dict[str, Any]:
        if action.op == "create":
            body = action.after or {}
            return client.call(
                "users.create",
                json_body={
                    "email": body.get("email"),
                    "name": body.get("name"),
                    "role": "admin" if body.get("is_admin") else "user",
                    # No OWUI password path exists (trusted-header auth); this
                    # value can never match anything and is never handed out.
                    "password": _secrets.token_urlsafe(16),
                },
            )
        if action.op == "update":
            observed = action.before or {}
            user_id = str(observed.get("id") or "")
            if not user_id:
                raise PolicyError("users.update: observed entity has no id")
            return client.call(
                "users.update", path_params={"user_id": user_id}, json_body=action.payload
            )
        raise PolicyError(f"users: unsupported op {action.op!r} (the loop never deletes)")


ENTITY_KINDS: dict[str, Any] = {
    ModelsHandler.kind: ModelsHandler(),
    UsersHandler.kind: UsersHandler(),
}


# --------------------------------------------------------------------------
# Secret masking (drift report + portal view)
# --------------------------------------------------------------------------

MASK = "***"

# Exact last-segment match on purpose: ``openai.api_keys`` is a credential list,
# while ``auth.enable_api_keys`` (a boolean flag) and
# ``auth.api_key.allowed_endpoints`` (a path allow-list) are not. The list covers
# leaves Open WebUI 0.11.0 actually stores credentials under (jupyter/terminal
# auth_token, OAuth client_secret, tool-server bearer_token, S3 keys).
_SECRET_LEAVES = frozenset({
    "api_key", "api_keys", "apikey", "password", "secret", "token",
    "access_token", "refresh_token", "auth_token", "bearer_token", "session_token",
    "client_secret", "private_key", "access_key", "access_key_id",
    "secret_access_key", "credential", "credentials",
})

# Entity kinds whose drift rows label a person: never echo the label into a
# member-visible payload (models collide by name, which *is* useful to show).
_PERSON_ENTITY_KINDS = frozenset({"users"})


def is_secret_key(key: str, spec: Any = None) -> bool:
    """True when a key's value must never be shown in a portal payload.

    The manifest's ``secret`` value kind is authoritative when the caller has
    the spec; the leaf-name heuristic is the backstop for keys the raw export
    carries that we do not manage (OWUI's ``Config.get_all()`` redacts nothing),
    so an admin's own API key cannot leak through a member-visible response.
    """
    if getattr(spec, "kind", None) == "secret":
        return True
    last = str(key or "").rsplit(".", 1)[-1].lower()
    return last in _SECRET_LEAVES


def _mask_nested(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: (MASK if is_secret_key(str(k)) else _mask_nested(v))
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_mask_nested(v) for v in value]
    return value


def mask_setting(key: str, value: Any, spec: Any = None) -> Any:
    """Display-safe form of one config value.

    Booleans/numbers are flags, never credentials, so they pass through (which
    is why the test is on the leaf *name*, not a substring: ``enable_api_keys``
    is a switch, ``api_keys`` is a secret). Structured values keep their shape
    with secret leaves masked, so ``openai.api_configs`` still reads as "one
    connection to <url>" with its ``api_key`` hidden.
    """
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if is_secret_key(key, spec):
        return MASK
    return _mask_nested(value)


def masked_report_entry(entry: "ReportEntry", manifest: Any = None) -> dict[str, Any]:
    """Serialize a drift entry for a *display* payload (secrets masked).

    Applied at serialization rather than in ``plan()`` on purpose: the planner
    and its tests keep seeing real values, and the payloads we *write* come from
    ``Action`` objects, never from these dicts -- so masking can never corrupt a
    reconcile. Everything that can reach an HTTP response (the cp_meta snapshot
    served by /api/modules, and the portal view built from it) uses this.
    """
    data = entry.as_dict()
    kind, sep, _rest = entry.key.partition(":")
    if sep and kind in _PERSON_ENTITY_KINDS:
        # Entity drift mirrors a person's attributes (e-mail is the users join
        # key) and the drift snapshot is member-visible: report the state, not
        # the mirrored values. The key keeps the id operators need to find the
        # row; member-visible ids are an accepted surface of the appliance.
        data["found"] = MASK
        data["want"] = MASK
        return data
    setting = (getattr(manifest, "settings", {}) or {}).get(entry.key) if manifest else None
    spec = getattr(setting, "spec", None) if setting is not None else None
    data["found"] = mask_setting(entry.key, entry.found, spec)
    data["want"] = mask_setting(entry.key, entry.want, spec)
    return data


# --------------------------------------------------------------------------
# Planner (§5.3) — pure; this table IS the mode semantics
# --------------------------------------------------------------------------


@dataclass
class Action:
    kind: str  # "setting" | "entity"
    op: str  # create | update | sweep
    target: str  # setting key or "<entity_kind>:<entity_id>"
    payload: Optional[dict[str, Any]] = None
    before: Any = None
    after: Any = None
    why: str = ""
    mode: str = ""
    entity_kind: str = ""
    entity_id: str = ""
    report_key: str = ""


@dataclass
class ReportEntry:
    key: str
    mode: str
    cadence: Optional[str]
    state: str  # ok | to_fix | pending | error | drift
    found: Any = None
    want: Any = None
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "mode": self.mode,
            "cadence": self.cadence,
            "state": self.state,
            "found": self.found,
            "want": self.want,
            "note": self.note,
        }


@dataclass
class PlanResult:
    actions: list[Action]
    entries: list[ReportEntry]

    def summary(self) -> dict[str, int]:
        counts = {"ok": 0, "fixed": 0, "pending": 0, "error": 0, "drift": 0}
        for entry in self.entries:
            counts[entry.state if entry.state in counts else "error"] += 1
        return counts


def _cadence_fires(cadence: Optional[str], triggers: frozenset[str]) -> bool:
    # Collections carry no cadence: they re-assert on every *write* trigger
    # (audit runs pass an empty trigger set, so writes stay zero — D3).
    if cadence is None:
        return bool(triggers)
    return cadence in triggers


def plan(
    manifest: Manifest,
    resolved: dict[str, tuple[Any, Optional[str]]],
    observed_config: dict[str, Any],
    observed_entities: dict[str, dict[str, dict]],
    desired_entities: dict[str, dict[str, dict]],
    triggers: frozenset[str],
) -> PlanResult:
    """Pure mode semantics: no I/O, no client, no clock.

    ``triggers`` is the firing set ("at_boot"/"on_upgrade"/"manual"); the
    empty set means audit-only (observe + report, zero write actions).
    """
    actions: list[Action] = []
    entries: list[ReportEntry] = []

    # -- settings: auth wiring first, then the rest, stable-sorted ----------
    wiring = {"auth.enable_api_keys", "auth.api_key.endpoint_restrictions",
              "auth.api_key.allowed_endpoints"}
    ordered_keys = (
        sorted(k for k in manifest.settings if k in wiring)
        + sorted(k for k in manifest.settings if k not in wiring)
    )
    for key in ordered_keys:
        entry = manifest.settings[key]
        report = ReportEntry(
            key=key, mode=entry.mode, cadence=entry.cadence, state="ok"
        )
        value, error = resolved.get(key, (None, "not resolved"))
        if error is not None:
            report.state = "error"
            report.note = error
            entries.append(report)
            continue
        report.want = value
        have = observed_config.get(key, _MISSING)
        report.found = None if have is _MISSING else have
        if entry.mode == "initial_setup":
            if have is _MISSING:
                report.state = "to_fix"
                actions.append(Action(
                    kind="setting", op="create", target=key,
                    payload={key: value}, after=value,
                    why="backfill missing initial_setup row",
                    mode=entry.mode, report_key=key,
                ))
            else:
                report.note = "initial_setup: row exists; never touched again"
            entries.append(report)
            continue
        # managed
        if have == value:
            entries.append(report)
            continue
        if not _cadence_fires(entry.cadence, triggers):
            report.state = "pending"
            report.note = f"drift; waits for {entry.cadence} cadence"
            entries.append(report)
            continue
        report.state = "to_fix"
        actions.append(Action(
            kind="setting",
            op="create" if have is _MISSING else "update",
            target=key,
            payload={key: value},
            before=None if have is _MISSING else have,
            after=value,
            why="managed value differs from observed",
            mode=entry.mode, report_key=key,
        ))
        entries.append(report)

    # -- entities -----------------------------------------------------------
    for kind in sorted(manifest.entities):
        spec = manifest.entities[kind]
        handler = ENTITY_KINDS[kind]
        observed = observed_entities.get(kind, {})
        desired = desired_entities.get(kind, {})
        for entity_id in sorted(desired):
            want = desired[entity_id]
            have = observed.get(entity_id)
            report = ReportEntry(
                key=f"{kind}:{entity_id}", mode=spec.mode, cadence=None, state="ok",
                want=want if kind != "models" else None,
            )
            if have is None:
                report.state = "to_fix"
                actions.append(Action(
                    kind="entity", op="create", target=f"{kind}:{entity_id}",
                    after=want, why="owned entity missing in Open WebUI",
                    mode=spec.mode, entity_kind=kind, entity_id=entity_id,
                    report_key=report.key,
                ))
            else:
                delta = handler.changes(spec, want, have)
                if delta and _cadence_fires(None, triggers):
                    report.state = "to_fix"
                    actions.append(Action(
                        kind="entity", op="update", target=f"{kind}:{entity_id}",
                        payload=delta, before=have, after=want,
                        why="owned entity drifted from CP intent",
                        mode=spec.mode, entity_kind=kind, entity_id=entity_id,
                        report_key=report.key,
                    ))
                elif delta:
                    report.state = "pending"
                    report.note = "drift; audit-only tick"
            entries.append(report)
        for entity_id in sorted(observed):
            if entity_id in desired:
                continue
            have = observed[entity_id]
            if spec.mode == "selective_management":
                # Foreign + unreserved: never touched, never reported.
                if handler.reserved(spec, have):
                    if _cadence_fires(None, triggers):
                        entries.append(ReportEntry(
                            key=f"{kind}:{entity_id}", mode=spec.mode, cadence=None,
                            state="to_fix", found=have.get("name"),
                            note="reserved-name collision; sweeping",
                        ))
                        actions.append(Action(
                            kind="entity", op="sweep", target=f"{kind}:{entity_id}",
                            before=have, why="foreign entity claims a reserved name",
                            mode=spec.mode, entity_kind=kind, entity_id=entity_id,
                            report_key=f"{kind}:{entity_id}",
                        ))
                    else:
                        entries.append(ReportEntry(
                            key=f"{kind}:{entity_id}", mode=spec.mode, cadence=None,
                            state="pending", found=have.get("name"),
                            note="reserved-name collision; audit-only tick",
                        ))
                continue
            entries.append(ReportEntry(
                key=f"{kind}:{entity_id}", mode=spec.mode, cadence=None,
                state="drift", found=have.get("name") or have.get("email"),
                note="exists in Open WebUI but not in Lemonseed; "
                     "the reconcile loop never deletes accounts",
            ))

    return PlanResult(actions=actions, entries=entries)


# --------------------------------------------------------------------------
# Engine state + last-known-good snapshot (§5.5) — CP Postgres only (D2)
# --------------------------------------------------------------------------


def load_state() -> dict[str, Any]:
    state = db.cp_get_meta(STATE_META_KEY)
    return state if isinstance(state, dict) else {}


def save_state(state: dict[str, Any]) -> bool:
    return db.cp_set_meta(STATE_META_KEY, state)


def write_lkg(config_map: dict[str, Any], policy_hash: str, owui_version: str) -> bool:
    """Single-slot last-known-good snapshot; called only after state=ok."""
    try:
        conn = db.cp_connect()
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO owui_config_lkg
                  (id, policy_hash, owui_version, config_map)
                VALUES (1, %s, %s, %s::jsonb)
                ON CONFLICT (id) DO UPDATE
                  SET policy_hash = EXCLUDED.policy_hash,
                      owui_version = EXCLUDED.owui_version,
                      config_map = EXCLUDED.config_map,
                      created_at = NOW()
                """,
                (policy_hash, owui_version, json.dumps(config_map)),
            )
        conn.close()
        return True
    except Exception as exc:  # noqa: BLE001 — storage boundary, snapshot is best-effort
        print(f"owui_policy LKG write: {exc}", flush=True)
        return False


def read_lkg() -> Optional[dict[str, Any]]:
    try:
        conn = db.cp_connect()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT policy_hash, owui_version, config_map, created_at "
                "FROM owui_config_lkg WHERE id = 1"
            )
            row = cur.fetchone()
        conn.close()
    except Exception as exc:  # noqa: BLE001
        print(f"owui_policy LKG read: {exc}", flush=True)
        return None
    if not row:
        return None
    policy_hash, owui_version, config_map, created_at = row
    if not isinstance(config_map, dict):  # JSONB normally comes back as dict
        try:
            config_map = json.loads(config_map)
        except (TypeError, json.JSONDecodeError):
            return None
    return {
        "policy_hash": policy_hash,
        "owui_version": owui_version,
        "config_map": config_map,
        "created_at": created_at.isoformat() if hasattr(created_at, "isoformat") else str(created_at),
    }


def restore_lkg(client: Any, lkg: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Replay the LKG config map through ``config.import``.

    Gated on a matching ``owui_version``: a config map written by a different
    OWUI build may carry keys/semantics this build does not understand
    (D1 mitigation ladder — otherwise skip, the ladder continues).
    """
    snapshot = lkg if lkg is not None else read_lkg()
    if not snapshot:
        return {"ok": False, "reason": "no LKG snapshot"}
    app_version = client.call("app.version")
    installed = ""
    if isinstance(app_version.get("data"), dict):
        installed = str(app_version["data"].get("version") or "")
    if not installed or installed != str(snapshot.get("owui_version") or ""):
        return {
            "ok": False,
            "reason": f"LKG owui_version {snapshot.get('owui_version')!r} "
                      f"!= installed {installed!r}",
        }
    result = client.call("config.import", json_body={"config": snapshot["config_map"]})
    if not result.get("ok"):
        return {"ok": False, "reason": f"config.import failed: {result.get('error')}"}
    return {"ok": True, "restored_keys": len(snapshot["config_map"])}


# --------------------------------------------------------------------------
# Reconcile entry point (§5.6) — the only I/O-performing layer, never raises
# --------------------------------------------------------------------------


@dataclass
class ReconcileDeps:
    """Injection seam from the module state machine (modules.py wires this in
    step 5) so the engine never has to import the supervisor (cycle-free)."""

    # (api_map, api_key, on_remint) -> OwuiClient-shaped transport.
    build_client: Callable[..., Any]
    # Kwargs for the client's trusted sign-in (the CP service identity).
    service_identity: dict[str, str]
    load_api_key: Callable[[], Optional[str]]
    persist_api_key: Callable[[str], bool]
    # Fresh per-run resolver context: module secrets, lemond base, resolved
    # placeholders (%CP_OWUI_ID% etc.) and the CP user list.
    refresh_context: Callable[[], ResolverContext]
    manifest_path: Optional[Path] = None


_reconcile_lock = threading.Lock()
_last_good_manifest: Optional[Manifest] = None
_status: dict[str, Any] = {
    "ok": False,
    "state": "idle",
    "manifest_error": None,
    "key_ready": False,
    "last_error": None,
    "last_reconciled": None,
    "last_trigger": [],
    "drift": None,
}


def state_summary() -> dict[str, Any]:
    """Cheap in-memory snapshot payload (no DB read on the HTTP path)."""
    return dict(_status)


def _set_status(**fields: Any) -> None:
    _status.update(fields)


def _utcnow_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def reconcile(port: int, triggers: frozenset[str], deps: ReconcileDeps) -> dict[str, Any]:
    """One reconcile run; **never raises** (module-boundary rule).

    ``triggers`` is the cadence firing set; pass the empty set for an
    audit-only run (observe + report, zero writes). Requires the OWUI slot
    healthy and the CP service identity provisioned (Phase 1); both are
    checked here as the cheap authoritative version.
    """
    if not _reconcile_lock.acquire(blocking=False):
        return {"ok": bool(_status.get("ok")), "skipped": "reconcile already running"}
    try:
        return _reconcile_run(int(port), frozenset(triggers), deps)
    except Exception as exc:  # noqa: BLE001 — service boundary; next tick retries
        _set_status(ok=False, state="error", last_error=str(exc)[:300])
        return {"ok": False, "last_error": str(exc)[:300]}
    finally:
        _reconcile_lock.release()


def _reconcile_run(port: int, triggers: frozenset[str], deps: ReconcileDeps) -> dict[str, Any]:
    global _last_good_manifest
    manifest: Optional[Manifest] = _last_good_manifest
    manifest_error: Optional[str] = None
    try:
        manifest = load_manifest(deps.manifest_path)
        _last_good_manifest = manifest
    except ManifestError as exc:
        manifest_error = str(exc)[:300]
        # Bad file on disk: keep serving the last-good manifest's state and
        # write nothing new from the rejected file.
        _set_status(manifest_error=manifest_error)
        if manifest is None:
            _set_status(ok=False, state="error",
                        last_error=f"no valid manifest: {manifest_error}")
            return {"ok": False, "last_error": _status["last_error"]}
    assert manifest is not None

    api_key = deps.load_api_key()
    client = deps.build_client(manifest.api, api_key, deps.persist_api_key)

    # Service sign-in every run: re-asserts the service role (Phase 1 path),
    # gives the client a JWT identity for 401 healing / key minting, and is
    # the hard precondition for every other call.
    sign = client.sign_in_trusted(**deps.service_identity)
    if not sign.get("ok"):
        error = f"service sign-in failed: {sign.get('error')}"
        _set_status(ok=False, state="error", last_error=error[:300],
                    manifest_error=manifest_error)
        return {"ok": False, "last_error": error[:300]}

    # Version gate (validator rule 7): an OWUI pin bump is an API-compat
    # event — drift-report only until the manifest is updated.
    version_result = client.app_version()
    installed = ""
    if isinstance(version_result.get("data"), dict):
        installed = str(version_result["data"].get("version") or "")
    if not version_at_least(installed, manifest.min_version):
        error = (f"installed OWUI {installed!r} < manifest min_version "
                 f"{manifest.min_version!r}; refusing to write")
        _set_status(ok=False, state="version-gate", last_error=error, owui_version=installed,
                    manifest_error=manifest_error)
        return {"ok": False, "last_error": error}

    ctx = deps.refresh_context()
    ctx.api_map = dict(manifest.api)

    observed_config_result = client.config_export()
    if not observed_config_result.get("ok") or not isinstance(
        observed_config_result.get("data"), dict
    ):
        error = f"config.export failed: {observed_config_result.get('error')}"
        _set_status(ok=False, state="error", last_error=error[:300])
        return {"ok": False, "last_error": error[:300]}
    observed_config: dict[str, Any] = observed_config_result["data"]

    resolved = {
        key: resolve_setting_value(entry, ctx)
        for key, entry in manifest.settings.items()
    }
    try:
        observed_entities: dict[str, dict[str, dict]] = {}
        desired_entities: dict[str, dict[str, dict]] = {}
        for kind, spec in manifest.entities.items():
            handler = ENTITY_KINDS[kind]
            observed_entities[kind] = handler.observe(client, spec, ctx)
            desired_entities[kind] = handler.desired(spec, ctx)
    except PolicyError as exc:
        _set_status(ok=False, state="error", last_error=str(exc)[:300])
        return {"ok": False, "last_error": str(exc)[:300]}

    result = plan(manifest, resolved, observed_config, observed_entities,
                  desired_entities, triggers)
    by_key = {entry.key: entry for entry in result.entries}
    first_error: Optional[str] = None
    for action in result.actions:
        entry = by_key.get(action.report_key)
        try:
            if action.kind == "setting":
                outcome = client.config_import(action.payload or {})
            else:
                outcome = ENTITY_KINDS[action.entity_kind].apply(client, action)
        except PolicyError as exc:
            outcome = {"ok": False, "error": str(exc)}
        if outcome.get("ok"):
            if entry is not None:
                entry.state = "fixed"
        else:
            if entry is not None:
                entry.state = "error"
                entry.note = f"apply failed: {outcome.get('error')}"[:200]
            if first_error is None:
                first_error = f"{action.target}: {outcome.get('error')}"[:300]

    # Machine key after settings land: on a fresh install the enable_api_keys
    # row only becomes true *through* the apply loop above (minting earlier
    # would 403 forever).
    key_ready = bool(client.api_key)
    if not key_ready:
        key_ready = bool(client.mint_api_key())

    # ok = every required entry satisfied. Optional-entry failures and
    # cadence-gated ``pending`` drift are reported but do not block the
    # iframe gate (D1: gate against a blank-slate OWUI, not against
    # audit observations).
    blocking = [
        e for e in result.entries
        if e.state in ("error", "to_fix")
        and not (e.key in manifest.settings and manifest.settings[e.key].optional)
    ]
    ok = not blocking and first_error is None
    summary = result.summary()
    drift = {**summary, "entries": [masked_report_entry(e, manifest) for e in result.entries]}
    last_error = first_error or (None if ok else "unresolved required drift")

    # LKG first, then state: the snapshot write timestamp rides along in the
    # saved state (host-side smoke proof that the LKG row exists). One row
    # carries across runs: only a successful write bump it. Audit ticks
    # (empty triggers) never snapshot: a drifted config is not last-known-good.
    lkg_at = load_state().get("lkg_at")
    if ok and triggers:
        fresh = client.config_export()
        if isinstance(fresh.get("data"), dict) and write_lkg(
            fresh["data"], manifest.policy_hash, installed
        ):
            lkg_at = _utcnow_iso()

    state = {
        "manifest_version": manifest.version,
        "policy_hash": manifest.policy_hash,
        "owui_version": installed,
        "last_reconciled": _utcnow_iso(),
        "last_trigger": sorted(triggers) or ["audit"],
        "last_error": last_error,
        "key_ready": key_ready,
        "lkg_at": lkg_at,
        "ok": ok,
        "drift": drift,
    }
    save_ok = save_state(state)
    if not save_ok:
        state["state_write_error"] = "cp_meta write failed; retried next tick"

    _set_status(
        ok=ok, state="ok" if ok else "error", manifest_error=manifest_error,
        key_ready=key_ready, last_error=last_error, owui_version=installed,
        policy_hash=manifest.policy_hash, manifest_version=manifest.version,
        last_reconciled=state["last_reconciled"], last_trigger=state["last_trigger"],
        lkg_at=state["lkg_at"], drift=drift,
    )
    return {**state, "state_saved": save_ok}
