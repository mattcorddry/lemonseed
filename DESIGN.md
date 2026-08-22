# Lemonseed Appliance — Design

Status: **living design** (M1–M4.1 done; M5 done; M6 next — primary UX undecided)  
Related: [Concept.md](./Concept.md) (product vision), [Requirements.md](./Requirements.md)

This document captures architectural decisions. It is the source of truth for implementation milestones. Product intent lives in Concept.md; this file translates that intent into buildable decisions.

---

## 0. Product framing (from Concept.md)

Lemonseed is a **local, multi-user home AI hub** — not a cloud client and not a DIY container toolkit. The OS appliance (read-only SquashFS, A/B updates, installer, control plane) exists so non-technical households can run open models privately on LAN hardware.

**Release 1 product surface (Concept):** chat (text; voice later), background workers/notifications, image gen/edit.  
**Release 2 (Concept):** short video, documents, speaker/voice appliance.

**Differentiator:** always-on LAN quality above phone/laptop on-device models; trusted sharing inside one household; open-source stack inspection; **no cloud AI dependency** for core chat.

**Stack intent (locked for M5 direction):**
| Layer | Choice |
|-------|--------|
| Inference backend | **Embeddable `lemond` in SquashFS** (headless; no Lemonade web/desktop UI). Built from source via Nix, not the Ubuntu release tarball. |
| First UX | Control-plane “local chat ready” status + Lemonade OpenAI-compatible API; **primary UI chosen in M6** (see M6 bookmark) |
| Image gen | Deferred past M5; prefer **ComfyUI** later over Lemonade llama-sd / sd-cpp |
| Agents | Candidate primary UX in M6 evaluation (NanoClaw-style); not dual-shipped with chat UI by default |
| Brand | **lemonseed.ai** |
| Docker | Remains for **modules** (Open WebUI, etc.); **not** the primary Lemonade runtime |

**Hardware product goal (later device):** silent shelf appliance, 1–10 users, 64GB+ class SoC (Ryzen AI Max / NVIDIA Spark-class). **Software validates first** as an OSS machine image on off-the-shelf PCs before ODM packaging.

---

## 1. Goals and non-goals

### Goals
- Build a **repeatable, version-controlled** NixOS-based appliance image for **x86_64**.
- Produce a **minimal, read-only** SquashFS root plus matching `vmlinuz`.
- Run **systemd** as the root of control; Docker as the container platform; a **control plane** we own for orchestration and UI.
- Support **A/B root updates** with GRUB one-shot boot and lock-in on successful startup.
- Keep artifact layout **ready for detached signature verification** and **HTTP distribution**.
- Prefer **no initrd** when practical; keep initrd as an open design option if hardware bring-up requires it.
- Discover hardware capabilities at runtime and **fit open models** to those constraints without requiring cloud accounts.

### Non-goals (for now)
- ARM builds
- Legacy BIOS / MBR boot support (EFI-only assumption)
- Offline USB image payload (installer downloads from lemonseed.ai; offline option later)
- Installer WiFi AP / captive portal (Ethernet-only for M4)
- CI builders (local builds first)
- Secure Boot / TPM root of trust (later; detached sigs first)
- Shipping application container images inside the base SquashFS
- Writable overlay / tmpfs overlay on root
- Cloud model burst / anonymized cloud GPU relay (Concept “future”; not M5)
- Multi-user auth, Open WebUI, ComfyUI (post-M5; primary UX TBD in M6)
- LUKS/encrypted user-data (deferred past M5; plan later)

---

## 2. Milestone plan

| Milestone | Deliverable |
|-----------|-------------|
| **M0 — Design** | This document |
| **M1 — Artifacts** | **Done** — Flake + Makefile → `root.squashfs`, `vmlinuz`, `initrd.img`, `manifest.json`, optional `.sig`; `build-production` / `build-qemu` / `run-qemu` / `clean` |
| **M2 — Bootable skeleton** | **Done** — LVM PV → initrd provisioner → btrfs `@etc`/`@var`; stub control plane; QEMU userdata disk + `qemu-clean` |
| **M3 — Partition & A/B tooling** | **Done** — `partitions.yaml` → `out/repart.d`; `boot/grub.cfg`; slot-aware root; `lemonseed-ab` + lock-in; `lemonseed-update` in-place updater |
| **M4 — USB installer** | **Nearly done** — install + portal + first-boot setup; polish remaining |
| **M4.1 — Preserve known user-data on reinstall** | **Done** — namespaced storage identity; preserve checkbox; layout-compatible reinstall |
| **M5 — Hardware-aware local chat** | **Done** — `lemond` + NVIDIA/CUDA; inventory + catalog planner + CP pull/load/pin + portal “Local chat ready”; unit tests + QEMU API smoke |
| **M6 — Primary interaction UX** | **In progress** — UX evaluation → one primary module + gateway Phase 2 auth/routing |
| **M6+ (later)** | ComfyUI, multi-user, AMD/UMA profiles, heterogeneous GPU planner, encryption, offline install, Secure Boot/TPM |

**Immediate next step:** run the M6 UX evaluation spike (below), record a decision, then implement gateway Phase 2 around the chosen primary module.

### M6 — Primary interaction UX (design)

**Goal:** Ship **one** household-facing chat/assistant experience on the LAN — not two overlapping “main” UIs. M6 ends with a chosen primary path, a Docker module wired through nginx, and gateway auth Phase 2 scoped to that path.

**Non-goals for M6:** multi-user accounts with per-user data isolation, ComfyUI/image gen, voice appliance, mobile apps, cloud relay, shipping both candidates as equal defaults.

#### Why this gate exists

Concept.md describes both **chat** (one-shot and long-running conversations) and **workers** (background agents, notifications, automations). Those can be one product surface or two. Lemonseed must pick a **primary metaphor** before we invest in login flows, module routing, and onboarding copy — otherwise we build gateway auth twice and households see duplicate entry points.

| Path | Primary metaphor | Best when… |
|------|------------------|------------|
| **A — Traditional chat** (Open WebUI or similar) | “Family ChatGPT on the LAN” | Chat is the main job; workers/agents are later add-ons behind the same UI or API |
| **B — Agentic messaging** (NanoClaw-style) | “Always-on assistant + channels” | Proactive workers, messaging-style threads, and tool loops are co-equal with chat |

**Locked regardless of path:** inference stays **headless `lemond` in SquashFS** on localhost; the primary UI is a **Docker module** talking to `http://127.0.0.1:13305` (OpenAI-compatible API + API key from CP).

#### Evaluation spike (do before implementation)

Run both candidates on the **NVIDIA prototype** (or future Proxmox GPU VM) against the **M5 default model** already loaded. Time-box to a few days; capture notes in this doc or a short `docs/m6-ux-evaluation.md`.

| Criterion | Questions |
|-----------|-----------|
| **Household fit** | Can a non-technical user start a chat in &lt;2 minutes after setup? Is proactive/agent behavior understandable without a manual? |
| **Lemonade integration** | How much config to point at local Lemonade (base URL, model id, API key injection)? Any features that assume cloud or Ollama-only? |
| **Ops on NixOS appliance** | Image size, pull policy, volume layout under `/var/lib/lemonseed/`, upgrade story, health checks CP can poll |
| **Security / isolation** | Container boundaries, tool execution, filesystem access — especially for Path B |
| **Concept alignment** | Path to Release 1 workers/notifications without a second front door |
| **Maintenance** | Upstream velocity, license, fit with our SquashFS + Docker module model |

**Decision output (required):** one row in the table below, plus explicit deferral of the other path.

| Decision | Primary module | Deferred path |
|----------|----------------|---------------|
| TBD | — | — |

#### Architecture (common to either path)

```
Browser / LAN client
        │
        ▼
   nginx :80  ── Phase 2: auth + route table
        │
        ├── /           → control plane portal (status, admin, module links)
        ├── /chat/…     → primary module (path prefix TBD by winner)
        └── /api/…      → CP JSON API (localhost origin only from CP process)
        
lemond :13305 (127.0.0.1, API key)  ← never exposed directly to LAN
```

**Gateway Phase 2 (M6 deliverable after UX decision):**
- Replace throwaway Phase 1 nginx vhost with **session auth** (exact mechanism TBD: e.g. household shared password first, per-user later).
- **Route table** owned by CP config (or generated nginx snippet): primary module at `/` or `/chat`, CP admin at `/settings` or subpath.
- Module containers get **only** what they need: Lemonade URL + read-only API key via env/file from CP; no host network.
- CP continues to own: catalog, model load, `chat-ready` state, Docker pull/start for modules.

**Portal changes:** “Local chat ready” remains the inference status line; add a single **“Open chat”** (or equivalent) CTA that deep-links into the chosen module — not separate buttons for two products.

#### Candidate A — Open WebUI (traditional chat)

| Aspect | Notes |
|--------|--------|
| **Shape** | Mature web UI; OpenAI-compatible backend; model picker, history, RAG hooks |
| **Pros** | Familiar UX; strong Lemonade/OpenAI API story; aligns with “chat first” in Concept R1 |
| **Cons** | Agent/worker story is bolt-on; may fight our “one default model” policy unless configured |
| **Integration sketch** | Docker module; env `OPENAI_API_BASE_URL=http://host.docker.internal:13305/v1` (or gateway subpath); inject `LEMONADE_API_KEY`; disable cloud providers; pin default model to catalog selection |
| **Risks** | Feature creep (RAG, multiple models) vs appliance simplicity |

#### Candidate B — NanoClaw-style agentic messaging

| Aspect | Notes |
|--------|--------|
| **Shape** | Container-isolated agent loop + messaging/web UI; channels, sessions, tools |
| **Pros** | Closer to Concept workers/notifications; differentiated from “another ChatGPT skin” |
| **Cons** | Younger ecosystem; harder household onboarding; security review burden for tool execution |
| **Integration sketch** | Docker module(s); Lemonade as LLM provider; CP provisions API key + model id; strict volume and network policy |
| **Risks** | Overlap with future “workers” if we also add Open WebUI later; ops complexity |

#### M6 phased deliverables

| Phase | Deliverable |
|-------|-------------|
| **M6.0** | UX evaluation spike + written decision (this section) |
| **M6.1** | Package chosen module in Nix/Docker; CP `modules.json` or equivalent; pull/start on boot when chat-ready |
| **M6.2** | Gateway Phase 2: nginx auth + routing; portal CTA; module health in `/api/status` |
| **M6.3** | Household smoke test: setup → portal → primary UI → send message → reply via local Lemonade |

#### M6 acceptance checklist

- [ ] Written UX decision (A or B) with deferred path noted
- [ ] Primary module runs as Docker module with Lemonade on localhost only
- [ ] nginx Phase 2: auth on LAN-facing routes; CP API not anonymously world-writable beyond current setup intent
- [ ] Portal single entry point to primary UX
- [ ] `make test` still green; module bring-up covered by test or documented manual gate
- [ ] No second co-equal “main” UI shipped by default

### M3 verification

```bash
make repart-generate          # partitions.yaml → out/repart.d/
sudo make repart-test         # 20G loopback: ESP + root-a/b + grow user-data
make disk-repart DISK=/dev/sdX DRY_RUN=1   # dry-run on real hardware
```

### M4 verification (as implemented)

```bash
make build-installer          # → out/installer/lemonseed-installer.iso
# Flash ISO to USB, boot target PC, open http://lemonseed.local/
```
### M4 — USB installer (design)

**Product goal:** non-technical user buys a blank PC, boots Lemonseed USB, finishes install from a phone/laptop browser on the LAN. No assumption of KVM or serial console.

#### UX / discovery
| Surface | Behavior |
|---------|----------|
| Boot | Auto-start installer (no GRUB/isolinux “press Enter” menu) |
| Discovery | Avahi/mDNS advertise `lemonseed.local` on all interfaces |
| Install UI | HTTP on port **80** (no TLS during install); mobile-friendly; progress bar |
| Local console (optional) | Status splash on **tty4** (auto `chvt`): URL + IPs; not tty1 (avoids getty races) |
| Debug | Root shell on **tty2** (`Ctrl+Alt+F2`); return to splash with `Ctrl+Alt+F4` |

#### Disk policy
- **Auto-pick** the preferred internal disk; confirmation required before wipe.
- **Disallow** install onto USB-attached or **eMMC** media (not suitable for appliance user-data).
- If multiple eligible disks exist, confirmation screen allows selecting an alternate.
- Inspect existing labels/partition tables; show a **warning bar** if an existing OS would be wiped.
- Never target the installer USB itself (boot medium exclusion).
- Reinstall over a previous Lemonseed disk is expected: leftover `appliance` LVM maps (even with nothing mounted) hold the disk busy and `systemd-repart` cannot reread the GPT. The wipe path deactivates those maps and the installer ISO disables LVM autoactivation.

### M4.1 — Preserve known user-data on reinstall

**Why now:** `/var` will hold chat history, agent configuration, user data, Docker volumes, and M5 model state. Reinstalling the immutable OS must not silently erase that data.

#### Ownership proof
A partition is preservable only if all of these checks succeed (M4.1+ only; no pre-M4.1 layout support):

1. GPT partition label `lemonseed-user-data` and LVM type.
2. LVM VG/LV `appliance` / `user-data` with tag `lemonseed.storage=v1` on a single PV on that partition.
3. The LV is btrfs labeled `lemonseed-user-data` with required subvolumes (`@etc`, `@var`).
4. Top-level `.lemonseed-storage.json` identity (`format=lemonseed-storage-v1`).

Any ambiguity means **do not offer preservation**.

#### Installer behavior
- Disk selection API reports `preservable_user_data` plus non-sensitive size/version summary only after the proof succeeds.
- If true, show **“Preserve existing user information on this appliance”**, checked by default. Explain that it retains chat/workers/models and replaces Lemonseed system software.
- Fresh install and unchecked preservation retain the current destructive `systemd-repart --empty=force` path.
- Checked preservation is a **layout-compatible reinstall** only: do not repartition or resize user-data; deactivate its LVM/DM maps **without** signature wiping; rewrite `root-a`, clear/reseed `root-b`, reformat/reseed ESP and GRUB.
- The installer must refuse preservation if ESP/root/user-data geometry or labels are incompatible. It may offer a destructive install, never a best-effort data move.
- After boot, storage provisioning recognizes the preserved identity and is strictly non-destructive.

#### Verification
- [ ] Fresh install writes identity and namespaced storage metadata.
- [ ] Reinstall detects a known volume and defaults Preserve on.
- [ ] Preserve path retains a sentinel under `@var` and setup state under `@etc`.
- [ ] Uncheck path destroys that sentinel.
- [ ] A generic LVM/btrfs disk never exposes Preserve.

#### Image source
- Download appliance artifacts from **lemonseed.ai** (channel default: `https://lemonseed.ai/releases/stable/`).
- Fetch `manifest.json`, then `vmlinuz` / `initrd.img` / `root.squashfs`; verify SHA-256 from the manifest.
- Avoids stale SquashFS baked onto old sticks.
- Offline/bundled payload and alternate channels (e.g. beta) are **later** options.
- **Lab overrides** (kernel cmdline or env, not in the UI):
  - `lemonseed.image=http://host:port/` — HTTP directory containing `manifest.json` + artifacts
  - `lemonseed.artifacts=/path` or `LEMONSEED_ARTIFACT_DIR` — local directory of artifacts

#### Network
- **Ethernet + DHCP only** for M4.
- WiFi AP SSID (e.g. `lemonseed-install`) is roadmap, not M4.

#### Install pipeline (high level)
1. Confirm disk → release leftover LVM/DM holders on the target (reinstall-safe; freeze udev so `appliance-user--data` cannot be remapped) → `systemd-repart --empty=force` from `partitions.yaml` (wipe)
2. Download + verify artifacts → write SquashFS to **root-a** (root-b left empty for the updater)
3. Seed ESP: GRUB (`grub-install` + `EFI/BOOT/BOOTX64.EFI`), kernels/initrds for slot a, `grubenv` (`boot_default=a`)
4. Register EFI boot entry (`efibootmgr`, label **Lemonseed**) on the target disk
5. Prompt: remove USB → reboot into appliance. The installer page stays open and polls `http://lemonseed.local/` (and the current origin) until the appliance `/api/status` reports `role=control-plane`, then loads the first-boot setup UI.

#### EFI reboot / USB stick
Always **prompt the user to remove the installer CD/USB**, then reboot into the installed system.

Before the prompt, `sync` so the target disk is fully flushed. Reboot uses **sysrq-b** (`echo b > /proc/sysrq-trigger`), not a graceful `systemctl reboot` — the live root is on the removable media, and a clean shutdown after eject floods SquashFS read errors.

Do not rely on `BootNext` / BootOrder tricks to skip the stick — firmware behavior is too inconsistent, and a single “remove media → reboot” step is clearer for non-technical users.

Installer still registers a normal EFI boot entry on the internal ESP so the machine boots Lemonseed once the stick is gone.

#### Roadmap (post-M4 installer)
- WiFi AP `lemonseed-install`
- Expandable web terminal for live install logs
- Advanced options: download rate limit, custom storage, image channel (beta/offline)

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

**AMD / Ryzen AI Max (post-M5):** treat usable accelerator memory as an **UMA / system-RAM budget**, not discrete VRAM. Inventory-only until a dedicated profile exists. Reuse patterns from [nix-amd-ai](https://github.com/noamsto/nix-amd-ai) (ROCm/Vulkan/NPU wiring) when that profile lands.

**Heterogeneous GPUs (post-M5):** physical devices stay separate. Planner assigns **workloads to devices**; mixed AMD+NVIDIA is **not** pooled memory for one model.

#### Capability planner + catalog
Catalog is a versioned artifact (served/signed like other release metadata) with entries such as:
- model id, backend (`llamacpp:cuda`, …), quantization, weights size, license/hash
- minimum accelerator memory + host RAM reserve + context/KV cost notes
- rank / quality tier from our benchmarks

Planner picks the highest-ranked entry that fits **one** supported execution target. CPU-only install is diagnostic/fallback and does **not** qualify a hardware profile as product-ready.

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

---

## 3. High-level architecture

```
┌─────────────────────────────────────────────────────────────┐
│  HTTP distribution                                          │
│  appliance-<version>/                                       │
│    vmlinuz  root.squashfs  manifest.json  *.sig             │
└─────────────────────────────┬───────────────────────────────┘
                              │ install / update
                              ▼
┌──────────────┐  ┌─────────────┐  ┌─────────────┐  ┌──────────────────────────┐
│ ESP (FAT32)  │  │ squashfs-a  │  │ squashfs-b  │  │ user-data (LVM PV)       │
│ GRUB +       │  │ (dd image)  │  │ (dd image)  │  │ → VG appliance           │
│ vmlinuz(s)   │  │             │  │             │  │ → LV user-data (btrfs)   │
│ grub.cfg     │  │             │  │             │  │ → @etc,@var → /etc,/var  │
│ grubenv      │  │             │  │             │  │   (+ docker volumes)     │
└──────┬───────┘  └──────▲──────┘  └──────▲──────┘  └──────────────────────────┘
       │                 │                │
       │    GRUB picks A or B (default / once)
       └─────────────────┴────────────────┘
                         │
                         ▼
              read-only SquashFS root
              systemd → control-plane → lemond (localhost API)
                         │                 └── llama.cpp / backends (Nix-wired)
                         └── Docker modules later (Open WebUI, …)
```

---

## 4. Disk layout (EFI-first GPT)

**Assumption for now:** EFI-capable hardware only. Non-EFI/MBR support is deferred (GPU appliances are typically EFI).

| # | Role | Type | FS / content | Size |
|---|------|------|--------------|------|
| 1 | ESP | GPT EF00 | FAT32 — GRUB EFI, kernels, `grub.cfg`, `grubenv` | 0.5–1 GiB |
| 2 | root-a | Linux / custom label | SquashFS written with `dd` (no FS layer) | ~4 GiB |
| 3 | root-b | Linux / custom label | SquashFS written with `dd` | ~4 GiB |
| 4 | user-data | GPT 8E00 (LVM) | **LVM PV only at install**; first boot creates VG/LV/btrfs | all remaining |

Notes:
- ESP is **not mounted in normal operation**; mount on demand for updates and A/B lock-in.
- **In-place updates:** `lemonseed-update` on the appliance writes verified SquashFS + ESP kernels into the **inactive** slot, sets GRUB `boot_once`, and leaves `@etc`/`@var` untouched. On the next reboot GRUB tries the new slot once; `lemonseed-ab-lock-in` promotes `boot_default` only after platform services (network, nginx, control plane, Avahi, Docker) are healthy. A failed boot falls back to the previous `boot_default`. Lab overrides: `--url` / `--dir` (same conventions as the installer).
- Root partitions are **raw SquashFS images**, bootable without unpacking to ext4.
- **Installer** creates the GPT layout and LVM PV; it does **not** format btrfs.
- **First-boot provisioner** (`appliance-storage-provision.service`, shell oneshot every boot): if LV missing, create VG `appliance` / LV `user-data`, `mkfs.btrfs`, subvols `@etc` / `@var`. If LV exists, no-op aside from ensuring subvols.
- Later: additional disks → extra PVs → `vgextend` / `lvextend` / btrfs resize (control plane; not M2).
- Exact sizes and type UUIDs live in a **separate partition config** (see §5), not hardcoded in Makefiles.

### Bootloader
- **GRUB** on EFI (`grubx64.efi` on ESP).
- A/B selection via **GRUB environment block** (`grub-editenv`), reusing stock GRUB mechanisms.

### A/B boot protocol

Variables (names indicative; finalize at M3):

| Variable | Meaning |
|----------|---------|
| `boot_default` | Permanent slot: `a` or `b` |
| `boot_once` | Optional one-shot slot for next boot |
| `boot_attempted` | Slot being tried (set when consuming `boot_once`) |

Flow:
1. Update writes new SquashFS + matching `vmlinuz` into the inactive slot / ESP.
2. Updater sets `boot_once=<new>` (and clears prior attempt state as needed).
3. GRUB: if `boot_once` set → boot that slot once, set `boot_attempted`, clear `boot_once`.
4. On **successful startup**, control plane (or early systemd unit) mounts ESP and sets `boot_default` to the running slot, clears `boot_attempted`.
5. On **failure** (crash/hang before lock-in), power cycle → GRUB uses last `boot_default`.

“Successful startup” criteria for lock-in will be defined with the control plane (e.g. control plane healthy + Docker up). Stub in M2 can lock-in after a simple local health check.

---

## 5. Partition configuration format

### Evaluation of existing tools

| Tool | Format | Strengths | Fit for us |
|------|--------|-----------|------------|
| **systemd-repart** | INI `repart.d/*.conf` | GPT-native; grows partitions to fill disk; `CopyBlocks=` for raw image install; btrfs `Subvolumes=`; already on a systemd appliance | **Best runtime/install engine** |
| **disko** | Nix | Excellent for NixOS installs; reproducible | Strong if installer is Nix-based; less ideal as a portable text contract for non-Nix helpers |
| **genimage** | libconfuse `.cfg` | Great for **building** fixed disk images offline | Weak for installing onto **arbitrary disk sizes** in the field |
| **Yocto WIC (.wks)** | Kickstart-like | Familiar A/B patterns in embedded | Yocto-centric; pulls us off Nix |
| **sfdisk** | Dump script | Simple, scriptable | Low-level; no FS/subvol/copy semantics |

### Decision (proposed)
- **Source of truth in-repo:** human-editable **YAML** (preferred), describing roles, sizes, FS, labels, mount policy.
- **Install/apply engine (M3/M4):** generate or map to **`systemd-repart`** definitions and invoke `systemd-repart` (plus thin helpers for GRUB and SquashFS `CopyBlocks`/`dd`).
- Rationale: YAML stays easy to review and edit; repart already solves “same layout, different disk sizes,” raw block copy into partitions, and btrfs subvolume creation—aligned with an appliance USB installer.

Example shape (illustrative only):

```yaml
# partitions.yaml
table: gpt
firmware: efi   # efi-only for now

partitions:
  - name: esp
    role: esp
    size: 1G
    type: ef00
    filesystem: vfat
    label: ESP
    mount:
      path: /boot
      policy: on-demand   # not mounted in normal operation

  - name: root-a
    role: squashfs-root
    size: 4G
    label: root-a
    content: raw-squashfs

  - name: root-b
    role: squashfs-root
    size: 4G
    label: root-b
    content: raw-squashfs

  - name: user-data
    role: lvm-pv
    size: grow
    type: 8e00
    label: lemonseed-user-data
    # First boot (not installer): VG appliance / LV user-data / btrfs
    # subvolumes @etc → /etc, @var → /var
```

Final mount flags per path will be tightened during bring-up; principle: **`noexec` / `nodev` / `nosuid` wherever practical**.

### Storage ownership
| Layer | Responsibility |
|-------|----------------|
| Installer (M4) | GPT + LVM PV for user-data |
| `appliance-storage-provision` | Every boot; create VG/LV/btrfs/subvols if missing |
| Control plane | Future multi-disk expand; Docker/app orchestration |

---

## 6. Root filesystem model

### Read-only SquashFS
- NixOS build produces a SquashFS root.
- Installed onto A/B partitions with **`dd` / block copy** so the kernel can use the partition as root.
- **No overlayfs / tmpfs overlay** on `/`.

### Mutable paths
- btrfs subvolumes on the **user-data LV**, mounted over paths that need RW (`subvol=@etc`, `subvol=@var`).
- Initial set: **`/etc`**, **`/var`** only.
- **`/tmp`:** size-capped **tmpfs** (`boot.tmp.useTmpfs`, 128M) — not a btrfs subvol and not a symlink to `/var/tmp`. Needed early (before user-data), and must not compete with GPU/model RAM. Persistent scratch belongs on `/var`.
- Docker volume data lives under user-data (typically under `/var/lib/docker` once `/var` is mutable).
- Additional mounts only as future need appears.
- Device discovery for the PV: `/dev/disk/by-partlabel/lemonseed-user-data`, else kernel cmdline `appliance.storage=` (QEMU).
- **`/etc` population:** empty `@etc` is fine; NixOS `setup-etc.pl` activation recreates managed `/etc` from the Nix store each boot. We do **not** copy SquashFS `/etc` into `@etc`.
- **Metadata convention:**
  - `/run/appliance/version` — image version from the booted closure (tmpfiles symlink; not on `@etc`)
  - `/etc/appliance-*` — mutable markers on `@etc` (e.g. `/etc/appliance-first-boot` ISO-8601 UTC, written once after `time-sync.target`)
- **`/var`:** empty `@var` is fine; systemd-tmpfiles and services create what they need; state persists across image updates.

### Initrd (open option)
- Soft goal: **no initrd** — kernel mounts SquashFS root directly with needed drivers built-in.
- Practical risk: diverse storage (NVMe/SATA/USB) and btrfs/ESP bring-up may force a small initrd.
- Decision deferred to implementation spikes in M1/M2; design must not assume initrd is impossible.
- If initrd is required, keep it **minimal** (storage + filesystem modules, mount logic) and version/sign it like other boot artifacts.

---

## 7. Image contents

### In the SquashFS base
- systemd
- Docker (host networking) — for **modules**, not for `lemond`
- **Headless Lemonade (`lemond` + CLI)** built from source; no Lemonade web/desktop UI
- GPU driver stacks in-base for discovery at boot (**NVIDIA open kernel modules** preferred over legacy closed; AMD later)
- Inference backends as Nix packages where practical (`llama-cpp` CUDA/Vulkan/…); kernel selected by **GPU support matrix**
- **Control plane** (systemd-supervised)
- Tools needed for updates: mount ESP on demand, `grub-editenv`, verify signatures, write inactive slot

### Not in the SquashFS base
- Open WebUI, ComfyUI, and other **module** images — pulled at runtime via control plane / Docker
- Model weights and Lemonade mutable config — live on user-data under `/var/lib/lemonseed/`
- User data

### Networking
- Docker **host networking** to avoid bridge/CNI complexity (for modules).
- `lemond` listens on **loopback only**; nginx :80 is the only LAN entry.

### Control plane (direction; stub in M2)
- **Language:** Python, if image overhead stays low–moderate (revisit Go if footprint hurts).
- **Process split:**
  1. **Privileged daemon** — Docker lifecycle, image updates, A/B lock-in, disk ops; binds **127.0.0.1:8080** only.
  2. **Web UI** — served by the same process for bring-up; later may split to an unprivileged UI talking to the privileged API.
- systemd starts and restarts the privileged control plane; that plane brings up containers/volumes.

### HTTP gateway (M4+)
- **Public entry is always nginx on :80** (firewall opens 80; CP stays loopback-only).
- **Phase 1 (first boot / unset appliance):** nginx is a straight reverse proxy to `127.0.0.1:8080`. The CP serves a **no-login** readiness + setup wizard (`/`, `/setup`, `/api/boot`). Completing setup writes `/etc/appliance-setup-complete` (+ JSON state).
- **Phase 2 (post-setup):** replace/swap the nginx vhost for **auth + module routing** (control plane UI + Docker modules such as Open WebUI) while keeping :80 as the single pane of glass. Phase 1 config is intentionally throwaway.
- **mDNS:** Avahi publishes the slug of the appliance **Name** from setup (`Matt Lemonseed 1` → `matt-lemonseed-1.local`). Before setup the name defaults to `Lemonseed`, so a fresh appliance is reachable at `lemonseed.local` for the installer hand-off. After a preserve reinstall of a **renamed** appliance, `lemonseed-avahi-alias` briefly publishes `lemonseed.local` as a CNAME so the installer can discover the box; the installer (and any leftover `lemonseed.local` hit) then redirects to `<slug>.local` and the alias is released (`/run/lemonseed/mdns-handoff-done`). Fully confirmed appliances never claim `lemonseed.local`, so multiple units can share a LAN.
- **Appliance identity (one name, not two):** setup collects a single user-facing **Name**; its slug is the system hostname, the mDNS name, and the hostname dhcpcd advertises. `networking.hostName` is therefore **empty** in the NixOS config — a non-empty value makes `/etc/hostname` a read-only store symlink and pins `host-name=` in `avahi-daemon.conf`, both of which would defeat the chosen name. `lemonseed-identity.service` seeds `/etc/hostname` on `@etc` (from `/etc/appliance-setup.json`, else the existing value, else `lemonseed`) before dhcpcd/Avahi start.
- **Name collisions:** setup resolves `<slug>.local` over mDNS before offering a default; if another host owns it, the suggested name becomes `Lemonseed 2` (editable), and submitting a name owned by a different host is rejected. Avahi's effective hostname (queried over D-Bus, including any collision suffix) identifies our own announcement; IP comparison is only a fallback because DHCP leases can change. Multiple appliances therefore never share one advertised name.
- **Local console:** tty4 status splash (IP / URL / boot hints, `docker*` interfaces excluded); tty2 root autologin for field debug (empty root password during bring-up only).

---

## 8. Build system

### Nix
- **Flakes** + pinned `nixpkgs` for reproducibility.
- Project root: this directory (`nix/`), intended for GitHub.
- No absolute paths in Makefiles or scripts — everything relative to repo root / `CURDIR` / Nix store outputs.

### Makefile (M1)
| Target | Behavior |
|--------|----------|
| *(default)* / `help` | Print help; no build |
| `build-production` | Production SquashFS + `vmlinuz` (+ initrd) → `out/latest` |
| `build-qemu` | QEMU smoke profile (different root device / tmpfs mounts) → `out/latest-qemu` |
| `run-qemu` | `build-qemu` if artifacts missing, then boot serial console |
| `clean` | Remove `out/` |
| `keys` | Generate local ed25519 test keys |

Production and QEMU are **separate NixOS builds**, not a repackage of each other.

### Versioning
- Default: `YYYYMMDD.NN` where `NN` is a **local build counter** (file in repo tree, gitignored).
- Override: environment variable (e.g. `VERSION=20260810.01 make build-production`) for official releases.

### Outputs (HTTP-ready layout)

```
out/appliance-<version>/
  vmlinuz
  root.squashfs
  manifest.json          # names, versions, hashes, slot metadata
  vmlinuz.sig            # optional until signing enabled
  root.squashfs.sig
  manifest.json.sig
```

Distribution may later wrap this as a tarball or serve discrete objects; layout stays the same either way.

### Signing
- **Detached signatures** over artifacts (and/or manifest that lists hashes).
- M1: signing **disabled by default** or driven by **local test keys** (gitignored); release keys later.
- Verifier used at download/install time (M4+). Secure Boot / TPM are explicit follow-ons.

### `make run-qemu`
- Kernel + SquashFS + serial console only.
- Full A/B disk and GPU validation on **real hardware** (Broadwell Xeon-D + NVIDIA first; AMD later). Dev VM (`lemonade.corddry.net`) is insufficient for GPU integration tests.

---

## 9. Security posture (v1 direction)

| Layer | Approach |
|-------|----------|
| Transport / update authenticity | HTTPS + **detached signatures** on artifacts |
| Root filesystem | Read-only SquashFS; no overlay |
| Mutable state | Minimal btrfs binds; restrictive mount flags |
| Privileged ops | Isolated control-plane daemon vs UI |
| Boot trust / TPM / Secure Boot | Deferred |
| Driver redistribution | All-in-image for simplicity; revisit if licensing blocks redistribution |

---

## 10. Proposed repo layout (M1+)

```
.
├── Requirements.md
├── DESIGN.md                 # this file
├── Makefile
├── flake.nix
├── flake.lock
├── partitions.yaml           # layout source of truth (used heavily from M3)
├── nixos/
│   └── configuration.nix     # appliance module(s)
├── scripts/                  # versioning, packaging, qemu run helpers
├── keys/                     # optional local test keys (gitignored contents)
├── control-plane/            # Python stub from M2
└── out/                      # build outputs (gitignored)
```

Exact NixOS module split can evolve; keep hardware/GPU, docker, and control-plane as separate modules early.

---

## 11. Open questions / deferred decisions

Resolved for early milestones where noted; remaining items do not block M5 start on NVIDIA HW:

1. **Initrd:** soft goal of none; still shipping initrd practically.
2. **Exact GPT type UUIDs** for A/B SquashFS — linux-generic in use for bring-up.
3. **Lock-in success criteria** — OS + control plane (+ Docker) for now; **not** gated on Lemonade model load (M5 decision).
4. **`/var` mount flags** — validate empirically under Docker/Lemonade.
5. **Python vs Go** — Python for CP through M5.
6. **HTTP channel details** — versioned dirs + `manifest.json` (current CDN path).
7. **NVIDIA open module + nixpkgs kernel pin + CUDA `llama-cpp`** — **blocks M5 GPU tests**; lock a known-good combo on the NVIDIA prototype before catalog load work.
8. **Model download timing** — **background after setup** (M5 decision).
9. **Encryption** — deferred past M5; document a later LUKS/migration plan before friends-and-family data.
10. **ComfyUI vs Lemonade sd-cpp** — ComfyUI preferred when image gen lands; skip images in M5.
11. **Heterogeneous GPU planner** — inventory-ready schema now; scheduling later.
12. **`lemond` packaging** — from-source Nix derivation (nix-amd-ai patterns); whether to flake-input `nix-amd-ai` later for AMD only, or vendor a slim copy.

---

## 12. Decision log (summary)

| Topic | Decision |
|-------|----------|
| Product | Local multi-user home AI hub; Concept.md is product source |
| First coding milestone | Artifacts only (M1) — done |
| Arch | x86_64; ARM later |
| Firmware | EFI-only for now |
| Bootloader | GRUB EFI |
| A/B | GRUB `grubenv` one-shot + lock-in |
| Root | SquashFS via `dd`; no overlay |
| User-data | Installer creates LVM PV; initrd/stage-2 provisioner creates VG/LV/btrfs |
| Mutable state | btrfs subvols `@etc`/`@var` mounted on `/etc`/`/var` |
| `/tmp` | size-capped tmpfs (128M) |
| ESP | On-demand mount only |
| Apps / modules | Not baked into image; Docker at runtime (Open WebUI, etc.) |
| Inference runtime | **`lemond` embedded in SquashFS** (headless); Docker is not the Lemonade path |
| GPU | Drivers in base; NVIDIA first (M5); AMD/UMA later; no VRAM pooling |
| Inference UX | Catalog-selected LLM; chat-only in M5; no Lemonade UI |
| Models | Stored under `/var/lib/lemonseed/`; versioned catalog fit |
| Lemonade packaging | From-source Nix; wire Nix backends; patterns from nix-amd-ai |
| Control plane | systemd root; Python; privileged API + web UI |
| Builds | Local; flakes; `build-production` / `build-qemu` / `run-qemu` / `qemu-clean` |
| Version | Date + local counter; `VERSION` override |
| Partition config | YAML source → systemd-repart as apply engine |
| Signing | Layout ready; detached sigs; optional/local keys in M1 |
| Distribution | HTTP; versioned artifact directory |
| Installer | M4; partitions only (no btrfs format); reinstall releases LVM/DM |
| Encryption | Deferred past M5 |
| Open WebUI / auth | Post-M5 (gateway Phase 2) |

---

## 13. Review checklist

Historical M1 checklist (kept for archive):

- [x] M1 scope (artifacts + QEMU `run`)
- [x] EFI-only GPT layout (ESP + A + B + user-data)
- [x] YAML → systemd-repart approach for partitions
- [x] No-overlay / btrfs bind-mount model
- [x] Signing optional/local for M1 but always present in layout
- [x] Python control plane direction

**M5 review (please confirm or correct):**
- [x] NVIDIA prototype is the M5 pass/fail host
- [x] Chat-only + catalog selection is the right M5 cut
- [x] Background model download after setup (not blocking Finish)
- [x] Soft A/B lock-in (no model-load gate)
- [x] Encryption deferred past M5
- [x] `/var/lib/lemonseed/` for models/state
- [x] Headless `lemond` in SquashFS (not Docker; no Lemonade UI)
- [x] Tier A unit tests (`make test`)
- [x] M5 closed — see commit on `main` (hardware-aware local chat + tests)

**M6 review (open):**
- [ ] UX evaluation spike completed
- [ ] Primary path chosen (Open WebUI vs agentic messaging)
- [ ] Gateway Phase 2 scoped to chosen module only
