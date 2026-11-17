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

1. **A defect found at a higher level gets pushed down.** When the appliance
   smoke
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
| D | Appliance VM smoke (libvirt) | `make build-installer release && make dev-httpd-start` then `make test-vm VM_STEP=…` | ✅ | ~15–30 min | The appliance contract end to end on real KVM: installer ISO, first boot, CP, portal, nginx gates, containers, data plane, signing — plus a real UEFI boot and a real DHCP lease for the LAN identity | GPU inference (CPU-only host); thermal behavior |
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
what phase D is for.

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
ESP): that is phase D/F/G territory.

### C — Gateway error contract

The gateway's 5xx/error_page contract (`/_lemonseed/cp-down` is `internal`,
the not-ready page is not directly reachable, the 500 carries the right body)
is nginx configuration behavior that no unit test can execute. This phase runs
a real nginx from nixpkgs against the generated config and nothing else — a
seconds-long phase that exists because a wrong error contract is invisible
until a user sees a blank page. Kept out of `make test` (it needs the network).

### Requirements

- **libvirt with the KVM hypervisor on the dev box** (`libvirtd` running, the
  `default` NAT network defined and active). The VMs live there; nothing
  touches any other host's hypervisor.
- **The libvirt Python bindings** (`python3-libvirt`) — the harness talks to
  libvirt natively (`import libvirt`), never through `virsh` callouts. If the
  socket refuses with *Permission denied*: your membership in the `libvirt`
  group was added after this session started, so log out and back in (a running
  desktop session keeps its old supplementary groups).
- **The artifact server** (`make dev-httpd-start`, serving `out/` on
  `192.168.0.83:8083`) — the installer downloads the release set over HTTP from
  it, exactly as a real install would pull from a channel.
- The smoke suite itself needs nothing extra; it runs unchanged through its
  own `SMOKE_SKIP_BOOT` hook (below).

### D — Appliance VM smoke (libvirt)

The appliance contract, end to end, on one block: define + boot a VM under
this host's libvirt from the installer ISO, drive the web installer's own API
to install, then probe `/healthz`, `/api/status`, `/api/boot`, `/api/data`,
the Open WebUI lifecycle, the module lifecycle (install → enable → disable →
settings → both remove paths), the signing smoke and the M6.5/M7/M8 surfaces,
through the real nginx with the real gates — all on real KVM at near-native
speed, which is what makes the long e2e (model pull/load) practical in normal
runs instead of gated behind an opt-in.

This phase replaces the two blocks it used to be: the managed-QEMU boot
harness (`make test-qemu`, TCG emulation, 10–40 min a lap) and the Proxmox VM
phase that existed to close QEMU's lies. KVM on this box closes most of those
lies *and* is fast: real UEFI (OVMF), a real disk layout under the installer,
a real DHCP lease for the LAN identity. What remains hardware-only — GPU
inference, thermal behavior, real NVMe — stays with phase G.

**The harness is `scripts/vm-test.py`, reached with `make test-vm` (opt-in:
it needs the hypervisor and the network). Its five steps:**

1. **`create`** builds a qcow2 disk volume and uploads the installer ISO into
   the default storage pool via the bindings' stream API, then defines the
   domain from XML: 4 vCPU, 16 GiB (the ISO's live `/var` is a tmpfs at 50% of
   RAM — the release set's 4.3 GiB payload must fit while the installer
   extracts it; an 8 GiB guest ran out at 45%), UEFI (`firmware='efi'`
   autoselection), and boot order **hd before cdrom** — so an empty disk falls
   through to the ISO for the install, and after the install the same order
   boots the installed image with no post-install boot rewrite.
2. **`start`** boots the domain and waits for the DHCP lease: the VM's IP is
   read from `net.DHCPLeases()`, keyed on the NIC MAC from the domain's own
   XML. This replaces mDNS discovery outright — a lease maps one machine to
   one IP, while an mDNS name answers with whichever box claimed it last (a
   bare-metal box on the LAN once answered as `lemonseed.local` and every
   assertion against "the VM" looked green while hitting that other machine).
   The install step records this lease IP in `out/vm-state.json`, and later
   steps use **only** that recorded IP.
3. **`install`** waits for the installer to answer on the lease IP, then drives
   its own API: `GET /api/disks` → `POST /api/install` with `channel=dev` and
   `source_url` pointing at the artifact server → wait for the install →
   `POST /api/reboot`.
4. **`smoke`** runs the smoke suite unchanged against the recorded IP: a local
   TCP forward from `127.0.0.1:19080` to the VM's :80, `SMOKE_SKIP_BOOT=1`
   skipping only the suite's boot harness so the assertions hit the live
   appliance.
5. **`delete`** tears the domain down and deletes its disk volume; the ISO
   volume is reused across runs (replaced only when its size changes).

**Allowlist invariant, kept from the Proxmox design:** the harness refuses any
domain outside `LEMONSEED_VM_ALLOWLIST` (default `lemonseed-*`) before every
state-changing call, and only ever creates volumes named after an allowed
domain. A hypervisor is a shared resource; the check fails loudly rather than
guessing.

**mDNS on the NAT segment is tested directly, not assumed.** The host's system
avahid may not listen on `virbr0` at all (and configuring it to do so is out of
scope), so `vm-test.py mdns-probe` proves the appliance's advertising works:
it multicasts a hand-built mDNS query (`lemonseed.local` A plus
`_http._tcp.local` PTR) onto the bridge and decodes every response with a
minimal DNS name reader — responders, names and IPs dumped, failure if nothing
answers. No daemon reconfiguration, no new dependencies; run it while the VM
is up: `make test-vm VM_STEP=mdns-probe`.

**A/B update with real reboots stays cheap here:** libvirt snapshots
(`snapshot-create-as` / `snapshot-revert`) on the qcow2 make the rerunnable
update path a follow-up, not a new harness.

The e2e's 14–16 GiB model load fits in this footprint (a 48 GiB host runs the
16 GiB guest alongside everything else), so the e2e is an explicit opt-in
(`--e2e-chat`) because it is the long pole (~20 min per model pull/load), not
because the host can't afford it.

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
| Per release | A → B → D → F → G if a GPU path changed |
| Per hardware milestone | G (BM-1…4) |

## Escalation when a phase finds a defect

Fix at the source, verify at the level that found it, and add a cheaper-level
test that would have caught the defect (rule 1). A defect found only in phase D
but fixable with a unit test against fakes is a *good* outcome: it
means the boundary between the levels is doing its job.
