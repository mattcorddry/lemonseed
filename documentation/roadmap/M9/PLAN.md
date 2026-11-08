# M9 — Modules framework, LiteLLM, and expanded model offerings

Draft for approval, revised 2026-11-02. `DESIGN.md` is the as-built product contract;
this document is the **sprint pull** from `BACKLOG.md` (items tagged `[M9]`) and from
M8's close-out. Decisions D32–D42 at the bottom continue the global decision log, so
nothing gets re-litigated mid-build.

M8 closed with P1–P7 shipped and bare-metal verified: signed GitHub releases with
channels, encrypted user-data with TPM-sealed keys and console lockdown, N-1 rollback,
and the production runbook exercised. M9's job is the first **extensibility** milestone:
turn the appliance from a fixed product into a platform — a **module catalog** with a
declared spec, install/lifecycle/tooling, and two real modules — and put **LiteLLM** in
as core infrastructure so models get friendly names, content filtering gets a home, MCP
tools get one gateway, and other LAN apps get an OpenAI-compatible endpoint.

**Definition of done for this milestone:**

- A module installs from the **catalog** (portal, two tabs: Catalog / Installed),
  declares its dependencies and platform contributions, and is lifecycle-managed
  (enable/disable/remove, blue/green, status) — proven by **two shipped modules**:
  the MCP gateway (Plugin) and Hermes (Application).
- Chat flows through **LiteLLM** under friendly, manifest-owned model names; OWUI
  consumes tools through **one MCP endpoint** with a static config, and adding a
  module never changes OWUI's configuration.
- Health is **live**: Postgres, Valkey, lemond, LiteLLM and every module flip the
  status fields when they stop serving.
- The AMD dGPU path is validated on bare metal (planner fit arithmetic on VRAM).

## Scope and boundary

| Phase | Title | Design gate | Bare metal |
|-------|-------|-------------|------------|
| P1 | LiteLLM core component (Docker, CP-generated config, friendly models) | **yes — config authority and OWUI repointing** | BM-1 |
| P2 | Module catalog spec v1 + CP lifecycle + hooks | **yes — spec schema and dependency model** | no |
| P3 | First module: MCP gateway (Plugin) | no | (BM-1) |
| P4 | Live API health | no | no |
| P5 | Model catalog expansion (build-time catalog refresh, checkpoint check, custom models) | no | no |
| P6 | Hermes (Application module) | **yes — dedicated design document** | BM-2 |
| P7 | AMD dGPU bare-metal validation | no | BM-3 |

**Phase gate (M8's, unchanged).** A phase ends when `make test` **and**
`QEMU_API_TIMEOUT=… make test-qemu` are both green, its new tests exist, and its
obsolete tests are deleted. No phase starts before the previous one passes. Bare-metal
checkpoints are yours; each is flagged.

**Explicitly deferred (not in this milestone):** hosted/remote module catalogs with
periodic refresh (v1 ships the in-tree catalog in the image), outbound-ACL
*enforcement* (declared in the spec, not enforced), Featured and broken-module UX,
GeoIP model preference, network throughput meter, group attributes beyond
`content_filter`, local Wikipedia cache, ComfyUI, the web-UI visual redesign, Secure
Boot/Lanzaboote, and the swtpm installer test harness. All remain in `BACKLOG.md`.

---

## P1 — LiteLLM core component

### Problem

OWUI talks to lemond's single-model configuration, so model names are whatever lemond
loads, content filtering has no home, tools have no aggregation point, and other LAN
apps cannot use the appliance as an OpenAI-compatible endpoint. Three backlog items
(friendly model names, content filtering, the endpoint toggle) all need the same thing:
an LLM gateway that fronts lemond under names the product owns.

### Proposal

LiteLLM proxy becomes a **core platform component** — alongside lemond, Postgres and
Valkey — not a module:

- **Packaging:** Docker, image pinned to an explicit upstream stable tag in-tree, on
  the `lemon` network, published `127.0.0.1`-only behind the gateway like every other
  service. It reaches lemond via `host.docker.internal` (the established convention).
- **Config authority:** the control plane generates `config.yaml` into
  `/var/lib/lemonseed/litellm/` and runs the container with it mounted — **no
  database, no portal UI**. The model list is derived from the **release manifest's
  model entities** (`lemonseed-ai`, `lemonseed-ai-filtered` are already reserved in
  `owui-policy.json`), so the offering stays release-defined.
- **OWUI repointing:** the CP-owned `openai.api_base_urls` moves to LiteLLM, and the
  env-era `DEFAULT_MODEL_METADATA`/`DEFAULT_MODEL_PARAMS` bootstrap retires.
- **Two friendly entities, one backend:** OWUI sees **Lemonseed AI** and **Lemonseed
  AI with Content Filters**, both pointing at the active lemond LLM. The filtered
  variant is the same model wrapped in LiteLLM's built-in
  `cf_harmful_child_safety` **guardrail set** — a config-only difference. Which
  users see which model is mapped by **OWUI's per-user model configuration**, so
  the enforcement point stays in OWUI's existing group/user machinery.
- **OWUI's MCP config is static from day one**: it points at LiteLLM's MCP endpoint,
  and modules register there — adding a module never changes OWUI's configuration.
- **Health:** LiteLLM joins the status fields (P4 makes those live).

### Tests

- Unit: config generation is deterministic and covered — model list from the
  manifest, both friendly entities (the filtered one carrying the guardrail set),
  MCP entries from installed modules, ownership of every emitted key.
- QEMU: chat works through LiteLLM under a friendly name; OWUI's model list shows
  both entities; lemond's native name does not leak; the filtered entity refuses
  a probe of disallowed content (or at minimum asserts the guardrail is wired).
- BM-1: the same on real hardware, plus model switching in the panel and the
  per-user model mapping.

### Gate

Config authority settled (this document + `DESIGN.md` §3.4/§3.5 updates), QEMU green,
BM-1 pass.

---

## P2 — Module catalog spec v1 + CP lifecycle

### Problem

`MODULES-FRAMEWORK.md` defines the intent (types, spec, catalog, lifecycle) but nothing
general exists: OWUI is special-cased in `modules.py`, and there is no catalog, no
install lifecycle, no dependency model, no hooks. A framework designed only as
"hello-world" would be hypothetical; P3 and P6 make it real.

### Proposal

- **Catalog spec v1** (`MODULES-FRAMEWORK.md` requirements 1–15, settled): one
  revision-controlled YAML file per module in an in-tree `modules/` catalog directory,
  merged by a build step with integrity checks. Fields: unique ID, name, license,
  maturity, version, owners (GitHub IDs), logo/description/homepage, type
  (Application/Plugin), spec version, dependencies (core offerings, other modules, AI
  capabilities), Docker service fragment, host-side script hooks
  (install/remove/start/stop/user-CRUD), Postgres/Valkey schema declarations, outbound
  connection declarations, and a no-external-dependency test harness.
- **Distribution:** the built catalog ships **in the image** and is seeded at boot into
  `/var/lib/lemonseed/modules/catalog/` — the runtime path is stable for the future
  hosted-catalog refresh, which is backlogged.
- **Platform config contributions:** new requirement — *Plugin modules may declare a
  contribution to a platform config file, currently the LiteLLM MCP server entry.* A
  YAML schema for contributions, with **outbound ACLs declared but marked
  not-implemented**.
- **Compatibility:** a **minimum-version check** in v1; broader hardware/version
  filtering is backlogged.
- **CP lifecycle:** install / enable / disable / remove with dependency resolution
  (core offerings, other modules — auto-install on install, dependency checks on
  remove), blue/green on the existing supervisor pattern, script hooks executed
  host-side, settings passed as environment variables (requirement 6), and the
  Postgres/Valkey schema provisioning flow (URIs + credentials via env).
- **Portal:** the Modules menu item becomes two tabs — **Catalog** and **Installed** —
  with install/remove/status. Per-module status and settings move to a `/settings`
  element per module (exact placement settled in implementation). Application modules
  with a web UI may add a **top-bar entry** (short name + icon) so Hermes contributes
  "Actions" without a portal build.

### Tests

- Unit: catalog parsing and merge (integrity checks, spec-version handling,
  min-version compatibility), dependency resolution and ordering, lifecycle
  state machine (install → enable → disable → remove, dependency refusal on remove),
  hook execution and failure handling, config-contribution schema validation,
  settings-as-env wiring.
- QEMU: install the MCP gateway module from the catalog, assert the container, the
  LiteLLM registration, and health; remove it and assert the teardown.

### Gate

Catalog spec v1 published in `MODULES-FRAMEWORK.md` (open questions settled there),
QEMU install/remove green, unit coverage for parsing and lifecycle.

---

## P3 — First module: MCP gateway (Plugin)

### Problem

The framework needs a real module, and the platform needs a tools story: OWUI and
other apps should reach MCP tools through **one endpoint**, with modules registering
themselves rather than each app's config changing.

### Proposal

A catalog entry for the MCP gateway (`docker/mcp-gateway`) as a **Plugin** module,
with the v1 service set **fetch, duckduckgo, memory, time**:

- The module declares a **LiteLLM MCP registration** as its platform config
  contribution; LiteLLM aggregates it behind its MCP endpoint.
- OWUI's MCP config is **static** — it points at LiteLLM and never changes when
  modules come and go (D34).
- Uninstall removes the registration and closes the contribution.

### Tests

- QEMU: install → the gateway's tools are reachable through LiteLLM under stable
  names → uninstall → the registration disappears.
- Unit: the catalog entry validates against the spec; the contribution schema
  round-trips.

### Gate

QEMU green; a tool call exercised through OWUI on BM-1.

---

## P4 — Live API health

### Problem

Status fields report "daemon started once" — a stopped Postgres or a wedged OWUI
still reads healthy, so the portal cannot explain why chat is broken.

### Proposal

Cheap live checks on the supervisor tick: `pg_isready` / `SELECT 1` for Postgres,
`PING` for Valkey, HTTP-ready for lemond, LiteLLM and every module. A service that
stops serving flips its status to unhealthy (with the last error and a timestamp);
recovery flips it back. The interstitial and the status panel surface the same state.

### Tests

Unit: the check wrappers against stubbed commands; QEMU: stop a data service inside
the guest and assert the status flips and recovers.

### Gate

QEMU green; `/api/data` and `/api/modules` never claim healthy while a live check
fails.

---

## P5 — Model catalog expansion

### Problem

Our model catalog has drifted from the pinned Lemonade release — Lemonade added new
models (e.g. Qwen3.8) that we never picked up, because the catalog is hand-maintained.
And nothing verifies that catalog checkpoints exist in the pinned Lemonade release
before an image ships.

### Proposal

- **Build-time catalog refresh:** the build derives our catalog entries from the
  pinned Lemonade release's `server_models.json`, so new upstream models are picked
  up by a rebuild instead of by hand. Our catalog carries only what we add on top
  (friendly names where they matter, availability, descriptions).
- **Build-time checkpoint check:** every catalog `checkpoint` must exist in the
  pinned `server_models.json` (the disk-space guard's directory matching once broke
  on a case disagreement).
- **Custom models:** models we offer that Lemonade does not — direct HF checkpoint
  path + `lemonade load`, plus the llama.cpp arch-support caveat. LiteLLM reduces
  the display-name pressure here: what OWUI shows comes from LiteLLM's config, not
  from Lemonade-side naming.
- **Deferred: the `moe` attribute.** Hand-maintaining a per-model MoE flag will
  rot; it returns to the backlog unless Lemonade (or the models themselves) expose
  a queryable attribute we can derive it from.

### Tests

Unit: the catalog refresh produces entries for every upstream model (and drops
vanished ones); the checkpoint check fails the build on a missing checkpoint; our
additions survive the refresh. The refresh is wired into the catalog build so a
Lemonade pin bump picks up new models without a manual pass.

### Gate

`make test` green, the build-time refresh + checkpoint check wired into the catalog
build, and a diff review of the refreshed catalog against the current hand-written
one.

---

## P6 — Hermes (Application module)

### Problem

The first **Application** module proves the web-app path (nginx placement, top-bar
entry, per-user state) — and Hermes brings the framework's hardest extension:
**user-scoped, multi-instance modules**. Hermes' multi-user support is thin, so the
appliance provisions one container per user, always-on by design, launched on demand
or enabled per user/group.

### Proposal

A **dedicated design document** (`roadmap/M9/HERMES.md`) covering: container-per-user
lifecycle and resource gating, demand-launch vs **opt-in per user** policy, the
user-CRUD hook contract (container teardown on delete), nginx routing and auth for
per-user panels, and storage/secret access. It must also evaluate the **web-UI
choice**: Hermes 0.23 shipped a new WebApp that is a large improvement over their
legacy web UI — evaluate Hermes' own WebApp vs using OWUI as the Hermes interface
(per the upstream [Open WebUI integration
guide](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/open-webui)).
This decision drives the D39 top-bar entry design, so it comes first in the document.
Implementation follows the approved design; standalone Hermes testing on the dev box
feeds the evaluation.

**Decision point:** if the design shows the multi-instance framework extension is
larger than the milestone can absorb, Hermes moves to M10 with the design note
landing here — the module framework (P2) does not depend on it.

### Tests

Unit: policy decisions (launch on sign-in, opt-in gating, teardown on user delete).
QEMU: two users, isolated panels, teardown.
BM-2: both users on real hardware, containers bounded.

### Gate

Design document approved → implementation → QEMU green → BM-2 pass. Or the explicit
push to M10 with the design note landed.

---

## P7 — AMD dGPU bare-metal validation

### Problem

No discrete AMD card has ever booted on the appliance: the Vulkan backend,
`select_backend` dispatch and `hardware.py` AMD discovery handle a PCIe Radeon, but
only Strix Halo APUs are validated. The paths differ semantically — an APU's model
budget comes from the `ttm.pages_limit` carve-out of system RAM, a dGPU's from its own
VRAM — so the planner's fit-versus-memory arithmetic is a different path, not the same
one with different numbers.

### Proposal

One tier-D bare-metal run on the RX 7600 (Navi 33, `1002:7480`) already identified on
the dev host: install from the current release, confirm discovery and backend
selection, run the model planner against real VRAM, exercise chat, and record the
planner decisions. Findings feed `DESIGN.md` §9 and the hardware-support matrix.

### Tests

The standard BM drill plus planner assertions on the dGPU box; no new code expected
unless discovery or fit arithmetic surfaces a defect.

### Gate

Documented BM run; hardware-support matrix updated.

---

## Bookkeeping (rides the phases, not its own phase)

- `MODULES-FRAMEWORK.md` graduates from proposal to **spec of record**: open questions
  settled (LLM catalog is a separate system, outbound ACLs declared-not-enforced),
  the platform-config-contribution requirement added, and the catalog distribution
  model (in-tree, seeded at boot) written down.
- `DESIGN.md`: §3.4 rewritten around the general module lifecycle (OWUI becomes one
  instance of it, LiteLLM joins the core services), §3.5 gains the static MCP config,
  §10 the new catalog tree, and §13 the decision log rows below.
- Docs mentioning "**OWUI is a module**" are reworded — OWUI is a core component, and
  the framework is for everything beyond it.
- `BACKLOG.md`: `[M9]` items leave as they land; Hermes' multi-instance framework
  extension lands as a design note; the swtpm installer harness and Lanzaboote remain
  `[BL]`.

---

## Decisions

| | Decision |
|---|---|
| **D32** | LiteLLM is a **core platform component**, packaged in Docker with a pinned image, configured exclusively by a CP-generated `config.yaml` under `/var/lib/lemonseed/litellm/` — no database, no portal UI. |
| **D33** | Friendly model names are **manifest-owned** model entities served by LiteLLM; OWUI's OpenAI connection is repointed to LiteLLM and the env-era model bootstrap retires. Two entities ship from day one — **Lemonseed AI** and **Lemonseed AI with Content Filters**, the latter being the same active lemond LLM wrapped in LiteLLM's built-in `cf_harmful_child_safety` guardrail set. Per-user visibility maps through **OWUI's per-user model configuration**. |
| **D34** | **All MCP clients consume via LiteLLM.** No direct OWUI↔module coupling: OWUI's MCP config is static, and modules register themselves through platform config contributions. |
| **D35** | The module catalog ships **in the image** and is seeded at boot into `/var/lib/lemonseed/modules/catalog/` — the runtime path is stable for the future hosted-catalog refresh (backlogged). |
| **D36** | Modules may declare **platform config contributions** through a versioned YAML schema (LiteLLM MCP registration first). **Outbound ACLs are declared but not enforced** in v1; enforcement is backlogged. |
| **D37** | Catalog compatibility is a **minimum-version check** in v1; broader hardware/version filtering is backlogged. |
| **D38** | OWUI remains a **core component** and is not migrated into the module framework; documentation stops calling it a module. The `Application` module type is proven by Hermes. |
| **D39** | The Modules surface is **two tabs (Catalog / Installed)**; Application modules with a web UI may add a top-bar entry (short name + icon); per-module status and settings live in a `/settings` element per module — exact placement settled in implementation. |
| **D40** | Hermes is an **Application module** with a dedicated design document (user-scoped, multi-instance containers; demand-launch vs opt-in policy; user-CRUD teardown; OWUI "Actions" entry). Implementation in M9 with a decision point to push to M10 if the design balloons. |
| **D41** | Model catalog maintenance is **build-time**: the catalog is refreshed from the pinned Lemonade release's `server_models.json` (new upstream models are picked up by a rebuild), every `checkpoint` is verified against that file, and custom models use the direct HF checkpoint path. Display-name pressure is reduced by LiteLLM (what OWUI shows comes from LiteLLM's config). The `moe` attribute is **deferred** until it can be derived from a queryable upstream attribute. |
| **D42** | The AMD dGPU bare-metal validation (RX 7600, tier-D) is an M9 deliverable: planner fit arithmetic on VRAM is a different path from the APU carve-out. |

## Open questions

1. ~~LiteLLM version pin and MCP maturity~~ — **answered:** detect and pin the
   **latest stable** LiteLLM release at build time (the same pin-at-build policy as
   every other component); its MCP proxy is stable enough to build the P3
   registration on.
2. ~~MCP service set for the gateway~~ — **answered:** `fetch`, `duckduckgo`,
   `memory`, `time` for v1.
3. ~~Filtered-model rollout~~ — **answered:** both entities ship with the model list
   in P1; the filtered one is the same active lemond LLM plus the
   `cf_harmful_child_safety` guardrail set, mapped per user in OWUI.
4. **Hermes launch policy default** — opt-in per user is the working answer;
   settled finally in the P6 design document (alongside the Hermes 0.23 WebApp vs
   OWUI evaluation).
