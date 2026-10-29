# M6.4 — AMD GPU Vulkan support (design record)

> **Milestone design artifact.** Moved out of `DESIGN.md` during the
> M6 close-out docs cleanup. `DESIGN.md` describes the product **as built**;
> this file is the historical design record for the milestone and may
> describe intent that later work changed. Check `DESIGN.md` first.

---

### M6.4 — AMD GPU Vulkan support (design)

**Decision (2026-09):** M6.4 adds **AMD GPU support via the Vulkan backend** so `lemond` + `llama-server` can use AMD GPUs (dGPU and Strix iGPU) on user hardware. The scope is **Vulkan only** — ROCm and the new HRX backend (Lemonade 11.9) are deferred to the backlog as "AMD GPU driver optimization (ROCm/HRX)". Long-term the system should auto-detect and enable any combination of NVIDIA GPU, AMD dGPU, AMD iGPU, AMD NPU, or CPU.

**Why Vulkan:**
- **Broad coverage:** Mesa RADV (the AMD Vulkan ICD) supports RDNA1/2/3/4 dGPUs and Strix iGPUs with a single driver. No per-GPU driver selection needed.
- **Lightweight:** No ROCm closure (which is multi-GB and lags kernel updates). The Vulkan ICD is already in `pkgs.mesa` (default for `hardware.graphics.enable`).
- **Simple driver model:** The kernel `amdgpu` module ships in the base kernel. Userspace is just the Mesa Vulkan ICD + loader. No display server required.
- **Runtime dispatch:** The control plane selects the backend at runtime from detected GPU vendor (`select_backend`), so a single production image works on both NVIDIA and AMD hardware.

**llama.cpp release strategy (decision 2026-09-19):** We **build llama.cpp from source at image-build time**, pinned to a **tagged upstream release** — never from `HEAD`. The build is part of the release process, so every shipped image's llama.cpp is tested by `make test` + QEMU + real-GPU validation before it reaches a user. We never build from a moving `main`/`HEAD` branch.

- **Tag selection.** We track the **latest stable tagged release** of llama.cpp (e.g. `v0.4.1`, 2026-09-14). When we rebuild, we compare the latest stable tag against the one currently pinned in-tree and bump to it if newer. This keeps us from silently drifting behind the catalog Lemonade curates (the b9190→b10273 gap that first broke `eagle3` loads, and the b9190 multi-slot `common_context_seq_rm` abort that crash-looped lemond on NVIDIA). The tag is recorded in `lemond.nix` (`LLAMA_BUILD_NUMBER` + the `fetchFromGitHub` hash) so the build is reproducible and the rebase is a one-line, reviewable diff.
- **Single build, multi-arch, dedicated per-backend binaries.** One source tree is compiled **once per backend** with the matching architecture flags, producing the dedicated `llamacpp-cpu` / `llamacpp-cuda` / `llamacpp-vulkan` binaries that lemond invokes via its `llamacpp.cpu_bin` / `cuda_bin` / `vulkan_bin` config keys (each key takes a *file path* to a `llama-server` binary). We do **not** ship a single fat binary with all backends compiled in: lemond's config model is one binary per backend slot, a per-backend build keeps each closure lean (a Vulkan-only QEMU box never pays for the CUDA toolchain), and it mirrors how Lemonade itself ships one `llama-server` per backend. The CPU and Vulkan builds come from the **`nix-amd-ai` flake** (built from the same pinned nixpkgs as `lemonadePackage`, carrying their patches — e.g. the RDNA3.5 numerics fix from llama.cpp#28211); the **CUDA build is ours** because nix-amd-ai ships no CUDA package, so we `override` our nixpkgs `llama-cpp` to the same pinned tag with `cudaSupport = true` (source build, no binary cache). All three track the **same pinned build number** so they never drift from each other.
- **Backend selection is the control plane's job, not lemond's.** lemond is told *which* binary to exec via the `--llamacpp <backend>` flag the control plane passes at `lemonade load` time (see `catalog.select_backend`: NVIDIA→cuda, AMD→vulkan, else→cpu). lemond does not choose; it runs the binary we hand it. This is what makes a single production image work on any GPU vendor with runtime dispatch.
- **Upstream platform patch sets.** Where a platform maintains an out-of-tree patch set for llama.cpp (e.g. **AMD ROCm** via `lemonade-sdk/llamacpp-rocm`, a nightly ROCm-7 build pipeline with no source patches today), we pull and build those patch sets into **dedicated targets** that lemond can invoke as an additional backend slot (e.g. `llamacpp-rocm`), and the control plane's `select_backend` decides when to route to them. This is deferred to the backlog item "AMD GPU driver optimization (ROCm/HRX)" — Vulkan is the AMD path until then.
- **Provenance policy (supersedes the 2026-09-18 "never override separately" rule).** The 2026-09-18 rule that we must consume llama.cpp *only* from the `nix-amd-ai` input is **retired** because nix-amd-ai provides **no CUDA package**, so the CUDA build necessarily comes from our own source build. The new policy: **CPU + Vulkan track the `nix-amd-ai` flake ref** (bumped together with `lemonadePackage`), and **CUDA tracks a tagged upstream llama.cpp release** that we pin and rebase ourselves. The `nix-amd-ai` flake input stays pinned — do **not** `.follows` nix-amd-ai's nixpkgs (it would break the binary-cache closure hash and force source rebuilds).
- **Rebase procedure.** (1) Check the latest stable llama.cpp tag on GitHub. (2) If newer than `LLAMA_BUILD_NUMBER`, bump the tag + recompute the `fetchFromGitHub` hash in `lemond.nix`. (3) `make test` + `make build-qemu` + `make test-qemu`, then validate on one real-GPU box (NVIDIA for the CUDA build, AMD for Vulkan). (4) Commit. A rebase is a small, reviewable diff — never a silent drift.
- **Lemonade-patch tracking.** We watch the `lemonade-sdk` GitHub org for llama.cpp patches and other capability packages. Today the only llama.cpp-related artifact is the **ROCm build pipeline** (`lemonade-sdk/llamacpp-rocm`) — incorporated when ROCm lands. Other org packages (e.g. `lemonade-sdk/ryzenai-sd-server` for Ryzen stable diffusion) are **future capability candidates** to evaluate as we bring online more inference features.

**What is NOT in M6.4 (backlog):**
- **CUDA backend alignment (in progress, 2026-09-19):** The CUDA `llama-server` was pinned to nixpkgs 26.05's `pkgs.llama-cpp` (b9190), which lacks `eagle3` and other newer archs and crash-loops on lemond's multi-slot path. Per the llama.cpp release strategy above, the CUDA build is now **our own source build of a tagged upstream release** (initially the latest stable, `v0.4.1`), via `pkgs.llama-cpp.override { cudaSupport = true; }` with the tag pinned in `lemond.nix`. This fixes the `eagle3` load failure (GPT-OSS) and the b9190 `common_context_seq_rm` crash. The rebase procedure (bump tag + hash, `make test` + QEMU + one real-GPU box) is documented in the strategy section. Validating on the NVIDIA VM (RTX Pro 2000 Blackwell) is the gate.
- **ROCm/HIP backend:** `llama-cpp` with `cudaSupport`-equivalent for AMD (`hipSupport`). Requires the multi-GB ROCm closure and kernel module alignment. Deferred until Vulkan proves out on real hardware.
- **HRX backend (Lemonade 11.9):** AMD's new `llamacpp-hrx` backend is experimental, initially targeting gfx1100 (RDNA3) and gfx1151 (Strix Halo) only. Not in our pinned Lemonade 11.5.2 nor nixpkgs 26.05. The RX 6800 (gfx1030/RDNA2) is NOT covered by HRX's initial targets. Re-evaluate when HRX stabilizes and broadens GPU coverage.
- **AMD NPU (XDNA):** The `amdxdna`/XRT/FastFlowLM stack is not in nixpkgs 26.05 (only in the nix-amd-ai flake). Out of scope for M6.4.
- **Lemonade version bumps:** nix-amd-ai has since moved on (11.9 adds the HRX backend, Qwen3-Next support, and a newer llama.cpp pin). Rebase the `nix-amd-ai` flake ref when the HRX/ROCm optimization item is scheduled; that single bump moves lemonade + llama.cpp together.
- **Heterogeneous GPU scheduling:** Mixed AMD+NVIDIA boxes assign workloads to devices; no memory pooling. Post-M5 backlog.
- **CPU RAM reserve re-evaluation:** The APU split reserves a fixed 16GB for the CPU. Re-evaluate once multiple Docker modules run (image generation, etc.) so container memory is reserved without adding swap.
- **Serial console strategy:** Move the kernel console to `/dev/ttyS0` only (RS232 becomes the debug port), keep gettys on KVM for now, and later disable VGA gettys — the final product's VGA should show only a Lemonseed ASCII splash (URL, system/network status, instructions).
- **Installer log hardening:** `/var/lib/lemonseed/installer.log` is currently written by `install.py` only; extend to capture all install-invoked shell scripts (repart, release, amdgpu-kernel-params) via a shared `LEMONSEED_INSTALL_LOG` env convention, and ship the log to the appliance's journal at first boot.
- **MoE preference for low-bandwidth platforms:** When two models of similar size are available (one dense, one MoE), prefer MoE for iGPU/low-memory-bandwidth platforms and dense for dGPU/high-bandwidth. Relevant when the catalog has both variants (e.g. Gemma4 27B MoE vs Gemma4 31B dense).
- **GeoIP model preference:** Do a simple geoip lookup and prefer "local" model families when the user is in a country that produces them (e.g. China → Qwen, US → Gemma) to reduce perceived licensing/political friction.
- **Advanced model picker tab:** Expose the full `lemonade list` catalog in an "Advanced" tab so users with special requirements can pick off-menu models.

#### Implementation

**Nix modules:**
- `nixos/modules/amd-gpu.nix` (new): `hardware.amd-gpu.enable` option. Sets `hardware.graphics.enable = true` (defaults to Mesa/RADV Vulkan ICD), adds `pkgs.vulkan-tools` (vulkaninfo) to systemPackages. Kernel `amdgpu` ships in base kernel (no module needed). No display server.
- `nixos/modules/lemond.nix`: Added `"vulkan"` to `llamacppBackend` enum (now `["cuda" "vulkan" "cpu" "auto"]`). `enableVulkan = cfg.llamacppBackend == "vulkan" || cfg.llamacppBackend == "auto"`. Backend binaries come from the **nix-amd-ai flake** (`llama-cpp-vulkan` / `llama-cpp` for the Vulkan + CPU symlinks; `pkgs.llama-cpp.override { cudaSupport = true; }` for CUDA — see the provenance decision above). Added `vulkan_bin` to defaults JSON + jq re-apply + `environment.etc` symlink. Added `char-kfd` to `DeviceAllow`. `enableCuda` changed to `== "cuda" || == "auto"` (so a `vulkan`-only box doesn't pull the CUDA closure). `flake.nix` passes `nix-amd-ai` via `specialArgs`.
- `nixos/configuration.nix`: Both `lemonseed.nvidia.enable` and `hardware.amd-gpu.enable` default to `true` so the single production image works on NVIDIA and AMD. QEMU profile overrides both to `false` + `llamacppBackend = "cpu"`.
- `nixos/modules/amd-gpu.nix`: `lemonseed-amdgpu-kernel-params` oneshot service (runs `after` `lemonseed-ab-lock-in` so a param-triggered reboot lands on the now-default slot). Exports `LEMONSEED_APPLY_REBOOT=1` so the script reboots when the params changed; `SuccessExitStatus = [ "0" "1" ]` (reboot returns non-zero from the service's view).

**Strix Halo iGPU shared memory (AMD APUs only, PCI 1002:1586):**
- `scripts/amdgpu-kernel-params.sh`: reads *physical* RAM from DMI via `lshw -class memory -json | jq` (NOT `/proc/meminfo` — that reports CPU-addressable memory, already reduced by the GPU split). Allocates `physical_total − 16GB` to the GPU (min 4GB; systems < 32GB keep the default split). Writes a **GRUB stub** to the ESP: `/grub/kernel-params` containing `set kernel_params='ttm.pages_limit=… ttm.page_pool_size=…'` — GRUB has no command substitution and `export` is not a GRUB command, so `boot/grub.cfg` does `source /grub/kernel-params` and appends `${kernel_params}` to every menuentry's `linux` line.
- **Installer path:** `seed_esp()` runs the same script (lspci fallback for iGPU detection — no Vulkan ICD in the installer) *before* first boot, so a fresh install gets the correct split on boot #1 with **zero extra reboots**.
- **Update path:** the service runs after lock-in; if the ESP value differs from the running `/proc/cmdline` it writes the stub, drops `/var/lib/lemonseed/reboot-pending.json` (so the portal shows "Rebooting to apply GPU memory settings…" and keeps polling), and reboots once. The updater also refreshes the ESP `grub.cfg` on every update (new images ship the template at `/usr/share/lemonseed/grub.cfg`; older images had `/etc/lemonseed/grub.cfg` — the updater falls back across both) so stale ESPs self-heal.
- **Manual override:** `LEMONSEED_AMDGPU_SHARED_MEM_GB` env var or `/etc/lemonseed/amdgpu-shared-mem-override` (GB) pins the split instead of auto-calculating.
- **Installer logging:** the install pipeline logs to syslog (journal) and appends to `/var/lib/lemonseed/installer.log`; `_run()` mirrors every subprocess's output (repart, grub-install, amdgpu-kernel-params, …) into the log with the command line prefixed. At the end of the install the log is copied to the target's user-data at `/var/lemonseed/lemonseed/installer.log` so it survives the first boot.

**Control plane:**
- `hardware.py`: New `_amd_gpus_llama_cli()` parses `llama-cli --list-devices` output (reports real GTT/VRAM size, e.g. `56210 MiB` on a Strix Halo iGPU), with `_amd_gpus_vulkaninfo()` as fallback when the Vulkan llama-cpp binary is absent. `collect_hardware()` merges AMD GPUs with NVIDIA GPUs (dedup by vendor+name).
- `catalog.py`: New `select_backend(hardware)` — returns `"cuda"` for NVIDIA, `"vulkan"` for AMD, `"cpu"` otherwise. `plan_model` is now vendor-aware: gates on the *selected* backend's state (not hard-coded CUDA). VRAM fit is skipped when `vram == 0` (AMD Vulkan) and delegated to lemond.
- `catalog/models.json`: Model `backend` field changed from `llamacpp:cuda` to `llamacpp` (vendor-agnostic).
- `chat_ready.py`: `lemonade load` uses `select_backend(hardware)` instead of hard-coded `--llamacpp cuda`. `chat_snapshot()` reports `state="rebooting"` while the reboot-pending marker exists (portal keeps polling until the box is back); CP clears the stale marker at startup. **Load-failure give-up gate (2026-09-18):** lemond's internal Router retries a failed `llama-server` start indefinitely ("evicting all models and retrying"), and the CP worker re-issues `lemonade load` on every `/api/chat` poll — together they form an unbounded crash loop on deterministic load failures (e.g. `unknown model architecture: 'eagle3'`). Fix: `prepare_local_chat` now counts *load* failures (pull/load/health) in addition to *verify* failures. When the cap (`_MAX_VERIFY_FAILURES = 3`) is reached, the model is marked in `_gave_up_models` (keyed by hardware ref). Subsequent non-force calls short-circuit with a terminal error ("failed to load on this hardware. Try a different model family in Settings.") without re-issuing `lemonade load`. The gate is cleared when (a) the user makes a new model choice (`POST /_lemonseed/api/models` → `clear_gave_up()`), or (b) the hardware reference changes (GPU upgrade). `force=True` (explicit user retry) bypasses the gate.

#### Testing

| Tier | Target | Command |
|------|--------|---------|
| **A — unit** | `select_backend` (nvidia→cuda, amd→vulkan, no-gpu→cpu), `plan_model` AMD/NVIDIA/no-GPU cases, `vulkaninfo` parsing, `collect_hardware` merge | `make test` (no root, no GPU) |
| **C — QEMU HTTP** | CPU-profile appliance boot. Note: `qemu-test.nix` sets `lemonseed.lemond.llamacppBackend = "cpu"`, so the QEMU image carries **no** CUDA/Vulkan closure — this tier proves the image builds and boots with the backend dispatch code present, but does **not** exercise Vulkan | `make test-qemu` (needs `make build-qemu`) |
| **D — GPU lab** | Real AMD hardware validation: `vulkaninfo` lists the GPU, `lemond` loads a model on the Vulkan backend, chat completes | Manual on RX 6800 (gfx1030) / Ryzen AI 385 (gfx1151) |

**VRAM reporting:** AMD Vulkan GPUs report real memory via `llama-cli --list-devices` (shipped as `/etc/lemonseed/lemond-backends/llamacpp-vulkan-cli` from the Vulkan llama-cpp closure). On APU iGPUs this is the GTT allocation (shared system RAM), not dedicated VRAM. The planner uses this for model-fit validation. `vulkaninfo` fallback (no memory size) is used only when the llama-cli binary is absent.

#### Acceptance checklist
- [x] `make test` green (96 control-plane tests including 11 new AMD/Vulkan tests)
- [x] `vulkaninfo` detects AMD GPUs (dGPU + Strix iGPU) with `vendor: "amd"`
- [x] `select_backend` returns `"vulkan"` for AMD, `"cuda"` for NVIDIA, `"cpu"` otherwise
- [x] `plan_model` is vendor-aware (gates on selected backend, not hard-coded CUDA)
- [x] `chat_ready` uses `select_backend` for `lemonade load`
- [x] Single production image includes both CUDA and Vulkan closures (`llamacppBackend = "auto"`)
- [x] QEMU smoke test green (CPU path). The QEMU profile is CPU-only, so this never exercised Vulkan; it is green on the landed profile as of the M6.3b runs.
- [ ] **UNTESTED — no AMD hardware available.** Real-hardware validation: RX 6800 (gfx1030) — `vulkaninfo` + lemond load + chat
- [x] **VALIDATED on real hardware (2026-10).** Strix Halo APU (Ryzen AI Max+ 395 class, gfx1151
      iGPU — the 8060S) — `vulkaninfo` + lemond load + chat, end to end, no problems found. The
      shared-memory carve-out (`ttm.pages_limit`) and GTT-sized VRAM reporting behave as designed.

**Status note (2026-10, corrected):** an earlier draft of this note said no AMD hardware had
been available and treated M6.4 as wholly unvalidated. **That was wrong**: the **Strix Halo
APU** path has been validated end to end on real hardware (see the checked item above), and
`hardware.py` parses measured output from that silicon. What is genuinely still unrun is the
**AMD dGPU** tier-D check — a PCIe Radeon card has never been booted on the appliance. That
gap is tracked in `DESIGN.md` §11 (deferred platforms), not here, since this file is the
historical design record. No *automated* test exercises Vulkan on either path: the QEMU
profile pins `llamacppBackend = "cpu"`.


