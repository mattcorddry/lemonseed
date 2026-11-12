"""Module catalog — spec v1 parse, validate, merge, and load (M9 P2).

A module is a revision-controlled directory in the repo (``modules/<id>/``)
whose ``module.yaml`` declares identity, type, version, compatibility,
dependencies, the Docker service fragment, host-side hooks, data-plane needs,
portal-rendered settings, and (for Plugins only) a platform-config
contribution. ``documentation/roadmap/M9/MODULES.md`` is the spec; this file
is the machine reader.

Two layers share this file:

* **Build time** (``scripts/build-modules-catalog.py``, wired into the Nix
  build and run by ``make test``'s fixture checks): walk ``modules/``, parse
  and strictly validate every spec, check the cross-module invariants a
  single file cannot see, and merge everything into one
  ``modules-catalog.json`` with a sha256 digest per spec.
* **Runtime** (the control plane): load the merged catalog from the seeded
  writable copy (``/var/lib/lemonseed/modules/catalog/catalog.json``,
  refreshed from the shipped one when it is not newer), re-hash each module's
  ``module.yaml`` against its digest, and re-validate the loaded specs. The
  seeded copy is data on a writable path, so a hand edit there can add noise
  but cannot silently change intent without tripping one of the checks.

Validation is strict, in the spirit of ``owui-policy.json``: unknown keys and
wrong types are rejected, not ignored, and the whole merge fails closed when
any module does not validate.

The appliance runtime never parses YAML — it reads the merged catalog.json —
so PyYAML is a build-time dependency of the merge step only.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Optional

SPEC_VERSION = 1
CATALOG_VERSION = 1
CATALOG_NAME = "modules-catalog.json"

MODULE_ID_RE = re.compile(r"^[a-z][a-z0-9-]{1,30}$")
# A contribution becomes an mcp_servers key in LiteLLM's config, and LiteLLM
# rejects keys containing a hyphen at startup — so the catalog refuses one
# here, before it can break the chat path (MODULES.md §6).
MCP_NAME_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,62}$")
DB_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
CONTAINER_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,62}$")
# Appliance versions are ``YYYYMMDD.NN`` (UTC date + build counter); the
# trailing ``.NN`` is optional in the compat floor for hand-written specs.
BUILD_VERSION_RE = re.compile(r"^(\d{8})(?:\.(\d+))?$")

HOOK_EVENTS = ("install", "remove", "start", "stop", "user")
CORE_DEPENDENCIES = frozenset({"postgres", "valkey", "lemond", "litellm"})
MODULE_TYPES = ("application", "plugin")
MATURITIES = ("alpha", "beta", "production")
SETTINGS_TYPES = ("string", "bool", "int", "enum")

HOOK_TIMEOUT_DEFAULT_SEC = 120
HOOK_TIMEOUT_MIN_SEC = 5
HOOK_TIMEOUT_MAX_SEC = 600

MODULES_VOLUME_ROOT = "/var/lib/lemonseed/modules"
# The one hard rule the framework makes about the Docker daemon: a module
# never reaches it. Enforced where the args are built (§7) and audited where
# they are declared (§3) so a spec cannot smuggle it past the reader.
_DOCKER_SOCK = ("/var/run/docker.sock", "/run/docker.sock")


class CatalogError(RuntimeError):
    """A spec is missing, unreadable, or invalid. Never guess at intent."""


# ---------------------------------------------------------------------------
# Parsing and validation
# ---------------------------------------------------------------------------


def parse_spec_text(text: str, source: str = "module.yaml") -> dict[str, Any]:
    """Parse one ``module.yaml``. PyYAML is imported lazily: the appliance
    runtime never calls this, so the CP image carries no YAML dependency."""
    try:
        import yaml  # noqa: PLC0415 — build-time only; see the module docstring
    except ImportError as exc:  # pragma: no cover — exercised only on hosts
        raise CatalogError(
            "PyYAML is required to read module specs (pip install pyyaml, or"
            " run the catalog merge step under the Nix build environment)"
        ) from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise CatalogError(f"{source} is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise CatalogError(
            f"{source} must be a mapping, got {type(data).__name__}"
        )
    return data


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_str(value: Any) -> bool:
    return isinstance(value, str)


def _require(condition: Any, message: str) -> None:
    if not condition:
        raise CatalogError(message)


def validate_spec(raw: Any, *, module_id: str = "<spec>") -> dict[str, Any]:
    """Strictly validate one spec and return it unchanged.

    ``raw`` is normally a parsed ``module.yaml`` (build time) or the ``spec``
    block of a catalog entry (runtime, where PyYAML is absent). Unknown keys
    and wrong types are rejected rather than ignored, so a future spec v2 can
    rely on the reader never silently dropping a field.
    """
    prefix = f"module {module_id!r}:"
    _require(isinstance(raw, dict), f"{prefix} spec must be a mapping")
    spec = raw
    unknown = set(spec) - _TOP_KEYS
    _require(not unknown, f"{prefix} unknown top-level keys: {sorted(unknown)}")

    _require(
        spec.get("spec_version") == SPEC_VERSION,
        f"{prefix} spec_version must be {SPEC_VERSION}",
    )
    mid = spec.get("id")
    _require(
        _is_str(mid) and MODULE_ID_RE.match(mid),
        f"{prefix} id must match {MODULE_ID_RE.pattern}",
    )
    if module_id != "<spec>":
        _require(mid == module_id, f"{prefix} id {mid!r} must equal its directory name")
    for key, label in (
        ("name", "display name"),
        ("version", "version"),
        ("license", "license"),
    ):
        _require(_is_str(spec.get(key)) and spec[key].strip(), f"{prefix} {label} is required")
    _require(spec.get("type") in MODULE_TYPES, f"{prefix} type must be one of {MODULE_TYPES}")
    _require(
        spec.get("maturity") in MATURITIES,
        f"{prefix} maturity must be one of {MATURITIES}",
    )
    owners = spec.get("owners")
    _require(
        isinstance(owners, list) and owners
        and all(_is_str(o) and o.strip() for o in owners),
        f"{prefix} owners must be a non-empty list of GitHub handles",
    )
    homepage = spec.get("homepage")
    if homepage is not None:
        _require(
            _is_str(homepage)
            and homepage.startswith(("http://", "https://")),
            f"{prefix} homepage must be an http(s) URL",
        )
    for key in ("description",):
        _require(
            spec.get(key) is None or _is_str(spec[key]),
            f"{prefix} {key} must be a string",
        )
    logo = spec.get("logo")
    if logo is not None:
        _require(
            _is_str(logo)
            and not logo.startswith("/")
            and ".." not in Path(logo).parts,
            f"{prefix} logo must be a relative path inside the module directory",
        )

    compat = spec.get("compat")
    if compat is not None:
        _require(
            isinstance(compat, dict) and set(compat) <= {"min_lemonseed"},
            f"{prefix} compat supports only min_lemonseed",
        )
        floor = compat.get("min_lemonseed")
        if floor is not None:
            _require(
                _is_str(floor) and BUILD_VERSION_RE.match(floor),
                f"{prefix} compat.min_lemonseed must look like 20261112.01",
            )

    deps = spec.get("dependencies") or {}
    _require(
        isinstance(deps, dict) and set(deps) <= {"core", "modules"},
        f"{prefix} dependencies supports only core and modules",
    )
    for key, allowed in (("core", CORE_DEPENDENCIES), ("modules", None)):
        values = deps.get(key) or []
        _require(
            isinstance(values, list) and all(_is_str(v) for v in values),
            f"{prefix} dependencies.{key} must be a list of names",
        )
        _require(
            len(set(values)) == len(values),
            f"{prefix} dependencies.{key} has duplicates",
        )
        if allowed is not None:
            _require(
                set(values) <= allowed,
                f"{prefix} dependencies.core must be a subset of {sorted(allowed)}",
            )

    docker = spec.get("docker")
    _require(isinstance(docker, dict), f"{prefix} docker block is required")
    _validate_docker(docker, prefix)
    # The control plane derives the container name from the module id so
    # teardown never needs a spec that may no longer exist (a removed
    # upstream entry must still be stoppable).
    _require(
        docker.get("container") == f"lemonseed-{spec['id']}",
        f"{prefix} docker.container must be lemonseed-<module id>",
    )

    hooks = spec.get("hooks")
    if hooks is not None:
        _require(
            isinstance(hooks, dict) and set(hooks) <= {"script_timeout"},
            f"{prefix} hooks supports only script_timeout",
        )
        timeout = hooks.get("script_timeout")
        if timeout is not None:
            _require(
                _is_int(timeout)
                and HOOK_TIMEOUT_MIN_SEC <= timeout <= HOOK_TIMEOUT_MAX_SEC,
                f"{prefix} hooks.script_timeout must be an int in"
                f" {HOOK_TIMEOUT_MIN_SEC}-{HOOK_TIMEOUT_MAX_SEC} seconds",
            )

    data = spec.get("data")
    if data is not None:
        _require(
            isinstance(data, dict) and set(data) <= {"postgres", "valkey"},
            f"{prefix} data supports only postgres and valkey",
        )
        pg = data.get("postgres")
        if pg is not None:
            _require(
                isinstance(pg, dict) and set(pg) == {"db_name"},
                f"{prefix} data.postgres takes exactly db_name",
            )
            _require(
                _is_str(pg["db_name"]) and DB_NAME_RE.match(pg["db_name"]),
                f"{prefix} data.postgres.db_name must match {DB_NAME_RE.pattern}",
            )
        vk = data.get("valkey")
        if vk is not None:
            _require(
                isinstance(vk, dict) and set(vk) == {"db"},
                f"{prefix} data.valkey takes exactly db",
            )
            _require(
                _is_int(vk["db"]) and 1 <= vk["db"] <= 15,
                f"{prefix} data.valkey.db must be an int 1-15",
            )

    settings = spec.get("settings")
    if settings is not None:
        _require(
            isinstance(settings, list),
            f"{prefix} settings must be a list of entries",
        )
        seen: set[str] = set()
        for entry in settings:
            _require(
                isinstance(entry, dict)
                and set(entry)
                <= {"name", "type", "default", "label", "required", "choices"},
                f"{prefix} settings entry has unknown keys: {sorted(entry)}",
            )
            name = entry.get("name")
            _require(
                _is_str(name) and ENV_NAME_RE.match(name),
                f"{prefix} settings name must match {ENV_NAME_RE.pattern}",
            )
            _require(name not in seen, f"{prefix} settings name {name!r} is duplicated")
            seen.add(name)
            kind = entry.get("type")
            _require(kind in SETTINGS_TYPES, f"{prefix} settings {name} type is invalid")
            default = entry.get("default")
            if kind == "bool":
                _require(
                    default is None or isinstance(default, bool),
                    f"{prefix} settings {name} default must be a bool",
                )
            elif kind == "int":
                _require(
                    default is None or _is_int(default),
                    f"{prefix} settings {name} default must be an int",
                )
            elif kind == "enum":
                choices = entry.get("choices")
                _require(
                    isinstance(choices, list) and choices
                    and all(_is_str(c) for c in choices),
                    f"{prefix} settings {name} enum needs a non-empty choices list",
                )
                _require(
                    default is None or default in choices,
                    f"{prefix} settings {name} default must be one of its choices",
                )
            else:
                _require(
                    default is None or _is_str(default),
                    f"{prefix} settings {name} default must be a string",
                )

    platform = spec.get("platform")
    if platform is not None:
        _require(
            spec["type"] == "plugin",
            f"{prefix} only a plugin may declare a platform block",
        )
        _require(
            isinstance(platform, dict) and set(platform) <= {"litellm"},
            f"{prefix} platform supports only litellm",
        )
        litellm = platform.get("litellm")
        if litellm is not None:
            _require(
                isinstance(litellm, dict) and set(litellm) == {"mcp_server"},
                f"{prefix} platform.litellm takes exactly mcp_server",
            )
            server = litellm["mcp_server"]
            _require(
                isinstance(server, dict) and set(server) == {"name", "url"},
                f"{prefix} platform.litellm.mcp_server takes exactly name and url",
            )
            _require(
                _is_str(server["name"]) and MCP_NAME_RE.match(server["name"]),
                f"{prefix} platform.litellm.mcp_server.name must match"
                f" {MCP_NAME_RE.pattern} (LiteLLM rejects keys with hyphens)",
            )
            _require(
                _is_str(server["url"])
                and server["url"].startswith("http://"),
                f"{prefix} platform.litellm.mcp_server.url must be an http:// URL"
                " on the lemon network",
            )

    network = spec.get("network")
    if network is not None:
        _require(
            isinstance(network, dict) and set(network) == {"outbound"},
            f"{prefix} network takes exactly outbound",
        )
        outbound = network["outbound"]
        _require(isinstance(outbound, list), f"{prefix} network.outbound must be a list")
        for entry in outbound:
            _require(
                isinstance(entry, dict) and set(entry) == {"host", "port", "reason"},
                f"{prefix} network.outbound entry takes exactly host, port, reason",
            )
            _require(
                _is_str(entry["host"]) and entry["host"].strip(),
                f"{prefix} network.outbound.host is required",
            )
            _require(
                _is_int(entry["port"]) and 1 <= entry["port"] <= 65535,
                f"{prefix} network.outbound.port must be an int 1-65535",
            )
            _require(
                _is_str(entry["reason"]) and entry["reason"].strip(),
                f"{prefix} network.outbound.reason is required",
            )
    return spec


_TOP_KEYS = frozenset(
    {
        "spec_version",
        "id",
        "name",
        "type",
        "version",
        "maturity",
        "license",
        "owners",
        "homepage",
        "description",
        "logo",
        "compat",
        "dependencies",
        "docker",
        "hooks",
        "data",
        "settings",
        "platform",
        "network",
    }
)

_DOCKER_KEYS = frozenset(
    {"image", "container", "network", "ports", "volumes", "run_args", "probe"}
)


def _validate_docker(docker: dict[str, Any], prefix: str) -> None:
    unknown = set(docker) - _DOCKER_KEYS
    _require(not unknown, f"{prefix} unknown docker keys: {sorted(unknown)}")
    image = docker.get("image")
    _require(
        _is_str(image) and image.strip(),
        f"{prefix} docker.image is the key of a pin in container-images.json",
    )
    container = docker.get("container")
    _require(
        _is_str(container)
        and CONTAINER_RE.match(container)
        and container.startswith("lemonseed-"),
        f"{prefix} docker.container must be a docker-safe name prefixed lemonseed-",
    )
    network = docker.get("network")
    if network is not None:
        _require(network == "lemon", f"{prefix} docker.network is fixed to lemon")
    ports = docker.get("ports") or []
    _require(
        isinstance(ports, list) and all(_is_str(p) for p in ports),
        f"{prefix} docker.ports must be a list of '127.0.0.1:<host>:<container>' strings",
    )
    for port in ports:
        parts = port.split(":")
        _require(
            len(parts) == 3 and parts[0] == "127.0.0.1",
            f"{prefix} docker.port {port!r} must publish to 127.0.0.1 only",
        )
        _require(
            all(p.isdigit() and 1 <= int(p) <= 65535 for p in parts[1:]),
            f"{prefix} docker.port {port!r} has an invalid port",
        )
    volumes = docker.get("volumes") or []
    _require(
        isinstance(volumes, list) and all(_is_str(v) for v in volumes),
        f"{prefix} docker.volumes must be a list of host:container strings",
    )
    for volume in volumes:
        # The forbidden mount wins over the generic under-root rule, so the
        # error names the real risk rather than a path policy.
        for sock in _DOCKER_SOCK:
            _require(sock not in volume, f"{prefix} docker.volume may not reach the Docker daemon")
        host = volume.split(":")[0]
        _require(
            host == MODULES_VOLUME_ROOT
            or host.startswith(MODULES_VOLUME_ROOT.rstrip("/") + "/"),
            f"{prefix} docker.volume {volume!r} must mount under"
            f" {MODULES_VOLUME_ROOT}/<module-id>/",
        )
    run_args = docker.get("run_args") or []
    _require(
        isinstance(run_args, list) and all(_is_str(a) for a in run_args),
        f"{prefix} docker.run_args must be a list of strings",
    )
    audit_run_args(run_args, prefix)
    probe = docker.get("probe")
    if probe is not None:
        _require(
            isinstance(probe, dict) and set(probe) <= {"port", "path"},
            f"{prefix} docker.probe takes only port and path",
        )
        _require(
            _is_int(probe.get("port")),
            f"{prefix} docker.probe.port must be an int (the published host port)",
        )
        path = probe.get("path", "/")
        _require(
            _is_str(path) and path.startswith("/"),
            f"{prefix} docker.probe.path must be an absolute HTTP path",
        )
        host_ports = {int(p.split(":")[1]) for p in ports}
        _require(
            probe["port"] in host_ports,
            f"{prefix} docker.probe.port must be one of the published host ports",
        )


def audit_run_args(run_args: list[str], prefix: str = "") -> None:
    """Reject the ``docker run`` args a module may never declare (§3, §7).

    The framework's hard rules: no privileged execution, no host PID or
    network namespace, no extra mounts (the declared volumes are the only
    mount surface), no override of the restart policy the supervisor owns,
    and above all no path to the Docker daemon. Publishes stay on 127.0.0.1
    because the gateway is the only LAN entry.
    """

    def _next_value(tokens: list[str], index: int) -> Optional[str]:
        if index + 1 < len(tokens) and not tokens[index + 1].startswith("-"):
            return tokens[index + 1]
        return None

    joined = " ".join(run_args).lower()
    for sock in _DOCKER_SOCK:
        _require(
            sock not in joined,
            f"{prefix} run_args may not reach the Docker daemon",
        )
    for index, token in enumerate(run_args):
        lowered = token.lower()
        for sock in _DOCKER_SOCK:
            _require(sock not in lowered, f"{prefix} run_args may not reach the Docker daemon")
        _require(
            not lowered.startswith("--privileged"),
            f"{prefix} run_args may not use {token!r}",
        )
        if lowered == "--pid" or lowered.startswith("--pid="):
            value = token.split("=", 1)[1] if "=" in token else _next_value(run_args, index)
            _require(value != "host", f"{prefix} run_args may not use --pid=host")
        if lowered in ("--network", "--net") or lowered.startswith(("--network=", "--net=")):
            value = token.split("=", 1)[1] if "=" in token else _next_value(run_args, index)
            _require(
                value is None or value == "lemon",
                f"{prefix} run_args may not join a network other than lemon",
            )
        if lowered in ("-v", "--volume", "--mount"):
            raise CatalogError(
                f"{prefix} run_args may not mount volumes; declare them under docker.volumes"
            )
        if lowered in ("-p", "--publish"):
            value = token.split("=", 1)[1] if "=" in token else _next_value(run_args, index)
            _require(
                value is None or value.startswith("127.0.0.1:"),
                f"{prefix} run_args may only publish to 127.0.0.1",
            )
        _require(
            not lowered.startswith("--restart"),
            f"{prefix} run_args may not override the supervisor's restart policy",
        )


# ---------------------------------------------------------------------------
# Build-time merge
# ---------------------------------------------------------------------------


def spec_path(modules_dir: Path, module_id: str) -> Path:
    return modules_dir / module_id / "module.yaml"


def build_catalog(
    modules_dir: Path, *, pins: Optional[dict[str, Any]] = None
) -> dict[str, Any]:
    """Merge every ``modules/*/module.yaml`` into the shipped catalog.

    Fails closed: one invalid module rejects the whole merge, because a
    catalog that half-works is worse than none — the installer would offer a
    module the box cannot actually run.
    """
    if pins is None:
        pins_path = Path(__file__).resolve().with_name("container-images.json")
        try:
            pins = json.loads(pins_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise CatalogError(f"container image pins unreadable: {exc}") from exc

    if not modules_dir.is_dir():
        raise CatalogError(f"modules directory {modules_dir} does not exist")
    modules: dict[str, dict[str, Any]] = {}
    for child in sorted(modules_dir.iterdir()):
        if not child.is_dir():
            continue
        module_id = child.name
        path = spec_path(modules_dir, module_id)
        if not path.is_file():
            raise CatalogError(f"module directory {module_id!r} has no module.yaml")
        raw = path.read_bytes()
        spec = validate_spec(parse_spec_text(raw.decode("utf-8"), str(path)), module_id=module_id)
        _require(
            (spec["docker"]["image"]) in pins,
            f"module {module_id!r}: docker.image {spec['docker']['image']!r}"
            " has no pin in container-images.json",
        )
        for dep in (spec.get("dependencies") or {}).get("modules") or []:
            _require(
                dep != module_id,
                f"module {module_id!r} depends on itself",
            )
        modules[module_id] = {"spec": spec, "sha256": hashlib.sha256(raw).hexdigest()}

    for module_id, entry in modules.items():
        spec = entry["spec"]
        for dep in (spec.get("dependencies") or {}).get("modules") or []:
            _require(
                dep in modules,
                f"module {module_id!r} depends on missing module {dep!r}",
            )
    _check_unique(modules, "docker", "container", "docker.container")
    _check_unique_host_ports(modules)
    _check_unique_valkey(modules)
    for module_id, entry in modules.items():
        server = (((entry["spec"].get("platform") or {}).get("litellm") or {})
                  .get("mcp_server") or {})
        if server:
            name = server["name"]
            clash = [
                other
                for other, other_entry in modules.items()
                if other != module_id
                and ((((other_entry["spec"].get("platform") or {}).get("litellm") or {})
                      .get("mcp_server") or {}).get("name")) == name
            ]
            _require(
                not clash,
                f"module {module_id!r}: mcp server name {name!r} is also declared by"
                f" {', '.join(sorted(clash))}",
            )
    dependency_order(modules)  # raises on a cycle
    return {
        "catalog_version": CATALOG_VERSION,
        "generated_from": "modules/",
        "modules": {key: modules[key] for key in sorted(modules)},
    }


def _check_unique(
    modules: dict[str, dict[str, Any]], section: str, key: str, label: str
) -> None:
    seen: dict[str, str] = {}
    for module_id, entry in modules.items():
        value = (entry["spec"].get(section) or {}).get(key)
        if value is None:
            continue
        if value in seen:
            raise CatalogError(
                f"module {module_id!r}: {label} {value!r} is also used by"
                f" {seen[value]!r}"
            )
        seen[value] = module_id


def _check_unique_host_ports(modules: dict[str, dict[str, Any]]) -> None:
    seen: dict[int, str] = {}
    for module_id, entry in modules.items():
        for port in (entry["spec"].get("docker") or {}).get("ports") or []:
            host = int(port.split(":")[1])
            if host in seen:
                raise CatalogError(
                    f"module {module_id!r}: host port {host} is already published"
                    f" by {seen[host]!r}"
                )
            seen[host] = module_id


def _check_unique_valkey(modules: dict[str, dict[str, Any]]) -> None:
    seen: dict[int, str] = {}
    for module_id, entry in modules.items():
        vk = ((entry["spec"].get("data") or {}).get("valkey") or {}).get("db")
        if vk is None:
            continue
        if vk in seen:
            raise CatalogError(
                f"module {module_id!r}: valkey db {vk} is already claimed by"
                f" {seen[vk]!r}"
            )
        seen[vk] = module_id


def dependency_order(modules: dict[str, Any]) -> list[str]:
    """Kahn topological order over ``dependencies.modules``.

    Core services (Postgres, Valkey, lemond, LiteLLM) are always provisioned
    before any module, so they are not nodes here. Raises with the cycle path
    when the catalog's dependency graph is not a DAG.
    """
    deps: dict[str, set[str]] = {}
    for module_id, entry in modules.items():
        spec = entry["spec"] if isinstance(entry, dict) and "spec" in entry else entry
        wanted = {
            d
            for d in ((spec.get("dependencies") or {}).get("modules") or [])
            if d != module_id
        }
        deps[module_id] = wanted
    order: list[str] = []
    ready = sorted(mid for mid, wants in deps.items() if not wants)
    while ready:
        mid = ready.pop(0)
        order.append(mid)
        for other, wants in sorted(deps.items()):
            if mid in wants:
                wants.discard(mid)
                if not wants and other not in order:
                    ready.append(other)
        ready.sort()
    if len(order) != len(deps):
        stuck = sorted(set(deps) - set(order))
        raise CatalogError(
            "module dependency cycle: " + " -> ".join(stuck + [stuck[0]])
        )
    return order


# ---------------------------------------------------------------------------
# Compatibility
# ---------------------------------------------------------------------------


def _parse_build_version(version: str) -> Optional[tuple[int, int]]:
    match = BUILD_VERSION_RE.match((version or "").strip())
    if not match:
        return None
    return int(match.group(1)), int(match.group(2) or 0)


def compat_reason(spec: dict[str, Any], appliance_version: str) -> Optional[str]:
    """Why this module does not fit the running appliance, or None.

    An unreadable running version never filters the catalog: a version file
    that says "unknown" must not hide every module from the admin who most
    needs to see what is installed.
    """
    floor = str(((spec.get("compat") or {}).get("min_lemonseed") or ""))
    if not floor:
        return None
    floor_parts = _parse_build_version(floor)
    if floor_parts is None:
        return f"spec declares an unreadable min_lemonseed {floor!r}"
    running = _parse_build_version(appliance_version)
    if running is None:
        return None
    if running < floor_parts:
        return (
            f"requires Lemonseed {floor} or newer (this appliance runs"
            f" {appliance_version})"
        )
    return None


# ---------------------------------------------------------------------------
# Runtime loading
# ---------------------------------------------------------------------------


def shipped_catalog_path() -> Path:
    """The catalog generated at build time, shipped beside the CP code."""
    return Path(__file__).resolve().with_name(CATALOG_NAME)


def durable_catalog_path() -> Path:
    """The writable copy a future hosted-catalog refresh may replace."""
    return Path("/var/lib/lemonseed/modules/catalog/catalog.json")


def module_root() -> Path:
    """The read-only image path carrying every module directory."""
    return Path(__file__).resolve().parent / "modules"


def ensure_seeded_catalog(
    shipped: Path, durable: Path
) -> dict[str, Any]:
    """Seed the writable catalog copy from the shipped one (§4).

    The shipped file is the authority at every boot: an appliance update
    ships a new catalog, and the writable copy must follow it. A durable copy
    with a *higher* catalog_version wins — that is the future hosted-catalog
    refresh, which must not be clobbered by a re-seed after an update.
    """
    try:
        shipped_data = json.loads(shipped.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"state": "missing-shipped", "seeded": False}
    shipped_version = int(shipped_data.get("catalog_version") or 0)
    try:
        durable_data = json.loads(durable.read_text(encoding="utf-8"))
        durable_version = int(durable_data.get("catalog_version") or 0)
        same = durable.read_bytes() == shipped.read_bytes()
    except (OSError, ValueError):
        durable_version = -1
        same = False
    if durable_version > shipped_version:
        return {"state": "kept-newer-durable", "seeded": False}
    if durable_version >= 0 and same:
        return {"state": "present", "seeded": False}
    try:
        durable.parent.mkdir(parents=True, exist_ok=True)
        tmp = durable.with_name(durable.name + ".tmp")
        tmp.write_text(
            json.dumps(shipped_data, indent=2, sort_keys=False) + "\n",
            encoding="utf-8",
        )
        tmp.replace(durable)
    except OSError as exc:
        return {"state": "seed-failed", "seeded": False, "error": str(exc)}
    return {"state": "seeded", "seeded": True}


def load_catalog(
    *,
    appliance_version: str = "unknown",
    shipped: Optional[Path] = None,
    durable: Optional[Path] = None,
    root: Optional[Path] = None,
) -> dict[str, Any]:
    """Load the catalog the CP operates on, with per-module problems.

    Returns ``{"modules": {id: spec}, "problems": [str], "source": str,
    "catalog_version": int}``. A module is dropped (never half-trusted) when
    its spec fails validation, its digest does not match the shipped
    ``module.yaml``, or its image pin is missing.
    """
    shipped = shipped or shipped_catalog_path()
    durable = durable or durable_catalog_path()
    root = root or module_root()
    if durable.is_file():
        source = durable
    else:
        source = shipped
    try:
        catalog = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {
            "modules": {},
            "problems": [f"catalog unreadable at {source}: {exc}"],
            "source": str(source),
            "catalog_version": 0,
        }
    problems: list[str] = []
    modules: dict[str, Any] = {}
    for module_id, entry in sorted((catalog.get("modules") or {}).items()):
        spec = entry.get("spec") if isinstance(entry, dict) else None
        if not isinstance(spec, dict):
            problems.append(f"module {module_id!r}: catalog entry has no spec")
            continue
        try:
            validate_spec(spec, module_id=module_id)
        except CatalogError as exc:
            problems.append(str(exc))
            continue
        digest = entry.get("sha256")
        try:
            actual = hashlib.sha256(spec_path(root, module_id).read_bytes()).hexdigest()
        except OSError:
            problems.append(
                f"module {module_id!r}: module.yaml missing from the image path"
            )
            continue
        if digest != actual:
            problems.append(
                f"module {module_id!r}: module.yaml does not match its catalog"
                " digest (the image is not the build that shipped this catalog)"
            )
            continue
        try:
            import image_pins  # noqa: PLC0415 — lazy: keeps this file testable standalone

            image_pins.entry(spec["docker"]["image"])
        except Exception as exc:  # noqa: BLE001 — ImagePinError is a refusal, not a crash
            problems.append(f"module {module_id!r}: {exc}")
            continue
        modules[module_id] = spec
    return {
        "modules": modules,
        "problems": problems,
        "source": str(source),
        "catalog_version": int(catalog.get("catalog_version") or 0),
    }
