# M9 — Modules framework, LiteLLM, and expanded model offerings

Draft for approval, revised 2026-11-02. **Revised again 2026-11-12**: P5 is
redefined as the **Lemonade 2026.46 engine upgrade** with the original
model-catalog-expansion work absorbed into it (D43), placed ahead of the Hermes
module work. `DESIGN.md` is the as-built product contract;
this document is the **sprint pull** from `BACKLOG.md` (items tagged `[M9]`) and from
M8's close-out. Decisions D32–D44 at the bottom continue the global decision log, so
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
- The appliance runs the **Lemonade 2026.46 engine** with the catalog refreshed
  from the pinned release at build time, and — if the phase budget allows —
  container backends on supported hardware (D43; the container migration carries
  its own decision point to push to M10).
- The AMD dGPU path is validated on bare metal (planner fit arithmetic on VRAM).

## Scope and boundary

| Phase | Title | Design gate | Bare metal |
|-------|-------|-------------|------------|
| P1 | LiteLLM core component (Docker, CP-generated config, friendly models) | **yes — config authority and OWUI repointing** | BM-1 |
| P2 | Module catalog spec v1 + CP lifecycle + hooks | **yes — spec schema and dependency model** | no |
| P3 | First module: MCP gateway (Plugin) | no | (BM-1) |
| P4 | Live API health | no | no |
| P5 | **Lemonade 2026.46 engine upgrade + model catalog refresh** — P5.1 engine bump + API re-baseline; P5.2 HTTP job surface + build-time catalog refresh and registration; P5.3 container backends + native-pin retirement (D43) | **yes — engine pin, catalog registration, container-engine bring-up** | BM-2 |
| P6 | Hermes (Application module) | **yes — dedicated design document** | BM-3 |
| P7 | AMD dGPU bare-metal validation | no | BM-4 |

**Why the upgrade sits here.** The module-framework phases (P2–P4) never touch
the inference core, so building them first does not create rework; P5 is one
coherent cut because the build-time catalog refresh is keyed on the pinned
release (bump the pin → refresh the catalog in the same change), and it keeps
P6 Hermes — the milestone's largest remaining lift — off the engine churn. If
nix-amd-ai carries a settled 2026.46 release before P2's build starts, the
upgrade may jump the queue ahead of P2 instead; both orders keep the same phase
gate. The detail lives in
[`LEMONADE-26.md`](../LEMONADE-26.md); P5 follows it.

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

> **As built (M9 P1):** design `LITELLM.md` + implementation landed; unit tests
> green (`control-plane/test_litellm.py`, plus policy/pin/snapshot fixtures
> updated). Deviations are recorded in `LITELLM.md` §4–§8 as built: the guardrail
> is `litellm_content_filter` + `harmful_child_safety` (the sketched id does not
> exist upstream and would brick the proxy), the master key lives under
> `general_settings.master_key`, the per-model guardrail attach lives inside
> `litellm_params`, the OWUI MCP config write is **deferred to P3** (mechanics
> unvalidated in the spike), the cutover is unconditional (§6), and there is
> deliberately no QEMU image preload — the proxy only starts once a model is
> chosen, which the QEMU profile never has, so chat-through-LiteLLM and the
> filtered-entity probe are BM-1 items. BM-1 review fixes, also as built: the
> OWUI model rows are **override-style** (id = the LiteLLM entity name,
> `base_model_id: null`, observed via paginated `models/list`) after the first
> wrapper implementation rendered every entity twice for admins, and
> `evaluation.arena.enable` is pinned off (its default "Arena Model" placeholder
> joined every picker). The filtered entity is **shipped**: the BM probe blocked a
> prompt through it (`harmful_child_safety`, severity high).

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

> **As built (M9 P2):** spec v1 published in `MODULES-FRAMEWORK.md` and the
> owner's four decisions recorded in `M9/MODULES.md` §11; implementation landed.
> The catalog lives at in-tree `modules/` (`modules_catalog.py` is the reader,
> `scripts/build-modules-catalog.py` the build step; the merged catalog is a
> **build artifact** in the image, seeded at boot into
> `/var/lib/lemonseed/modules/catalog/catalog.json`). The lifecycle is
> `module_manager.py`: one operation at a time, the container disposable with
> the CP as authority, no silent hook failures, and **a pull failure is a
> wait, never a verdict** — the registry is an external condition, so a module
> blocked on it goes WAITING and the supervision tick retries, with BROKEN
> reserved for what the data plane cannot fix. The remove flow carries the
> **Preserve data** checkbox (§11 Q1): unchecking it is a typed confirmation
> (`purge`), checked it keeps the volume and the module database. Deviations,
> as built: **no blue/green** — updates overwrite in place on `run_hash`
> drift (§11 Q2; blue/green and rollback are backlogged); hooks run as root
> with a per-module `hooks.script_timeout` clamped to 5–600 s (default 120,
> §11 Q3); the portal is **one Modules tab** with two groups (Installed /
> Available) rather than the plan's two tabs, because the rail is already
> five tabs and the two-tab split would have crowded it; **the top-bar entry
> for Application modules is not built** (no Application module exists yet);
> and the first module is **hello-world** (`traefik/whoami`, a self-harness in
> `modules/hello-world/test/test.sh`), not the MCP gateway — P3 builds the
> gateway on top of this framework. The QEMU smoke exercises the lifecycle
> with hello-world instead of the gateway: install → enable → disable →
> enable → settings → both remove paths, the admin-only gating, the audit
> records, and the image preloaded by the QEMU profile so the install
> satisfies the data-plane pull's tag fallback and never needs the registry.

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

## P5 — Lemonade 2026.46 engine upgrade + model catalog refresh

*(Redefined 2026-11-12 — the original P5, "Model catalog expansion", is absorbed
into P5.2 below; the upgrade's detail is [`LEMONADE-26.md`](../LEMONADE-26.md),
which this phase follows.)*

### Problem

The appliance runs lemond 11.5.2 (pinned llama.cpp b10273/b10236, July release)
against an upstream now on the date-versioned `v2026.MM.NN` cadence. 2026.46.2
brings the container-backend machinery, a server-owned download/job surface, and
a much richer `/v1/system-info` — the surfaces the catalog work and the planner
need. Meanwhile our hand-maintained catalog has drifted from the pinned release
(Lemonade added models, e.g. Qwen3.8, that we never picked up) and nothing
verifies catalog checkpoints against the pinned release before an image ships.
The engine bump and the catalog refresh are one cut: the refresh derives from
the pinned release, so bumping the pin and refreshing the catalog land together.

### Proposal — three sub-phases

- **P5.1 Engine bump + API re-baseline.** Bump the nix-amd-ai flake pin (or
  vendor the upstream build) to a settled 2026.46 release (46.3 preferred over
  the two-day-old 46.2 once nix-amd-ai carries it); keep the existing native
  llama.cpp builds **unchanged** as the legacy test point. The **native-bridge
  gate**: verify the new lemond still execs our `*_bin` paths verbatim
  (`prefer_system = false`) and does not demand its own `backend_versions.json`
  pins — if it refuses, skip the native-bridge work and jump to P5.3's
  container work instead. Adopt the new `/v1/system-info` in `hardware.py`
  (read `devices.amd_gpu`, plan on `virtual_mem_gb`, read
  `recipes.*.backends[].state`, delete the llama-cli/vulkaninfo fallback
  probes); audit every `/v1/models` consumer for the
  `image_defaults`/`audio_defaults` → `recipe_options` break; refresh the
  `lemonade-python-sdk` pin; lemond config review (`LEMONADE_DEFAULTS_PATH`,
  `LEMONADE_ADMIN_API_KEY`, `no_fetch_executables` semantics, telemetry off,
  `withWebApp=false` still excludes `/app`).
- **P5.2 HTTP job surface + build-time catalog refresh.** The CP moves pull/load
  to `POST /v1/pull` + `GET /v1/downloads` (+ `POST /v1/downloads/control` for
  cancel — killing a `Popen` dies), the portal's "Getting Ready" interstitial
  and the power interlock (`in_flight_work()`) read real job progress, and the
  curated catalog **registers itself** through `POST /v1/models/register` as
  `user.*` definitions with named checkpoints. The original P5 work rides
  here: the build derives catalog entries from the pinned release's
  `server_models.json` (which this release refactored to list every model file
  — name drift is a live risk that registration removes), every `checkpoint`
  is verified at build time, custom models keep the direct HF checkpoint path,
  and the `moe` attribute stays deferred. LiteLLM reduces display-name
  pressure. `/internal/aliases` is the optional native home for the
  `Lemonseed AI` naming layer below LiteLLM.
- **P5.3 Container backends + native-pin retirement.** lemond's container
  machinery (rootless Podman default, Docker fallback) is the destination for
  inference: upstream refreshes the engine images, so new Strix Halo engines
  (nathanw, halobox, rocmfpx, gufo, ds4:rocm, halogen) arrive as *installable
  backends* instead of a Nix packaging battle. Bring-up: Docker via the
  existing daemon (docker-group for `lemond` — root-equivalent risk accepted,
  D43), a hardening carve-out in `lemond.nix` scoped to the socket, a
  **digest-pin allowlist of our own** (upstream refreshes image digests between
  releases; track them alongside `container-images.json`), and the
  no-pull-on-the-boot-path invariant preserved by pre-pulling images at
  install/update/first-selection. **The CP never enumerates hardware**: the
  backend offer is whatever `recipes.llamacpp.backends[]` reports as
  `installed`/`installable`, filtered only by a derived allowlist (backends
  whose device families lemond reports — never hardcoded to `gfx1151`), a
  core > guest tier preference, and the container-tool preflight. Native CUDA
  moves to lemond's own per-arch core binary (installed off the critical path
  via `POST /v1/install`); the Nix-built CUDA binary becomes the offline
  fallback/test point. Native Vulkan drops from the production image once
  Strix Halo is container-served and no AMD dGPU path depends on it; the
  native CPU build stays for QEMU.
- **Decision point (P5.3):** if the container migration balloons beyond the
  milestone's budget, it moves to M10 with the design note landing in
  `LEMONADE-26.md` — P5.1 and P5.2 do not depend on it, and neither does P6.

### Tests

- Unit: system-info parsing (AMD devices + backend states, no double counting
  with the deleted probes), the `/v1/models` `recipe_options` break, the
  catalog refresh (entries for every upstream model, vanished ones dropped,
  our additions survive, the checkpoint check fails the build on a missing
  checkpoint), registration round-trip (`user.*` ids, named checkpoints),
  download-job progress/cancel plumbing, and P5.3's allowlist derivation
  (family-derived, tier-preferred, container-tool preflight).
- QEMU: the engine bump keeps `make test-qemu` green end to end; a model
  switch mid-pull cancels cleanly through `downloads/control`; the refreshed
  catalog loads from registered definitions; P5.3 adds container bring-up on
  the QEMU CPU profile only if the profile adopts containers (otherwise QEMU
  stays on the native CPU backend).
- **BM-2:** the Strix box validated on the new engine — pull → load → verify →
  `/api/system` healthy, the planner's `virtual_mem_gb` mapping correct, and
  (P5.3) at least two container engines loaded, one unload leaving no orphan
  containers.

### Gate

`make test` + QEMU green through the new engine; the refreshed catalog diff
reviewed against the hand-written one; BM-2 pass. P5.3 carries the M10 decision
point.

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
BM-3: both users on real hardware, containers bounded.

### Gate

Design document approved → implementation → QEMU green → BM-3 pass. Or the explicit
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

Documented BM run; hardware-support matrix updated. *(This phase's bare-metal
checkpoint is BM-4.)*

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
| **D43** | **The Lemonade engine is upgraded to 2026.46 within M9 (P5), ahead of the Hermes module work.** Container backends are the destination for inference; the native llama.cpp pin is a legacy test point, not the production path (no re-pin to the upstream build numbers unless containers are blocked). The container engine is **Docker via docker.sock for the `lemond` account** — root-equivalent risk accepted (Docker is already the module platform; rootless Podman is the fallback, with the runc→CPU trap). **The control plane never enumerates hardware**: lemond's `/v1/system-info` decides, and the CP adds only policy on top — an allowlist derived from the device families lemond reports (never hardcoded to one family), a core > guest tier preference, and the container-tool preflight. NVIDIA has no CUDA container upstream (NVIDIA container images are spec'd future work), so the NVIDIA path is lemond's own **native** per-arch CUDA binary (installed off the critical path via `POST /v1/install`); the Nix-built CUDA binary is only an offline fallback/test point. Detail in [`LEMONADE-26.md`](../LEMONADE-26.md). |
| **D44** | **Model catalog maintenance is build-time *and* registration-based.** The catalog refresh derives entries from the pinned release's `server_models.json` and the CP **registers** them through `POST /v1/models/register` as `user.*` definitions with named checkpoints, so our offering no longer depends on upstream's built-in catalog still carrying our exact names. (Extends D41; D41's checkpoint check verifies the refresh at build time, and custom models keep the direct HF checkpoint path.) |

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
5. ~~Which 2026.46 release to pin~~ — **answered:** a settled 2026.46.2/46.3
   once nix-amd-ai carries it; if nix-amd-ai lags, vendor the upstream source
   build in-tree (the open question in DESIGN §12.8). The upgrade never
   re-pins the native llama.cpp builds (D43's legacy-test-point stance).
6. **Does `no_fetch_executables = true` block container image pulls?** Must be
   tested explicitly in P5.1 — the flag's semantics are load-bearing for the
   appliance's no-downloads-on-the-boot-path invariant.
