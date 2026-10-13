# M5 — Hardware-aware local chat (design record)

> **Milestone design artifact.** Moved out of `DESIGN.md` during the
> M6 close-out docs cleanup. `DESIGN.md` describes the product **as built**;
> this file is the historical design record for the milestone and may
> describe intent that later work changed. Check `DESIGN.md` first.

---

### M5 — Hardware-aware local chat (design)

**Pass/fail target:** existing **NVIDIA** prototype (Broadwell Xeon-D + NVIDIA class per lab notes). QEMU/`lemonade.corddry.net`-style VMs without GPU are **install-only**, not M5 acceptance hosts.

**Workload:** text chat only — Lemonade serves **one** local LLM; complete a known prompt/response. No image model, no Open WebUI in M5.

**Model policy:** versioned, benchmarked **model catalog**; control plane selects the best fit for detected hardware. Do **not** hardcode a single model for all boxes; do **not** require the user to pick during M5 setup.

#### Hardware inventory (control plane API)
Canonical report (e.g. `/api/hardware`), persisted under `/run/appliance/hardware.json` (or equivalent):
- CPU model, sockets/cores, system RAM
- Per-GPU devices: vendor, PCI ID, name, **per-device** accelerator memory (do **not** sum VRAM across GPUs)
- Driver/toolkit readiness (NVIDIA driver, CUDA/container toolkit visibility)
- User-data free space on the btrfs LV
- Optional later: thermals/power

**NVIDIA path (M5):** host NVIDIA open kernel modules in the SquashFS base; `lemond` runs on the host (systemd) with CUDA/Vulkan backends wired from Nix packages — **not** via NVIDIA Container Toolkit for inference.

**AMD / Ryzen AI Max (M6.4):** Vulkan backend via Mesa RADV ICD. `llama-cli --list-devices` detects AMD GPUs and reports real GTT/VRAM size; `vulkaninfo` is the fallback. See **M6.4** section for details. ROCm/HRX optimization is backlog. NPU (XDNA) is out of scope for M6.4.

**Heterogeneous GPUs (post-M5):** physical devices stay separate. Planner assigns **workloads to devices**; mixed AMD+NVIDIA is **not** pooled memory for one model.

#### Capability planner + catalog
Catalog is a versioned artifact (served/signed like other release metadata) with entries such as:
- model id, backend (`llamacpp:cuda`, …), quantization, weights size, license/hash
- minimum accelerator memory + host RAM reserve + context/KV cost notes
- rank / quality tier from our benchmarks

Planner picks the highest-ranked entry that fits **one** supported execution target. When a GPU is present, `min_vram_gb` applies to VRAM and `min_ram_gb` to system RAM (OS + KV spillover). When no GPU is detected, CPU inference is used and the requirement becomes `ram >= min_vram_gb + min_ram_gb` (weights + overhead all in system RAM), which naturally restricts selection to the smallest models (e.g. E2B, E4B). System RAM is rounded to the nearest whole GiB before comparison (see M6.5).

Legacy intuition from `/home/wago/setup-lemonade.sh` (VRAM buckets → Gemma-class sizes) informs **catalog tiers**, but that script is **not** reused: one-vendor assumption, string-concat VRAM bug, guessed AMD memory, mutates upstream systemd, `/home` model paths.

Rough tier anchors (catalog will own exact ids/hashes):

| Accelerator mem | Example LLM tier (chat-only M5) |
|-----------------|----------------------------------|
| ≥64 GiB | large MoE / 26B+ class |
| ≥32 GiB | ~26B class |
| ≥16 GiB | ~12B class |
| ≥8 GiB | E4B class |
| ≥6 GiB | E2B class |
| below | best small catalog entry or “not ready” |

#### Lemonade bring-up (locked: embeddable `lemond` in SquashFS)
- **Primary path:** ship headless `lemond` (+ `lemonade` CLI for bring-up/debug) **inside the SquashFS**, supervised by systemd; control plane owns lifecycle, catalog selection, pull/load, and health.
- **Not primary:** Lemonade Docker image, Lemonade web UI, Tauri desktop app. Docker remains for later *modules* (Open WebUI, ComfyUI, …).
- Bind **127.0.0.1** only; require `LEMONADE_API_KEY` known only to the control plane; `no_broadcast=true`.
- Mutable state on user-data: **`/var/lib/lemonseed/lemonade/`** (`config.json`, backends cache if any, recipe options) and **`/var/lib/lemonseed/models/`** — never SquashFS, never `/tmp`.
- Prefer **Nix-built backends** (`llama-cpp` CUDA/Vulkan/… as available) wired into `lemond` via store paths / defaults seeding, rather than letting `lemond` download foreign FHS backend trees at runtime.
- **llama.cpp must come from the `nix-amd-ai` flake** (the same source `lemonadePackage` builds against), never an independent pin — see the M6.4 provenance decision. An independent llama.cpp drifts behind the model architectures Lemonade's curated catalog ships (first casualty: `eagle3` in GPT-OSS GGUFs).
- One-shot / idempotent bring-up owned by the control plane (not `/.lemonade-setup-done` on the rootfs).

##### Lessons from [nix-amd-ai](https://github.com/noamsto/nix-amd-ai) (adopt patterns; do not vendor their whole NPU stack in M5)
That flake already solves “Lemonade on NixOS” the hard way. Steal these ideas:

| Pattern | Why it matters for Lemonseed |
|---------|------------------------------|
| **Build `lemond` from source** with `withWebApp=false` / `withDesktopApp=false` | Avoids Ubuntu glibc tarball/`patchelf` pain; keeps SquashFS headless and smaller |
| **Do not let Lemonade fetch TheRock / foreign backend trees** | On NixOS those shadow RPATHs and break loaders; ship self-contained Nix backends |
| **Pin `backend_versions.json` to the backends we ship** | Stops “needs update → download” loops against our store paths |
| **Seed defaults via `LEMONADE_DEFAULTS_PATH` (or equivalent)** | Declarative `*_bin` paths survive read-only `/usr` and SquashFS layout |
| **Re-apply declarative settings on every `lemond` start** | Mutable `config.json` on `@var` must not silently win over appliance policy forever |
| **systemd unit with `RuntimeDirectory`, groups for GPU devices, optional `nix-ld`** | Matches how foreign helper binaries (ASR/TTS later) actually run |
| **Separate package vs module** | Package = store closure in SquashFS; module/CP = port, key, models_dir, backend enable flags |

We may later take `nix-amd-ai` as a flake input for AMD/NPU profiles, or vendor a slim `pkgs/lemonade` derivation inspired by theirs. M5 NVIDIA starts with a **minimal headless packaging spike**, not a full import of their AMD module.

#### First-boot UX (M5)
1. Existing name/timezone setup completes (unchanged).
2. **Background** download/load of the catalog-selected model with progress in the portal (“Preparing local chat…”). Setup must not block Finish on multi-GB downloads.
3. Portal shows **Local chat ready** (or a clear non-ready reason), selected model, and rough resource use.
4. Expandable diagnostics can expose inventory + planner rationale.

#### Acceptance checklist
- [x] `/api/hardware` reports the NVIDIA device(s) and per-GPU memory
- [x] Planner returns a catalog selection with a human-readable reason
- [x] `lemond` systemd unit is healthy on localhost with API key auth
- [x] Host GPU/backend is visible to `lemond` (e.g. `/v1/system-info` / installed recipe)
- [x] Selected model is pulled with hash/license check, then loaded *(CP background pull/load; verify on hardware)*
- [x] Known prompt completes via Lemonade’s local OpenAI-compatible API *(validated manually on prototype; CP loads catalog model)*
- [x] Portal shows ready / not-ready without requiring a manual refresh loop beyond normal polling

**M5 platform note (2026-08-21):** Nix-wired `llamacpp` cpu/cuda, FHS probes, correct `config.json` reseed, and manual Gemma load/chat were verified on the GTX 1660 SUPER prototype before CP automation landed. CP background pull/load/pin + boot reload verified on hardware.

#### Testing (M5)
| Tier | Target | Command |
|------|--------|---------|
| **A — unit** | Update/artifacts, gen-repart, control-plane catalog/hardware/chat/HTTP | `make test` (no root) |
| **B — loopback** | systemd-repart layout, A/B updater | `sudo make repart-test`, `sudo make update-test` |
| **C — QEMU HTTP** | CPU-profile appliance boot; `/healthz`, `/api/status`, `/api/boot` | `make test-qemu` (needs `make build-qemu`) |
| **D — GPU lab** | Full M5 acceptance on NVIDIA hardware / future Proxmox GPU VM | manual / lab automation (later) |

#### Explicitly out of M5
Open WebUI, gateway auth Phase 2, ComfyUI/sd-cpp images, Lemonade web/desktop UI, multi-user accounts, LUKS, AMD production path, multi-GPU scheduling, A/B lock-in gated on model load (lock-in stays on OS/CP health for now).

