# Lemonade 2026.46 — engine upgrade and container-backend migration plan

Status: **plan** — nothing here has landed. Not yet a milestone record; when the
work ships, fold the outcomes back into [`ARCHITECTURE.md`](../ARCHITECTURE.md) (§3.6 model
management, §3.4, §8 llama.cpp provenance, §13 decision log).

| | |
|---|---|
| Target upstream release | **Lemonade v2026.46.2** (2026-11-11) |
| Current appliance engine | **lemond 11.5.2** (verified on lemonseed1, appliance 20261108.07, Gemma-4-31B on Strix Halo / Vulkan) |
| Current pin | nix-amd-ai flake rev from 2026-08-10; llama.cpp b10273 (CPU/Vulkan) / b10236 (CUDA, own source build) |
| nix-amd-ai upstream | already at Lemonade **2026.45.3** with llama.cpp b11382; 46.2 not yet present there |

Upstream changed its versioning scheme mid-2026 (`11.x` → `v2026.MM.NN`,
roughly weekly). 46.2 shipped two days after its three `candidate-` tags, so the
first cut of this plan should prefer **46.3** or a settled 46.2 once nix-amd-ai
carries it (or vendor the upstream source build in-tree per the open question in
DESIGN §12.8).

---

## 0. Direction — decisions this plan commits to

1. **Container backends are the destination for inference on supported
   hardware.** lemond's own hardware detection and backend-loading machinery
   (`/v1/system-info` → `recipes.<r>.backends{state, action, setup_checks}` →
   `POST /v1/install` → digest-pinned image per loaded model) is how new engines
   reach the appliance going forward: upstream CI already refreshes the engine
   images, so a new Strix Halo optimization fork arrives as an *installable
   backend on our box* instead of a multi-day Nix packaging and pin-validation
   battle. This gives much broader out-of-the-box compatibility than we can
   ever achieve by chasing llama.cpp ourselves.
2. **The native llama.cpp pin is a legacy test point, not the production
   path.** The b10273/b10236 pin discipline took significant back-and-forth
   testing to get right (GGUF architecture drift, multi-slot server bugs, the
   RDNA3.5 numerics fix) and is expected to stay brittle. Do not re-pin it to
   b11846/b11836 unless containers are blocked (the P1 gate below). Once P3
   lands, the native builds exist only for the QEMU CPU profile and as a
   reference point for engine comparisons — and the Nix-built CUDA binary
   becomes a fallback, because upstream already ships **core, native, per-arch
   CUDA binaries with the CUDA runtime bundled** (sm_75–sm_121, no system CUDA
   toolkit on the host); the control plane should let lemond install and own
   that binary rather than maintain its own (P2/P4).
3. **Engine for container backends: Docker, via the existing daemon.** The
   appliance already runs Docker for modules (OWUI, Postgres, Valkey, LiteLLM)
   and we accept giving the `lemond` user access to `docker.sock`. Stated
   plainly: this gives the `lemond` account root-equivalent container control of
   the *whole* host container surface, including the data-plane containers — an
   accepted risk. Rootless Podman remains the documented fallback if Docker
   semantics fail (and note upstream's trap: rootless Podman under **runc**
   silently runs containers on the CPU; `crun` is mandatory).
4. **No hardware enumeration in the control plane.** The appliance never
   hardcodes a device family or picks a recipe itself: lemond's
   `/v1/system-info` is the detector (`devices.*` for what exists,
   `recipes.<r>.backends[].state` for what can run). The CP contributes only
   *policy on top of lemond's answer* — an allowlist of backends it will
   permit (derived from the device families lemond reports, never hardcoded to
   one), tier preferences (core > guest), and the container-tool preflight. If
   lemond says a backend is `installable`, the CP's job is to make the
   prerequisites true, not to reason about the GPU.
5. **Scope guard:** the Lemonade web app (`withWebApp = false` stays) and the
   desktop app remain out of scope for this plan — exposure of the new Lemonade
   App UI is a separate future decision. No changes to the OWUI chat surface,
   the LiteLLM gateway, or the no-cloud-dependency stance ride on this work.

---

## 1. Why this release, and what is in it

Lemonade 2026.46.2 headline items that matter to Lemonseed:

- **Container backends** via a ContainerManager — `ds4:rocm` (speculative
  decoding), `llamacpp:nathanw` (Vulkan performance fork), `llamacpp:halobox`
  (ROCm fork), `rocmfpx:rocm`, `gufo:rocm`, and `halogen:rocm` (Flash; needs
  Linux ≥ 6.18.4 with `CONFIG_HSA_AMD_SVM`). Five of the six are exactly our
  flagship Strix Halo (`gfx1151`). Each loaded model gets its own hardened
  container: model files mounted read-only under `/mnt/models`, only the GPU
  device nodes the backend needs, capabilities dropped + `no-new-privileges`,
  its own `--internal` network; unloading stops it. **Breaking:** the native
  DS4 backend was replaced by its container.
- **A server-owned download/job surface** — `POST /v1/pull` (JSON / SSE / job),
  `GET /v1/downloads` + `POST /v1/downloads/control` (pause/cancel/remove), the
  Job Engine (`/v1/jobs`), `POST /v1/models/register` (user.* models with named
  checkpoints), `GET /v1/pull/variants` + `/v1/registry/search`, and
  `/internal/aliases` (symbolic model routing).
- **A much richer `/v1/system-info`** — `devices.amd_gpu` natively (with
  `family`, `vram_gb`, and `virtual_mem_gb`, which is our `ttm.pages_limit`
  carve-out reported by the server itself), plus a `recipes` block where every
  backend carries `state` (`installed`/`installable`/`update_required`/
  `action_required`/`unsupported`), `tier`, `format` (`native`/`python`/
  `container`), `message`, `action`, and `setup_checks[]`.
- **Breaking API changes** to audit: `image_defaults`/`audio_defaults` left the
  `/v1/models` response (now nested under `recipe_options`); a new
  `user_configurable` flag marks user-facing backend knobs; FLM v1.0.11
  behavior changes (NPU — not on our current hardware, note for later).
- New config keys worth adopting or pinning: `allowed_origins`,
  `LEMONADE_ADMIN_API_KEY` (secures `/internal/*`), `default_model_source`,
  `model_catalogs`, `auto_evict*`, `offline`, `config_version 2`. Our standing
  pins (`max_loaded_models = 1`, `auto_check_model_updates = false`,
  `no_broadcast`, telemetry off) remain coherent.

---

## 2. What the control plane touches today (verified)

| Consumer | Today | What changes |
|---|---|---|
| `hardware.py` | `/v1/system-info` GPU inventory + own llama-cli/vulkaninfo AMD probes, de-duped by name | Adopt `devices.amd_gpu`; delete the probes; map `virtual_mem_gb` into the planner budget |
| `catalog.py` `select_backend()` | vendor-derived `cuda`/`vulkan`/`cpu` | Backend id becomes first-class and may be a container id (`llamacpp:nathanw`, …) |
| `chat_ready.py` | `lemonade pull` / `lemonade load` CLI subprocesses; `Popen` bookkeeping for cancel | HTTP jobs: `/v1/pull` + `/v1/downloads` |
| `model_store.py` | lemond storage meters + LRU eviction via API | Unchanged shape; still CP-owned policy |
| `lemond_client.py` | SDK 1.0.10 subclass | Expect an SDK refresh; re-verify `/v1/health` / `/v1/models` field drift |
| `lemond.nix` | `LEMONADE_DEFAULTS_PATH` + boot-time jq re-apply; three Nix-built `*_bin` backends | Config keys review; `*_bin` bridge behavior must be re-verified on 46.2 |

---

## 3. Execution plan

### P1 — Engine bump and API re-baseline (no native llama.cpp re-pin)

The mechanical, must-do cut that makes every later phase possible. **It does
not touch the llama.cpp pin.**

1. Bump the nix-amd-ai flake pin (or vendor the build) to the chosen Lemonade
   release; keep `withWebApp = false; withDesktopApp = false`.
2. **Keep the existing native llama.cpp builds unchanged** (b10273/b10236). Do
   not chase b11846/b11836. The native `*_bin` wiring stays exactly as it is.
3. **Native-bridge gate (the "just works" test):** verify on the Strix box and
   in QEMU that 46.2's lemond still execs our `*_bin` paths verbatim
   (`prefer_system = false`) and does not *demand* the versions its own
   `backend_versions.json` pins. If it accepts them → P1 continues with the
   legacy builds as the bridge. If it refuses or misbehaves → **skip the rest
   of the native bridge work and jump straight to P3's container work**, since
   that is the destination anyway; do not spend the multi-day re-pin exercise
   on a path we are replacing.
4. Adopt the new `/v1/system-info` in `hardware.py`: read `devices.amd_gpu`
   (plan on `virtual_mem_gb`, not `vram_gb`), read `recipes.*.backends[].state`,
   delete `_amd_gpus_llama_cli()` / `_amd_gpus_vulkaninfo()` and their
   name-based de-dupe. Guard against double counting during the transition.
5. Audit every `/v1/models` consumer for the `image_defaults`/`audio_defaults`
   → `recipe_options` break; refresh the `lemonade-python-sdk` pin (or widen
   the hand-written client) and re-derive the SDK-subclassed endpoints.
6. lemond config review in `lemond.nix`: confirm `LEMONADE_DEFAULTS_PATH` +
   `defaults.json` is still the override mechanism in 46.2; set
   `LEMONADE_ADMIN_API_KEY` alongside `LEMONADE_API_KEY`; confirm
   `no_fetch_executables = true` does **not** block container image pulls
   (explicit test — this flag's semantics are load-bearing for our
   no-downloads policy); keep telemetry off; confirm `withWebApp=false` still
   excludes serving `/app`.
7. Acceptance: Strix Halo validated end-to-end (pull → load → verify →
   `/api/system` healthy), QEMU CPU profile green, `make test-qemu` passes with
   the new lemond, `/api/system` still reports the carve-out budget correctly.

### P2 — Model lifecycle on the HTTP job surface

Replaces the subprocess pattern with the release's own machinery. Biggest UX
and code-quality win of the plan.

1. **Register the catalog instead of depending on its names.** The curated
   catalog (`catalog_version` 8) registers each model through
   `POST /v1/models/register` as `user.*` definitions with named `checkpoints`.
   Upstream refactored `server_models.json` in this release, so name drift is a
   live risk today and a solved problem with registration.
2. Move pull to `POST /v1/pull` and read progress from `GET /v1/downloads`;
   model switches cancel via `POST /v1/downloads/control` instead of killing a
   `Popen`. Delete `cancel_active_pull`'s subprocess bookkeeping.
3. Feed the portal's "Getting Ready" interstitial and the power interlock
   (`in_flight_work()`) from the job surface (real percentages, pause where it
   makes sense).
4. Optional, low-cost: `/internal/aliases` as the native home for the friendly
   `Lemonseed AI` naming layer below LiteLLM; `/v1/pull/variants` +
   `/v1/registry/search` reserved for a future power-user "browse" surface.
5. Acceptance: a model switch mid-pull cancels cleanly with no truncated
   weights file; reboot mid-pull still lands in the interstitial; the
   catalog's 12 models all load from CP-registered definitions.

### P3 — Container backends (the destination)

Own milestone; expect the beta channel to carry it first.

1. **Engine bring-up.** Install `podman` + `crun` in the SquashFS *or* wire
   Docker (chosen): add the `docker` group to the `lemonseed-lemond` user and
   set `CONTAINER_HOST` in the lemond unit. Confirm lemond's engine discovery
   picks Docker when Podman is absent. Hardening carve-out in `lemond.nix`
   scoped to the container path (socket access; revisit `DevicePolicy` /
   `ProtectSystem` only where the socket demands it).
2. **Digest-pin policy of our own.** Upstream refreshes container image digests
   between releases, so the appliance needs an explicit allowlist: which
   backends and image digests we permit, per detected device `family`. Track
   them alongside `container-images.json` (the same digest-pinning discipline
   the data plane already uses) and re-pin on each Lemonade release.
3. **Preserve the no-pull-on-the-boot-path invariant.** Container images are
   pulled at model-load time, never at boot. Pre-pull the pinned images at
   install/update/first-selection (the same moment the GGUF pull runs, and the
   same pattern as the QEMU profile's pre-pulled Postgres/Valkey), so a first
   load after boot starts from an image already on disk. Surface a first-load
   image pull through `/v1/downloads` progress in the interstitial.
4. **Backend offer policy, derived not hardcoded.** Do not enumerate hardware:
   offer whatever `recipes.llamacpp.backends[]` reports as `installed` or
   `installable` for the detected host, filtered only by the CP's own allowlist
   (which is *derived* from the device families lemond reports, e.g. a guest
   container backend is offered when its descriptor's device family appears in
   `devices.amd_gpu`/`devices.nvidia_gpu`), tier preference (core > guest), and
   the container-tool preflight. Today that means the six container backends
   are all `gfx1151` (Strix Halo) — but the *mechanism* must not know that;
   when upstream ships the spec'd NVIDIA images (GB10 `llama.cpp` CUDA 13, DS4
   CUDA 13, vLLM nightly are already listed as future work in the
   container-backends spec), the same derived allowlist covers them with no
   control-plane change. Native CUDA remains the NVIDIA path until then, and
   the CP should let lemond install its own per-arch native CUDA binary
   (`POST /v1/install {recipe: llamacpp, backend: cuda}` — the asset carries
   its bundled CUDA runtime, no host toolkit) at update time, off the critical
   path, keeping the Nix-built CUDA binary only as an offline fallback/test
   point. `halogen:rocm` additionally requires Linux ≥ 6.18.4 with
   `CONFIG_HSA_AMD_SVM` — surface the requirement, verify the appliance kernel
   config before offering it.
5. **Planner and catalog evolve.** Backend id becomes a first-class catalog
   field; `select_backend()` reads `recipes.llamacpp.backends[].state` from
   system-info instead of inferring from vendor alone. `slot_policy` /
   `tier` / `format` from system-info decide whether a backend is offered
   (core vs experimental vs guest; guest-tier models stay hidden from
   `/v1/models` until the backend is installed).
6. **Portal narrative.** Render the backend's `message` / `action` /
   `setup_checks` when chat is not ready ("Podman/Docker needs crun", missing
   device groups, kernel too old for halogen) instead of a generic error —
   this is the out-of-the-box-compatibility payoff made visible.
7. **Readiness and health.** The CP keeps talking to lemond (never to the
   container), so the readiness verify and `/v1/chat/completions` checks are
   unchanged; assert the container's `backend_url` is loopback/internal only.
   Confirm the container never joins the LAN.
8. **Acceptance:** real Strix Halo validation across at least two container
   engines (one llama.cpp fork + one ROCm-FPX-class engine); a load/unload
   cycle leaves no orphan containers (`podman ps --filter label=ai.lemonade`
   empty after unload, `docker ps` equivalent with Docker); a power-off during
   a load is recovered by lemond's own container replace on the next load;
   QEMU still green on the native CPU backend.

### P4 — Retire the native pin as the production path

Closing the loop on decision 2 — do this only after P3 is validated.

1. Keep the native CPU build for the QEMU profile (containers are not needed
   there; keep the image lean).
2. Keep native CUDA in the production image **only as an offline fallback and
   test point.** The preferred NVIDIA path is lemond's own native CUDA backend
   (core tier, per-arch `sm_75`–`sm_121` builds with the CUDA runtime bundled,
   so no system CUDA toolkit and no Nix source build); the CP pre-installs it
   via `POST /v1/install` off the critical path so an offline first load still
   finds it on disk. When upstream lands its spec'd NVIDIA container images
   (GB10 `llama.cpp` CUDA 13 is already listed as future work), NVIDIA moves to
   the same container machinery as AMD and the Nix CUDA build retires too.
3. Drop native Vulkan from the production image once Strix Halo is served by
   containers and no AMD dGPU path depends on it — or keep it behind the
   existing single-backend profile flags if the dGPU work (M6.4's unvalidated
   path) still wants it.
4. Remove `LLAMA_BUILD_NUMBER` drift-chasing from the maintenance checklist and
   update `lemond.nix`'s pin comments to describe the legacy role; record the
   new stance in DESIGN §3.6 / §8 / the decision log.
5. **Rollback floor.** A GGUF pulled for a newer llama.cpp build may not load
   under an older one, so any release that changes what the CP loads is a
   one-way door for `min_rollback_version` purposes — set the floor when a
   release changes the loading story.

---

## 4. Verification items (must be tested explicitly; do not assume)

1. `no_fetch_executables = true` vs container image pulls — does the flag
   block the image pull, the backend asset download, or only one of them?
2. `LEMONADE_DEFAULTS_PATH` + `defaults.json` still honored in 46.2.
3. `*_bin` native bridge behavior on 46.2 (the P1 gate).
4. `withWebApp = false` still excludes serving `/app`.
5. Container image pull at first model load — measured size and time, and how
   it interacts with the disk-GC high-water mark (`model_store.py` must count
   the image store, not just `/var/lib/lemonseed/models/`).
6. `all_models_loaded[]` now carries router/classifier helper entries
   (`residency_class: routing-helper`) — the model picker's "loaded" state and
   `loaded_model_names()` should distinguish them from user-facing models.
7. A container-backend load under the lemond unit's existing hardening
   (protects, device policy, `PrivateDevices=false`, `DeviceAllow` list) —
   which carve-outs the socket actually forces.

## 5. Non-goals for this plan

- Exposing the Lemonade App web UI (`withWebApp` stays false; revisit later).
- Changes to OWUI chat, the LiteLLM entity layer, or cloud-offload stance.
- vLLM / NPU / FLM / audio-3D recipes (not on supported hardware today).
- Multi-GPU / heterogeneous scheduling (DESIGN §12.9, unchanged).
