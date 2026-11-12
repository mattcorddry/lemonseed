# M9 P2 — Module catalog spec v1 + CP lifecycle

Approved 2026-11-12 (§11 records the owner's answers). The brief is **as simple as possible**: one declarative YAML
schema per module, one in-tree catalog directory, one merge step with integrity
checks, and a lifecycle the CP already knows how to run (pull → run → supervise on
the data-services pattern). No hosted catalog, no outbound-ACL enforcement, no
multi-container modules — all deferred per `PLAN.md`.

`MODULES-FRAMEWORK.md` is the requirements source; this document is the v1
specification that settles its open points. When P2 lands, `MODULES-FRAMEWORK.md`
becomes the published spec and this document carries only the build/lifecycle
mechanics.

## 1. Summary

A module is a **revision-controlled directory** in the repo (`modules/<id>/`) whose
`module.yaml` declares identity, type, version, compatibility, dependencies, the
Docker service fragment, host-side hooks, data-plane needs, settings the portal
renders, and (for Plugins) a platform-config contribution. A build step merges every
module into one **catalog file** with a digest per module; the appliance image ships
the catalog and the module directories read-only; the boot seeder copies the merged
catalog into `/var/lib/lemonseed/modules/catalog/` so the future hosted-catalog
refresh can replace that path without moving it. The CP installs/enables/disables/
removes modules from the catalog with dependency resolution, provisioning, hooks and
blue/green-free container management, and renders the portal's Modules page as one
tab with two groups (Installed / Available) — as built; the plan's original two-tab
split was superseded because the settings rail is already five tabs wide.

## 2. In-tree layout

```
modules/
  mcp-gateway/
    module.yaml          # the spec (below)
    hooks/               # host-side scripts, executed by the CP (§7)
      install.sh
      remove.sh
      start.sh
      stop.sh
      user.sh
    test/                # no-external-dependency harness (§9)
      test.sh
  hermes/                # P6
    module.yaml
    ...
```

- One directory per module; the directory name **must** equal the module `id`
  (merge step enforces it).
- `modules/` sits at the repo root, next to `nixos/`. `nixos/modules/` is
  namespaced under `nixos/`, so the two do not collide; the top-level name is the
  one `MODULES-FRAMEWORK.md` already promises.
- Supporting files (logos, static assets) live in the module directory too; the
  runtime path for everything in-tree is the read-only SquashFS (the CP refers to
  modules by `Path(__file__).parent / "modules"`'s packaged sibling — the nix
  packaging copies the tree beside the CP code, exactly like `catalog/models.json`).

## 3. `module.yaml` — spec v1

Strict validation, in the spirit of `owui-policy.json`: unknown keys and wrong
types are **rejected, not ignored**, and the whole catalog merge fails closed if
any module does not validate. `spec_version: 1` is the only accepted value; a
future breaking change ships as spec v2 alongside a v1 reader.

```yaml
spec_version: 1
id: mcp-gateway                  # [a-z][a-z0-9-]{1,30}; = directory name
name: MCP Gateway                # display name
type: plugin                     # application | plugin
version: 1.0.0                   # upstream release this catalog entry tracks
maturity: beta                   # alpha | beta | production
license: Apache-2.0
owners: [wago]                   # GitHub handles; merge step requires >= 1
homepage: https://github.com/docker/mcp-gateway
description: >-
  One MCP endpoint aggregating fetch, duckduckgo, memory and time for
  Open WebUI and other modules, through LiteLLM.
logo: logo.svg                   # optional, path inside the module directory

compat:
  min_lemonseed: 20261109.01     # optional; appliance version floor (§5)

dependencies:
  core: [litellm]                # subset of {postgres, valkey, lemond, litellm}
  modules: []                    # other module ids; auto-installed, topologically
                                 # ordered; cycles are a catalog error

docker:
  image: mcp-gateway             # KEY in control-plane/container-images.json —
                                 # never a raw tag; the pin carries tag + digest
  container: lemonseed-mcp-gateway
  network: lemon                 # fixed; the only network a module may join
  ports: []                      # 127.0.0.1 publishes only, if any (default none)
  volumes:
    - /var/lib/lemonseed/modules/mcp-gateway:/data
  run_args: []                   # extra `docker run` args, CP-audited (below)

data:
  postgres:                      # optional; CP provisions role + database
    db_name: mcp_gateway
  valkey:                        # optional; CP provisions a logical DB index
    db: 2

hooks:                           # optional; host-side scripts (§7)
  script_timeout: 120            # seconds; the CP clamps it to 5–600

settings:                        # optional; portal-rendered, env-delivered (§7)
  - name: ALLOWED_HOSTS
    type: string
    default: ""
    label: Allowed hosts
    required: false

platform:                        # optional; Plugins only (§6)
  litellm:
    mcp_server:
      name: fetch                # must be unique across the whole catalog
      url: http://lemonseed-mcp-gateway:8010/mcp

network:                         # DECLARED, NOT ENFORCED in v1 (§5)
  outbound:
    - host: api.github.com
      port: 443
      reason: module update checks
```

### Merge step and integrity

`scripts/build-modules-catalog.py` (wired into the build, and run by
`make test`'s fixture checks so a bad module fails CI before it fails a release):

- Validates every `module.yaml` against the v1 schema (strict, fail-closed).
- Checks invariants the single-file view cannot see: directory name = id, unique
  `platform.litellm.mcp_server.name`, no dependency cycles, every `docker.image`
  key exists in `container-images.json`, every dependency id exists.
- Emits `modules-catalog.json`:

```json
{
  "catalog_version": 1,
  "generated_from": "modules/",
  "modules": {
    "mcp-gateway": {
      "spec": { ...the validated module.yaml... },
      "sha256": "…digest of module.yaml bytes…"
    }
  }
}
```

The runtime loader re-hashes each module's `module.yaml` against the digest and
**refuses a module whose bytes do not match** — a hand edit of the writable copy
can add noise but cannot silently change intent. The digest covers the spec only;
hooks and assets are inside the read-only image, where tampering would already
mean a compromised image.

## 4. Distribution and seeding

| Artifact | Build-time home | Runtime home |
|----------|-----------------|--------------|
| Merged catalog | `modules-catalog.json` (generated, shipped in the image beside the CP code) | `/var/lib/lemonseed/modules/catalog/catalog.json`, seeded at boot |
| Module specs + hooks + assets | `modules/<id>/` (shipped in the image) | read-only image path; **hooks execute from here, never from the seeded copy** |
| Module state | — | CP Postgres `modules` table |
| Module data volumes | — | `/var/lib/lemonseed/modules/<id>/` |

Rationale for hooks staying on the read-only path: `/var/lib` is writable, so a
catalog dir that could carry executable hooks would be a code-injection surface
the appliance does not otherwise have. The seeded copy is **data only** (the
catalog file), which is why it can be refreshed by the future hosted-catalog
mechanism without a re-install.

Boot seeding follows the existing seeder pattern: if the durable copy is missing,
copy the shipped one; never overwrite a newer durable copy (the seeded file
carries `catalog_version`, and the seeder compares before replacing).

## 5. Compatibility, ACLs, and what v1 does not enforce

- **`compat.min_lemonseed`** is the only compatibility check in v1: the portal
  filters catalog entries below the running appliance version, and install refuses
  with a sentence. Broader hardware/version filtering stays backlogged.
- **`network.outbound` is declared but not enforced.** The field is validated for
  shape (so a v2 can rely on it), surfaced in the portal ("this module declares
  outbound connections to…"), and carries a `reason` — but no firewall or proxy
  restriction is applied. Marked not-implemented in the spec so nobody reads its
  presence as a guarantee.

## 6. Platform-config contributions (Plugins only)

The first contribution type is the one P3 needs — a LiteLLM MCP server entry. The
schema is deliberately narrow:

```yaml
platform:
  litellm:
    mcp_server:
      name: <unique across the catalog>
      url: <docker-network URL>
```

- Only `type: plugin` modules may carry a `platform:` block; the merge step
  rejects it on an Application (and Applications get their contribution surface —
  the top-bar entry — through a separate field in a later revision, not by
  overloading this one).
- `litellm.render_config()` takes an extra argument — the list of
  **enabled** plugin contributions — and renders them into `mcp_servers: []`.
  The config fingerprint therefore changes whenever the enabled-module set or a
  contribution changes, which drives the existing recreate path. No OWUI change,
  ever: OWUI's static MCP config points at LiteLLM and stays.
- Contributions from a module that is installed but **disabled** are excluded, so
  disabling a module really does remove its endpoint from the chat path.

## 7. Lifecycle

### State model

Stored in CP Postgres (new `modules` table: `id`, `state`, `version`,
`settings` jsonb, `error`, timestamps). States:

```
   install ──▶ installed ──▶ enable ──▶ enabled
                 ▲                      │
                 │                 disable
                 ▼                      ▼
              removed ◀───────────── installed
```

plus a transient `pending` while an operation runs and `broken` when the
supervisor finds the container repeatedly failing or a hook failed — surfaced in
the portal with the recorded `error` and a retry action.

### Install

1. **Validate against the running box**: spec version, `min_lemonseed`, image pin
   present in `container-images.json`, dependencies satisfiable.
2. **Resolve dependencies topologically** (core services first — Postgres/Valkey
   must be ready before DB provisioning; lemond/litellm before a chat-dependent
   module) and auto-install missing module dependencies first. Cycle = refusal.
3. **Provision the data plane**: declared `data.postgres` gets a role + database +
   password via the existing `_ensure_role_and_db` flow (the `module_db` table
   already carries the mapping); declared `data.valkey` gets a logical DB index.
   URIs and credentials are written into the module env file
   (`/var/lib/lemonseed/data/secrets/modules/<id>.env`) — the existing pattern.
4. **Materialize settings**: declared `settings` defaults are written to the env
   file on first install; user-set values from the portal override defaults and
   are stored in the `modules` row.
5. **Run `hooks/install.sh`** (§8 contract).
6. **Pull the pinned digest, run the container** on `lemon` with the env file,
   volumes and run args; health-wait on the declared port probe (Plugins get a
   TCP/HTTP probe field; a module with no port is "running" once docker says so).
7. State → `enabled` (install implies enable; a "disabled install" is a follow-up
   action, not a second state to keep straight).

### Enable / disable / remove

- **disable**: stop + remove the container, run `hooks/stop.sh`, drop platform
  contributions (§6), keep data, DB and settings. State → `installed`.
- **enable**: run `hooks/start.sh`, re-run the container with current settings,
  re-assert contributions.
- **remove**: refuse while any *installed or enabled* module depends on this one
  (refusal lists the dependents — cascade removal is not offered in v1; explicit
  is safer and the portal can suggest the order). Then: stop + remove container,
  `hooks/remove.sh`, drop platform contributions, and record the removal. What
  happens to the data is one checkbox in the confirm dialog, **Preserve data**
  (default checked): checked keeps the module's data volume and its Postgres
  database (the reinstall path stays lossless — the same principle as
  preserve-reinstall); unchecked purges — drops the database and deletes
  `/var/lib/lemonseed/modules/<id>/`. The unchecked path is the destructive one
  and runs behind the destructive confirm copy (typed confirmation, like the
  OWUI admin reset), so an accidental uninstall can never take the data with it.
- Container recreation rules: a new upstream digest, changed settings, or changed
  run args recreates the container (stop old → run new). **No blue/green for
  modules in v1** — OWUI keeps its blue/green because it is the household's
  always-on UI; a module being down for the seconds a recreate takes is
  acceptable, and blue/green doubles the state space for no v1 benefit.
  Confirmed 2026-11-12: module updates (`docker image pull` of the new digest)
  simply overwrite in place; blue/green or a rollback path for modules is a
  later revision (backlogged). The plan's "blue/green on the existing supervisor
  pattern" reads as "supervised by the existing pattern", not literal
  slot-swapping.

### Hooks

- Location: `modules/<id>/hooks/<event>.sh`, executed **from the image path**,
  by the CP, as root, with a per-hook timeout — `120s` by default, overridable
  once per module through the spec key `hooks.script_timeout` (seconds; the CP
  clamps it to a sane range, 5–600). One number covers every hook event; a
  per-event override stays backlogged until a hook needs it.
- Contract: env — `LEMONSEED_MODULE_ID`, `LEMONSEED_MODULE_DIR` (the module's
  data volume), `LEMONSEED_EVENT`; plus everything already in the module env file
  (DB URIs, credentials, settings). stdin carries the JSON payload — for
  `user.sh`: `{"event": "created|deleted|renamed", "username": …}`; for the
  others: `{"module": …}`. stdout is logged; a **non-zero exit fails the
  operation** and the state records it (install → `broken` with the error, remove
  → `installed` with the error and a retry). No silent swallowing — the AGENTS
  service-boundary rule applies to hooks.
- User-CRUD wiring rides the existing `usermgmt.notify()` observer list, so a
  module never patches account code.
- `run_args` is CP-audited: the merge step rejects `--privileged`,
  `--pid=host`, `--network` anything but `lemon`, and docker.sock mounts — the
  framework's hard rule that modules never reach the Docker daemon.

## 8. Portal

- **Modules menu item → one tab, two groups** (as built; the plan's two-tab
  split was superseded because the rail is already five tabs wide). *Installed*:
  one card per installed module — status (docker's own wording, as today), version,
  error, settings form, enable/disable, remove. *Available*: every catalog entry
  that passes `compat.min_lemonseed`, with logo, description, owners, license,
  maturity badge and an Install button (dependencies are auto-installed, so the
  button rarely disables). The remove confirm dialog carries the **Preserve data**
  checkbox — **checked by default** (§11 Q1: the safe path needs no reading);
  unchecking it summons the typed confirmation (`purge`), and the confirm word is
  issued by the control plane (§8's `confirm_text` shape) so a UI/CP drift cannot
  soften the destructive path.
- **Top-bar entries**: an Application module that declares `app: {menu: …}` in a
  later revision gets a shell menu item to an iframe page. In v1 the only
  Application is Hermes (P6), so the field is specified there; P2 ships the
  one-tab page and the generic install/status plumbing it hangs off.
- Module settings are rendered from the declared schema (`string`, `bool`,
  `int`, `enum`); saving rewrites the env file and recreates the container on the
  next supervision tick.

## 9. Tests

- **Unit** (`control-plane/test_modules_catalog.py`, plus extensions to
  `test_litellm.py` and `test_db.py`):
  - schema validation (strict: unknown keys, wrong types, bad ids, Application
    with a `platform:` block) and merge invariants (dir name = id, unique MCP
    server names, cycles, missing pin keys, missing dependency ids);
  - digest verification (tampered spec refused; missing spec refused);
  - min-version filter for the portal view and the install refusal;
  - dependency resolution (topological order, auto-install chain, cycle refusal,
    remove-with-dependents refusal);
  - lifecycle state machine over mocked docker/subprocess — install → enable →
    disable → remove, hook failure handling, DB/env provisioning, settings → env
    wiring, contribution inclusion/exclusion;
  - `litellm.render_config` gains the contributions argument (rendered into
    `mcp_servers`, fingerprint change drives recreate, disabled module's
    contribution absent).
- **QEMU** (as built: the lifecycle rides hello-world; P3's gateway rides the
  same harness when it lands, adding the LiteLLM-registration assertions):
  install `hello-world` from the preloaded image, assert the container and the
  install hook's marker; disable and assert the teardown (container parked,
  data volume and database retained); remove with **Preserve data** checked and
  assert the retention; reinstall, remove unchecked through the typed confirm
  and assert the database drop and the data volume's deletion. The smoke also
  asserts the admin-only gating and that every action (both remove paths
  included) landed in the audit log.

## 10. Explicitly out of scope (v1)

Hosted/remote catalogs with periodic refresh, Featured and broken-module UX,
multi-container (`docker-compose`) modules, outbound-ACL *enforcement*, broader
hardware/version compatibility filtering, per-module health beyond the supervisor
status (P4 generalizes), and any module that needs GPU time-slicing. All remain in
`BACKLOG.md`.

## 11. Recorded decisions (2026-11-12)

The four open questions were answered by the project owner; the body above
already reflects the answers. This section is the record.

1. **Data at remove time is one checkbox, not two actions.** The remove dialog
   carries a **Preserve data** checkbox (default checked): checked keeps the
   data volume and the Postgres database, unchecked purges both behind the
   destructive confirm copy. (Answer verbatim: "Give the user a checkbox
   'preserve data' when they remove a module; purge if checked, do not purge if
   not checked." The checkbox's label is the direction — checked preserves —
   and the default is the safe one; the sprint report flags the one-wording
   ambiguity for confirmation.)
2. **No blue/green for modules in v1 — confirmed.** Module updates
   (`docker image pull` of the new digest) simply overwrite in place;
   blue/green or rollback is a later revision (backlogged).
3. **Hooks run as root from the image path with a 120s default timeout,
   module-overridable** through the spec parameter `hooks.script_timeout`
   (seconds, CP-clamped to 5–600). The parameter fits the owner's "a module
   spec might include a set of simple parameters" framing; one number covers
   every hook event in v1.
4. **Modules code lives at `/modules` in the repo root** (not under the control
   plane), and that tree is also the master module catalog: the merge step reads
   it and emits the shipped `modules-catalog.json`. Root matches the framework
   doc and keeps module content out of the CP package; the nix packaging needs
   one `cp -r` either way.
