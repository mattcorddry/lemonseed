# Testing strategy

## Why this document exists

Every phase of testing is a trade between two goals:

- **(a)** find new defects as early as possible — a defect found in a 25-second
  unit run costs minutes; the same defect found during a release drill costs a
  day, and the same defect found by a user costs trust;
- **(b)** preserve a fast cycle time for the test/fix iteration — the loop that
  actually fixes defects is *run test → read failure → change code → rerun*, and
  anything that makes one lap slower makes every fix slower.

The levers are different at each level, and the point of this document is to
make the boundary between levels explicit so we neither duplicate effort nor
slow ourselves down. Two rules follow from the trade:

1. **A defect found at a higher level gets pushed down.** When the QEMU smoke
   or a release drill finds a defect, the fix is accompanied — where possible —
   by a cheaper-level test that would have caught it, so the same class of
   defect is found earlier next time. This is the main lever on (a).
2. **Every phase that can be automated, is.** A big lever on (b) is test
   systems an agent can run without a human in the loop: the agent can iterate
   (fix → rerun) on its own, and a test that needs a human is a test that runs
   rarely enough to rot. The phases below are labeled by who can run them.

This document is the strategy; `AGENTS.md` §1–5 is the sprint protocol that
applies it.

## The matrix

| # | Phase | Runs | Agent-runnable | Speed | Catches | Must not try to catch |
|---|-------|------|----------------|-------|---------|-----------------------|
| A | Unit tests | `make test` | ✅ | ~25 s | Logic, state machines, config render, migrations | Anything needing real I/O — the fakes are the contract |
| B | Block-device loopback smokes | `make update-test` · `sudo make repart-test` (both need passwordless sudo; block-device ops only) | ✅ | minutes | GPT/repart logic and the A/B layout against a real kernel view of a real (loop) disk | Real NVMe, real UEFI, real ESP refresh |
| C | Gateway error contract | `make test-gateway` | ✅ | seconds | The 5xx/error_page/nginx contract against a real nginx | Anything behind the gateway |
| D | QEMU smoke | `make build-qemu && make test-qemu` | ✅ (long under TCG) | 10–40 min | The appliance contract end to end: CP, portal, nginx gates, containers, data plane, signing | Real LAN identity, real disk layout, real firmware, GPU |
| E | VM smoke *(proposed)* | `make test-vm` via the Proxmox API | ✅ | minutes | Real LAN identity, real disk layout, real UEFI, the installer ISO, A/B update with real reboots | GPU inference (CPU-only host); thermal behavior |
| F | Release drill (human) | `documentation/RELEASE.md` part 4 | ❌ (signing hardware) | hours | The real release chain: signing key, channel resolution, rollback on real hardware | Defects the lower phases should have caught |
| G | Bare-metal checkpoints | `M*/PLAN.md` BM-1…4 | ❌ | hours | The GPU paths (gfx1151 Vulkan, CUDA), thermal, real NVMe, real UEFI | Repeatability — run on milestones, not per commit |

## Phase reasoning

### A — Unit tests (`make test`)

The fastest lap possible: ~25 s, offline, no root, no hypervisor — the loop a
fixing agent can run unattended. It proves **logic**: the lifecycle state
machines, the config renders, the migrations, the parsers. Its boundary is the
fakes: Docker's argv surface, the DB rows, the catalog are handed in because
the *spec* is the contract, not the container. A defect that only exists in the
interaction between two real components is invisible here, by design — that is
what phase D (and E) are for.

Two things ride along: the runner first runs `scripts/check-untracked.sh`, so a
broken build-input state (a new file Nix has not seen) fails at the cheapest
level; and the control-plane suite runs **every module's self-harness**
(`modules/*/test/test.sh`) — the no-Docker, no-registry contract a new Lemonseed
release runs against every module before it ships.

`make lint` is deliberately outside `make test` (ruff comes from nixpkgs and
needs the network): `make test` must stay offline, so lint is part of the
pre-commit gate instead.

### B — Block-device loopback smokes

The A/B layout and the repart definitions are disk-shape-dependent, and unit
tests fake the disk. A loop device gives the real kernel — the real partition
table, the real mount of a FAT ESP — without hardware. These are slower than
the unit lap and need loop devices (both scripts use passwordless sudo for
block-device operations only), so they
run **when the update/repart code changes**, not every sprint. They must not
pretend to test real NVMe (TRIM, power states, the real UEFI boot of a real
ESP): that is phase E/F/G territory.

### C — Gateway error contract

The gateway's 5xx/error_page contract (`/_lemonseed/cp-down` is `internal`,
the not-ready page is not directly reachable, the 500 carries the right body)
is nginx configuration behavior that no unit test can execute. This phase runs
a real nginx from nixpkgs against the generated config and nothing else — a
seconds-long phase that exists because a wrong error contract is invisible
until a user sees a blank page. Kept out of `make test` (it needs the network).

### D — QEMU smoke

The appliance contract, end to end, automated: boot the **appliance-qemu**
profile, then probe `/healthz`, `/api/status`, `/api/boot`, `/api/data`, the
Open WebUI lifecycle, the module lifecycle (install → enable → disable →
settings → both remove paths), the signing smoke, the M6.5/M7/M8 surfaces,
through the real nginx with the real gates. This is the most complete
automated phase, and the one a fixing agent iterates against when the change
touches boot, gateway, CP HTTP, the data plane or modules.

The QEMU profile preloads the pinned Postgres/Valkey images and loads Open
WebUI from a host virtio-fs export so the smoke never depends on registry
pulls inside the guest — the smoke must be able to run in an offline sandbox,
because (a) is defeated if the test itself is flaky.

**What QEMU structurally lies about** — and why a second VM phase exists:

- **LAN identity.** User networking is NAT; the smoke fakes the Host header
  with `--resolve` and never resolves `lemonseed1.local` the way a real browser
  on a real device does. mDNS advertisement, cookie-domain behavior against a
  real DNS name, the gateway's TLS story are approximated. One such lie is
  already documented in the smoke itself (`/settings/` 403s in QEMU but works
  on hardware — a Host/cookie-domain interaction).
- **Disk and installer.** The userdata image is synthetic; systemd-repart on a
  real GPT, the ESP refresh, TRIM are loopback-approximated. The **installer
  ISO** has no automated test point in QEMU at all.
- **Firmware and GPU.** OVMF is not the real UEFI; QEMU has no AMD GPU, no
  NVIDIA GPU, and under TCG it is slow enough to force large timeouts.

### E — VM smoke (Proxmox API)

A narrow phase, deliberately: it exists to close exactly the Phase-D lies, and
not to be a bigger QEMU. A real VM on a real bridge gives the appliance a real
LAN identity (mDNS, cookie-domain, a real client), a real disk layout, real
UEFI, and a bootable **installer ISO**; near-native (KVM) speed makes the long
e2e (model pull/load, GC eviction) practical in normal runs instead of gated
behind an opt-in flag; and the Proxmox API's snapshot/revert/reboot makes the
**A/B update with real reboots** cheaply rerunnable — the one thing loopback
cannot do.

The release drill's `lemonseed-update check --url` mechanics (RELEASE.md part
4) become automatable here: the drill's URL-serving half runs against a VM
instead of a bare-metal box, which is why this phase pays for itself.

The harness is `scripts/vm-test.py`, reached with `make test-vm` (opt-in like
D, and only after D passes).

Design constraints, recorded so they are not rediscovered:

- **The allowlist is the phase's first invariant.** The host serves real
  workloads — the 192 GiB `llama` VM is the box that runs this agent, and
  `circ-dataplane-test` (id 120, 192.168.1.82) is a **different project's**
  test VM built by Hermes in another thread, never touched — so the harness
  refuses any VM id or name outside `LEMONSEED_VM_ALLOWLIST` (a
  comma-separated set of numeric ids, or a name pattern like `lemonseed-*`)
  and any storage outside `LEMONSEED_STORAGE_ALLOWLIST` (default
  `local-zfs`, the only pool with the free space). The check runs before
  every state-changing call — create, start, stop, delete, resize, config —
  and fails loudly rather than guessing. The pattern `lemonseed-*` is the
  whole default: a specific numeric id is an explicit, deliberate choice.
- **Do not duplicate the smoke.** The VM phase runs the QEMU smoke suite
  **unchanged** through two small hooks: the suite's base URL is
  `http://127.0.0.1:19080` (the QEMU port-forward), so the harness forwards
  that local port to the VM's :80; and `SMOKE_SKIP_BOOT=1` skips the suite's
  boot harness (the artifacts, the QEMU start, the signing sets) so the
  assertions run against a live appliance. The assertion library is the same
  code the QEMU smoke runs; a refactor that splits them into a
  boot-harness + a library remains the long-term shape, but for now the
  hooks are enough to reuse the suite verbatim without maintaining two
  copies.
- **GPU inference is out of scope by construction.** The Proxmox host has no
  spare GPU, so the VM is a CPU-only box — which is also a useful *negative*
  test point: with no GPU, `/v1/system-info` reports none and the model planner
  picks the CPU path. The AMD and NVIDIA inference paths remain bare-metal
  only (phase G, BM-2/BM-4).
- **The appliance will actually appear on the LAN** and try to claim an mDNS
  name — either an isolated bridge/VLAN or a rename at install time, or
  accepted transient presence, documented either way.
- **No virtio-fs on Proxmox**, so the OWUI preload needs a second disk
  carrying the image tar, or the VM pulls from the registry (slower, acceptable
  for the long e2e).
- **Credentials:** the Proxmox token arrives by environment variable, never in
  the repo — the same hygiene as every other secret.
- **Not in `make test`:** the phase needs a hypervisor host and the network, so
  it is its own target, opt-in like D.

The harness runs the phase end to end, in five steps:

1. **The ISO reaches the node by URL, not by upload.** The node pulls it via
   `POST …/storage/local/download-url` from the dev box's `dev-httpd` server —
   no base64 upload through the API, no SSH on the node. The privilege the
   node needs for this is `Datastore.AllocateTemplate` on the storage (the
   API's 403 names it when missing).
2. **`vm-test.py create`** builds the VM: 4 cores, 16 GiB, a 40 GiB disk on the
   storage allowlist, `vmbr0` (the internal network, where the dev box is —
   the mDNS claim must be on the same network the finder resolves it from;
   `vmbr1` is PROD) so the LAN identity and the mDNS claim are both reachable,
   the ISO as the boot
   medium. The id is allocated in the 200–219 range so it can never collide
   with a real workload; the name is always `lemonseed-vm-…` so the allowlist
   pattern covers it. 16 GiB because the ISO's live `/var` is a tmpfs at 50%
   of RAM, and the release set's 4.3 GiB payload must fit while the installer
   extracts it.
3. **`vm-test.py install`** boots the ISO, finds the installer via mDNS
   (`lemonseed.local`), and drives the web installer's own API: `GET /api/disks`
   → `POST /api/install` with `channel=dev` and `source_url` pointing at the
   dev box's artifact server → wait for the install → `POST /api/reboot`.
4. **`vm-test.py smoke`** waits for the installed image's portal, starts a
   local TCP forward from `127.0.0.1:19080` to the VM's :80, and runs the
   QEMU smoke suite unchanged with `SMOKE_SKIP_BOOT=1`.
5. **`vm-test.py delete`** tears the VM down; the disk is deleted with it.

The e2e's 14–16 GiB model load fits in this footprint, so the e2e is an
explicit opt-in (`--e2e-chat`) because it is the long pole (~20 min per
model pull/load), not because the host can't afford it.

### F — Release drill (human)

`documentation/RELEASE.md` part 4, per release: production-signed release set,
`make release-verify` against the staged bytes, update a **bare-metal box
running the current stable release from GitHub**, exercise it, then roll back
via the GRUB boot menu. This is the one phase that cannot be automated — the
signing hardware's authorization cannot be bypassed, and the drill's point is
to prove the chain (key → manifest → channel → apply → rollback) exactly as a
user would experience it. Everything the drill finds that a lower phase could
have caught gets pushed down (rule 1); the drill is the last line, not the
first.

### G — Bare-metal checkpoints (BM-1…4)

Human-operated hardware, per milestone checkpoint. The only place the GPU
paths exist (gfx1151 Vulkan on Strix Halo, CUDA on NVIDIA), along with thermal
behavior, real NVMe, real UEFI and model switching in the panel. Run on
milestones, not per commit: bare metal is the slowest lap, and every defect
that can be found in D or E should be found there first.

## Cadence

| When | Phases |
|------|--------|
| Every commit that changes code | A (+ lint before the commit) |
| End of every sprint | A, plus D when the change touched boot, gateway, CP HTTP, the data plane or modules |
| Update/repart/signing code changes | B |
| Gateway config changes | C |
| Per release | A → B → D → E (when it exists) → F → G if a GPU path changed |
| Per hardware milestone | G (BM-1…4) |

## Escalation when a phase finds a defect

Fix at the source, verify at the level that found it, and add a cheaper-level
test that would have caught the defect (rule 1). A defect found only in the
QEMU smoke but fixable with a unit test against fakes is a *good* outcome: it
means the boundary between the levels is doing its job.
