# M8 — Beta readiness: signed GitHub releases, release channels, and data-at-rest security

Draft for approval, revised 2026-10-19 (second review pass). `DESIGN.md` is the
as-built product contract; this document is the **sprint pull** from `BACKLOG.md` and
from M7's "Split out to M8" section. Decisions D1–D26 at the bottom record everything
settled before drafting, so nothing gets re-litigated mid-build.

M7 closed 2026-10-19 with P1–P5 shipped and bare-metal verified (BM-1 through the
final update-flow pass; `make test` 952 OK, lint clean, QEMU PASSED,
`make build-production` clean). M8's job is what remains: turn an appliance that has
only ever run on our own boxes into one an **external beta tester** can install from
USB, update over the network, and trust.

**Definition of beta-ready for this milestone:**

- Install from USB → setup → update from GitHub over a **stable** or **beta** channel
  with **fail-closed signature verification** — the appliance never applies a payload
  whose manifest signature does not verify.
- A failed update falls back to the previous slot (M7 P5's detector), and a deliberate
  rollback returns to **N-1** — tested in QEMU and drilled on every release.
- Physical access to the disk yields nothing: encrypted user-data with keys sealed to
  the TPM, a GRUB password, and no empty-root console. **The household never needs a
  KVM on the appliance** — every recovery path works from the boot menu or the web UI.

## Scope and boundary

| Phase | Title | Design gate | Bare metal |
|-------|-------|-------------|------------|
| P1 | Signing root of trust (key ceremony, verifier, fail-closed) | **yes — key custody and rotation before any key exists** | no |
| P2 | Packaging: sharded payload, installer size gate, `make release` | no | no |
| P3 | Update & install channels: GitHub discovery, `--channel`, `--url`/`--dev` | no | — |
| P4 | CI build & signing pipeline + `RELEASE.md` | no | BM-1 |
| P5 | Release channel + release notes in the control plane | no | (BM-1) |
| P6 | N-1 rollback: slot version tags, GRUB/CLI path | no | (BM-1 drill + BM-2) |
| P7 | Encrypted user data + console lockdown | **yes — the migration plan, installer UX, and recovery story** | BM-3 |

**Phase gate (M7's, unchanged).** A phase ends when `make test` **and**
`QEMU_API_TIMEOUT=… make test-qemu` are both green, its new tests exist, and its
obsolete tests are deleted. No phase starts before the previous one passes. Bare-metal
checkpoints are yours; each is flagged.

**Explicitly deferred (not in this milestone):** TLS on the gateway, TOTP/MFA, Secure
Boot, GitHub artifact attestations (CI-side provenance only, and only once the repo is
public), a rollback *UI*, rollback schema-boundary safety checks (backlogged), offline
installs, installer stream-to-disk, the AMD dGPU bare-metal validation, and the modules
framework (M9). All remain in `BACKLOG.md`.

---

## P1 — Signing root of trust

**Status: shipped 2026-10-19.** `SIGNING.md` approved with its three decisions
resolved; release key v1 generated on the build/signing box; `make test` green,
`make lint` clean, `QEMU_API_TIMEOUT=1200 make test-qemu` PASSED including the new
signing smoke (signed set verifies; flipped payload byte refused at the hash check;
altered manifest refused at the signature). Design record:
[`SIGNING.md`](./SIGNING.md).

### Problem

`DESIGN.md` §8 states it plainly: *"As built, nothing verifies a signature. Writing and
wiring the verifier is M8's first job, not a formality."* `scripts/sign.sh` writes
`.sig` files (ed25519 via `openssl pkeyutl`, keys from `scripts/gen-keys.sh`, gitignored
test keys) and `scripts/package.sh` records
`"signing": {"scheme": "detached-ed25519", "enabled": false}` — but no code in
`scripts/`, `installer/`, `control-plane/` or `nixos/` reads a `.sig`, and no public key
ships in the image. The manifest's SHA-256 values come from the same origin as the
blobs, so the existing download + read-back hash chain proves **transfer integrity,
not authenticity**. On plain HTTP with the release channel moving to GitHub, anyone who
can stand between the appliance and the release (or compromise the repo) can serve a
malicious `root.squashfs` that passes every check we run today.

### Proposal

- **Key ceremony on the signing box (D11, D15).** One long-lived ed25519 keypair,
  generated **on** the dedicated CI/build/signing box, private key never leaves it.
  Signing is a modular step with a single interface (`sign.sh <dir>` behind an env
  switch), so a hardware-backed signer (YubiKey/PKCS#11, tap-to-sign) can replace the
  file key later without touching the verifier or the pipeline.
- **The public key ships in the image (D13).** A Nix module bakes the public key into
  the read-only SquashFS; the installer image carries the same key. The verifier trusts
  exactly the key(s) baked into the running image — nothing fetched at runtime. The key
  is also published on lemonseed.ai as out-of-band human confirmation, never as a
  runtime trust source.
- **Verify before trusting.** Both consumers — `lemonseed_update.py` (`check`/`apply`)
  and `installer/install.py` — fetch `manifest.json` **and** `manifest.json.sig` and
  verify the signature *before* reading a single hash out of the manifest. Only then do
  the manifest's SHA-256 values become the integrity chain for artifacts and parts
  (D12). Verify-before-write ordering is preserved: signature → manifest hashes →
  per-part/per-artifact download verification → partition write → read-back.
- **Fail-closed in production (D10).** A missing, malformed, or invalid signature is a
  hard refusal with a legible reason ("release signature does not verify — refusing").
  There is no hash-only fallback. `--dev` (P3) is the *only* bypass, and it is CLI-only;
  the control plane's install path is always production-strict.
- **Signing scope (D12).** The detached signature over `manifest.json` is the single
  root of trust; everything else is covered by the manifest's SHA-256. `sign.sh`
  therefore trims to signing the manifest alone (today it signs all four artifacts) —
  one root, not two.
- **Rotation is designed, not exercised (D13).** Trust set = keys baked into the
  running image. Rotation = a new image signed by the old key embeds the new key; the
  verifier accepts *any* trusted key during the overlap window. The design goes in the
  key-ceremony document; the mechanism can wait until a second key exists.
- **GH-compromise posture, stated in §9:** an attacker with GitHub write access can
  publish releases and flip channel labels, but cannot produce a valid manifest
  signature. Worst case is selecting among *our* signed releases (downgrade/upgrade
  confusion), never arbitrary code.

**Design-gate deliverable:** `SIGNING.md` — key generation, the modular
signer interface, rotation, and the threat model. Key **custody and escrow are decided
and held privately**; `SIGNING.md` records only that escrow
exists (D-escrow). The key is generated only after this is approved; generating it
first is a one-way door.

### Tests

- Verifier unit tests: good signature; wrong key; missing `.sig`; tampered manifest
  byte; signature valid but manifest altered after signing (mismatch); truncated sig.
- Fail-closed assertions: every production path refuses unsigned/invalid; `--dev`
  bypasses, and no non-`--dev` path can reach an apply.
- Known-answer test for the openssl ed25519 verify path against a fixed key/vector.
- QEMU: updater against a locally served, correctly signed artifact set (passes), and
  against the same set with a flipped payload byte (refuses, legible reason).

### Gate

`make test` + QEMU green; the shipped manifest carries
`"signing": {"enabled": true}` and the image contains the public key; QEMU shows both
the pass and the refuse paths. **Key ceremony doc approved before key generation.**

---

## P2 — Packaging: sharded payload, installer size gate, `make release`

**Status: shipped 2026-10-20.** `PACKAGING.md` approved with its decisions resolved;
`make test` green (incl. the 13-test `test_release_payload.py` suite), `make lint` clean,
`QEMU_API_TIMEOUT=1200 make test-qemu` PASSED — the guest consumes a real sharded set
(`pack` + `sign.sh`, QEMU trust set) end-to-end over `--url`: signed set verifies, a
flipped payload byte is refused at the part hash check, an altered manifest is refused at
the signature. `VERSION=20261020.05 make release` staged a complete signed release set
(4 payload parts incl. a 16 MiB remainder + 1.3 GiB installer ISO) and self-verified it
with the appliance's verifier. Fixes found by the gates: the build manifest's JSON had a
trailing comma (only `SIGN=1` parses it — `jq` refused), `web.nix` called
`release-keys.nix` without `lib`, and the signing smoke's host block passed a `str` where
the packer needed a `Path`. `scripts/push-image-to-website.sh` removed (D23). Design
record: [`PACKAGING.md`](./PACKAGING.md).

### Problem

GitHub caps each release asset under **2 GiB** and `root.squashfs` is ~3 GB, so
sharding is mandatory, not an optimization (BACKLOG, GitHub-releases entry). The USB
installer image is under the cap today (D16) but nothing asserts it. There is no
`make release`; publishing is a hand-run SCP (`scripts/push-image-to-website.sh`,
removed in this iteration — D23).

### Proposal

- **Canonical tar → fixed parts.** All appliance payload artifacts (`vmlinuz`,
  `root.squashfs`, `initrd.img`) go into one canonical tar —
  `tar --format=pax --sort=name --mtime=@0 --owner=0 --group=0 --numeric-owner`, fixed
  file order — cut into fixed **1 GiB** parts named with conservative ASCII
  (`payload-000.part`, …), stable across releases so a future updater can keep parts it
  already has and refetch only the ones whose SHA changed. The final part of a release
  is a remainder, which is expected. Direct-asset shipping for small artifacts was
  rejected: one assembly path, always exercised.
- **`manifest.json` stays outside the tar** and is both the assembly plan and the root
  of trust (signed, P1): it describes the reassembled **outputs** (name, length,
  SHA-256) *and* the **chunks** (name, order, length, SHA-256). The USB installer image
  ships as its own direct asset and is recorded in the manifest the same way, so its
  hash is signed too (D16).
- **Install order, enforced:** verify manifest signature → verify each part →
  concatenate → verify the tar → untar to staging → re-verify every extracted file
  against the manifest → only then flip the A/B slot. Download resume is *checkpoint
  after each verified part* — GitHub documents no `Range` support for release assets.
- **Build-time size gate (D16):** the packaging step fails the build if any would-be
  release asset exceeds 2 GiB. That covers the installer image today and any future
  artifact tomorrow.
- **`make release` (local):** build → package → sign → stage the complete, upload-ready
  asset set under `out/release-<version>/`. Publishing is P4's job; this target is the
  single place the asset set is defined.

### Tests

- Byte-determinism: identical inputs → byte-identical parts (two runs, `cmp`).
- Round-trip: reassemble → verify tar → verify outputs → compare against originals,
  including a short final (remainder) part.
- Refusals: corrupt part, corrupt tar, output hash mismatch, truncated download, part
  count mismatch.
- Size gate: a synthetic oversize artifact fails the build with the offending name.
- Updater `--dir` consumes a `make release` output end-to-end in QEMU.

### Gate

`make test` + QEMU green, including the QEMU `--dir` round-trip above.

---

## P3 — Update & install channels: GitHub releases

**Status: shipped 2026-10-20.** `CHANNELS.md` approved with its four decisions
(D-C1–D-C4; the flip is a GitHub metadata edit, no re-sign — see D28); `make test`
green (962 tests, incl. the discovery decision table, flag matrix, channel precedence
and switch-never-downgrades), `make lint` clean, `QEMU_API_TIMEOUT=1200 make
test-qemu` PASSED — the stub GitHub API resolves stable to the non-prerelease set and
beta to the newer prerelease, both acquired production-strict through the sharded
assembly path, with the poisoned-manifest refusal intact. `IMAGE_BASE_DEFAULT` left
both files; `make dev-httpd-start`/`-stop` added (D-C3); `cp_meta` settings wrapper
shipped (D-C4). Bare metal shares BM-1 with P4/P5 (below). Design record:
[`CHANNELS.md`](./CHANNELS.md).

### Problem

The channel is a flat HTTP directory on `dev-server` — hardcoded in
`scripts/lemonseed_artifacts.py` and `installer/install.py` (`IMAGE_BASE_DEFAULT`,
which must agree), served from an RFC1918 address, signature-less, and useless to anyone
off that network (D8). The updater (`status|apply|cancel|check`, `--url`/`--dir`,
`--reboot`, `--force`, `--record`) has no GitHub semantics and no channel concept; the
same is true of the installer.

### Proposal

- **Discovery (D5).** `GET /api.github.com/repos/mattcorddry/lemonseed/releases?per_page=10`
  (D-repo; the owner may change later, so the constant stays one line per file) with a
  distinctive User-Agent. **stable** = newest non-draft, non-prerelease release;
  **beta** = newest non-draft release, prereleases included. Publishing an RC as a
  GitHub *prerelease* is what keeps it out of stable; flipping beta → stable is clearing
  the prerelease flag **plus** a scripted `make release-channel` that re-signs and
  re-uploads that release's `manifest.json` with `"channel": "stable"`, so the signed
  manifest — not unsigned repo metadata — is the authority on what a release is
  (metadata tampering can then only reorder *our* signed releases).
- **Channel onboarding (D6).** The installer **asks** for the release channel at
  install time with **stable** as the default — some brand-new hardware may need beta —
  and `--channel=(stable|beta)` covers unattended installs. The answer seeds the **CP
  database setting** (P5) that the weekly check reads. Git tags are `v$VERSION`;
  release name is `$VERSION`.
- **Flags: source × trust (D8, D10).** `--url=BASE` is a source override and remains
  **production-strict** (the base must serve a signed manifest — that is how
  `dev-server` stays usable as a private mirror). `--dir=PATH` is unchanged for
  local artifact sets. `--dev` skips signature verification and all GitHub semantics and
  composes with both. `--dev` is CLI-only; the portal's install path is always
  production-strict.
- **De-hardcode (D8, D23).** Every hardcoded `dev-server` reference leaves the
  appliance and the installer; `IMAGE_BASE_DEFAULT` becomes the GitHub releases base in
  both files, which must agree (existing rule). `scripts/push-image-to-website.sh` is
  **removed** in this iteration — dev iterations run through `--url`/`--dir` against a
  local artifact set and never need a mirror.
- **Optional: `make serve-out` (D23).** A best-effort target that serves `./out` over
  HTTP on the LAN for local installer testing when a local nginx or Apache is
  available, and prints a skip message otherwise. Explicitly optional; nothing
  depends on it.
- **Rate limits (D26, cited).** Unauthenticated REST API requests are limited to
  **60 requests/hour**, and that budget is *associated with the originating IP address,
  not the user, application, or repository* — clients never burn a repo-owner quota, so
  adoption does not aggregate against us. Release-asset downloads are served from
  GitHub's CDN and do not count against the REST API at all. A check costs one
  `GET /releases` call, so the fuzzed weekly check plus manual checks sit orders of
  magnitude under the limit for any household IP; the one real scale risk is many
  appliances behind a single shared egress IP, which the existing heavy fuzzing and the
  weekly cadence already mitigate — and if adoption ever makes it relevant, a tiny
  static channel file on lemonseed.ai's CDN costs no API quota. We never poll.
  Source: GitHub Docs, *Rate limits for the REST API*, read 2026-10-19. Asset
  downloads need `Accept: application/octet-stream` and must follow `302`.
- **Registry reachability preflight (D3).** Keep improving the surfaced message and the
  retry/backoff so a dead WAN link never looks like a broken appliance; the message now
  covers the GitHub API as well as the registry.

### Tests

- Discovery decision table: no releases; only prereleases; newest-is-beta;
  newest-is-stable; draft ignored; malformed API answer; rate-limited (legible error).
- Channel filtering for both channels, and the flip: after `make release-channel …
  stable`, a stable-channel check finds the release.
- Flag matrix: `--url` alone (strict), `--dev` alone (default source, no verification),
  `--url --dev` (explicit source, no verification); `--dir` unchanged.
- Installer prompt: defaults to stable, accepts beta, and seeds the CP setting.
- QEMU against a stubbed GitHub API serving signed releases: stable-channel box never
  sees a prerelease; beta-channel box does; a poisoned release (bad signature) is
  refused before any hash is trusted.

### Gate

`make test` + QEMU green. Bare metal shares BM-1 with P4/P5: a real beta release
consumed production-strict on both test boxes, then flipped to stable and re-tested.

---

## P4 — CI build & signing pipeline + `RELEASE.md`

### Problem

There is no CI and no publishing path beyond the SCP script. GitHub releases hang off
git tags; asset uploads go to the release's `upload_url` as raw bodies; a mid-upload
`502` leaves an empty asset in `state: starter` (safe to delete) and re-uploading a
duplicate filename returns `422`. The repo is private today (`mattcorddry/lemonseed`);
the download path cannot be exercised unauthenticated until it flips (D9).

### Proposal

- **Dedicated build/sign box with a self-hosted runner (D15).** It pulls from GitHub,
  builds the release candidate, signs it (P1's interface — modular for a future
  hardware key), and uploads. The repo stays private until the pipeline is proven, then
  flips public so the unauthenticated download path can be tested end-to-end (D9). No
  token or credential ever ships in the image.
- **Release trigger (D14):** manual `workflow_dispatch` with the git tag as input —
  never a push. Checkout the **tag**, not HEAD; assert the tag equals
  `v$VERSION` (D7). This is the "label a HEAD checkpoint" process: the tag is the
  label.
- **Publish:** create the release (prerelease = beta), upload the asset set
  (`manifest.json`, `manifest.json.sig`, parts, USB installer image, installer
  hash/sig), tolerate the starter-asset/422 edges (delete-and-retry), then
  **self-verify**: re-download everything and run the same verifier the appliance runs.
  A release is not done until the publisher's own check passes.
- **Rollback drill rides the release (D21).** The per-release BM checklist — BM-1's
  script, run for *every* release, not just the first — includes: install/update over
  the channel, and one **N-1 rollback drill** (P6).
- **Retention (D16):** keep the newest **10** installer iterations per channel; pruning
  is an explicit scripted step, never silent.
- **Release notes:** generate a draft body (version, artifact list, hashes) for you to
  edit before publishing; the same body is what P5 surfaces to users.

### Deliverable: `RELEASE.md` (D22)

Developer-facing docs, at the repo root, covering: how a release is tagged, built,
signed, published, self-verified, flipped between channels, and rolled back; the
verifier's trust model; and the operator runbook for the build box. Key custody details
stay private — `RELEASE.md` records only that escrow exists and where the public key is
published.

### Tests

- Workflow dry-run mode: tag/VERSION agreement, asset-name ASCII check, upload set
  completeness (asserted from a listing, not from memory).
- Idempotent re-run: a second run after a mid-upload failure cleans starter assets and
  produces the same release.
- Self-verification failure blocks "done" (the workflow reports the release as failed).

### Gate

`make test` + QEMU green; `RELEASE.md` reviewed. **BM-1 (yours, shared with P3/P5):**
the pipeline publishes one real **beta** release from the box; both test boxes consume
it over the **beta** channel, production-strict; the release is flipped to **stable**
and both boxes update again; and the **rollback drill** runs. This needs the repo
public (D9) — accepted.

---

## P5 — Release channel + release notes in the control plane

### Problem

There is no user-facing channel concept; the weekly fuzzed check polls the flat
channel; and when an update is pending, the user sees a version number with no context.

### Proposal

- **Release channel setting.** A **Release channel** control in the SYSTEM panel —
  `stable` (default) | `beta` — admin-only, persisted as a **CP database setting**
  seeded by the installer's prompt (D6). The weekly fuzzed check and *Check now*
  resolve "latest" against it. Flipping writes an **audit log entry** and nothing more —
  no banner and no announcement; a beta box simply receives beta updates (D-flip).
  Everything downstream (red dot, arm, install job, failed-update banner) is M7 P5's
  machinery, unchanged.
- **Release notes surface (D-notes).** The check result already names the newer
  version; it now also carries the GitHub release body (fetched in the same `/releases`
  call). Next to the pending update, the panel renders a **"See release notes"** link
  that opens a read-only box with the notes — sanitized plain text, no HTML passthrough.
  An admin decides with context instead of a version number.

### Tests

- Setting persists and is admin-only; the check honors it; a beta-channel box sees a
  beta release, a stable-channel box does not; flipping is audited and nothing else
  changes.
- Release notes: link renders only when a pending update exists; the box renders
  sanitized text (script/HTML in the release body is escaped); members never see the
  panel.
- The public `/api/status` subset gains nothing new.

### Gate

`make test` + QEMU (panel renders, setting sticks, the check uses it, notes render).
Bare metal rides BM-1.

---

## P6 — N-1 rollback (as built)

### Problem

Nothing records which version is in which slot (M7 PLAN, "Split out to M8"); the
rollback primitives (`lemonseed-ab set-once`/`lock-in`) exist but are untested for this
purpose. M7 P5's arm-time `update-attempt.json` records the slot we just wrote, never
the one we boot.

### Design (D2/D30, as approved)

- **Slot-version registry.** `/var/lib/lemonseed/slot-versions.json`, written by every
  apply (target slot with the release's declared rollback floor; running slot
  self-healed), by the installer for slot a, read by the rollback verb and the control
  plane's panel: `{"slots": {a: {version, written_at, min_rollback_version?}, …},
  "rolled_back_version"}`. Bookkeeping, never a boot risk — unreadable reads as empty.
- **`lemonseed-update rollback [--reboot] [--force]`.** A boot-pointer flip through the
  tested `lemonseed-ab lock-in <inactive>` primitive — no download, no slot writes.
  Eligibility (both the CP panel's cheap gate and the root-side verb): knowable slot;
  nothing pending; the inactive slot holds exactly the prior release (a newer re-apply
  over it means nothing to flee to); and the prior sits at or above the running
  release's declared floor. Deliberately N-1: older releases return via an explicit
  `apply --force` at an older set.
- **`MIN_ROLLBACK_VERSION` (D30).** The floor rides inside the signed manifest
  (`rollback.min_rollback_version`, written by `release_payload.py pack` and by
  `package.sh` for the flat appliance set; `MIN_ROLLBACK_VERSION=<v>` through
  `release.sh` pins it for a one-way-door release). Default at pack time: the release's
  own date minus the 30-day horizon (`lemonseed_artifacts.ROLLBACK_HORIZON_DAYS`);
  legacy manifests without the field fall back to the same computed floor at rollback
  time. The horizon refusal is hard — `--force` is the CLI's explicit override.
- **GRUB labels.** `boot/grub.cfg` titles are "Boot Lemonseed version {version} (slot
  {slot})", expanded from grubenv vars (`slot_a_version`/`slot_b_version`) written at
  every apply; the installer seeds slot a's. In-config "unknown" fallbacks keep the
  menu legible on boxes installed before the registry existed.
- **Re-offer suppression.** `rolled_back_version` marks the version the operator fled
  to; `check` and the panel offer nothing at or below it until a strictly newer release
  is published. An explicit `apply --force` overrides it, and any successful apply
  clears the marker.
- **Web UI (scope added at review).** SYSTEM panel gains a "Prior version" row and a
  "Roll back to prior version" button, shown only when the registry says eligible and
  no failed-update banner is up; POST `/_lemonseed/api/update/rollback` runs the job
  (kind `rollback`, updater `rollback --reboot`), audited as `update.rollback` —
  panel actions audit; CLI actions log to journald only.
- **Caveat stated, not hidden:** a rollback undoes the *root*, never the data —
  anything a newer CP wrote to shared user-data (and an operator-triggered OWUI
  `config` TRUNCATE) is not undone by reverting the slot.

### Tests

- Every apply writes the slot version record (target + self-healed running slot); the
  GRUB entries name both slots.
- Unit: updater eligibility matrix, registry writes, suppression; CP eligibility
  matrix, snapshot prior/suppression, HTTP 202/409 + audit.
- Loopback smoke (`update-smoke-test.sh`): apply → cancel → **rollback** (boot_default
  flips to the prior slot, registry honest, grubenv labels render) → re-apply with
  `--force` (marker cleared).
- QEMU: signing + update smokes green (the rollback verb rides the same lock-in
  primitive those smokes exercise).

### Gate

`make test` + QEMU green. **BM-2 (yours):** a real rollback drill on a test box —
update, roll back, confirm the previous version runs and the post-rollback state is
honest — then repeat it on the next release to prove the checklist, not the milestone,
carries it.

---

## P7 — Encrypted user data + console lockdown

### Problem

User-data (the btrfs LV carrying `@etc`/`@var`, Postgres, Valkey, models, container
state) is unencrypted, root has an **empty password** and tty2 auto-logs-in a root
shell (§9: *"must be closed before general availability"*). A stolen box hands over
household data. D1 sets the bar: M8 ships to an **external** install base, so the
data-at-rest story must be real — and the product constraint is hard: **the household
never needs a KVM on the appliance** (D-KVM), so every recovery path must work from the
boot menu or the web UI.

### Proposal

- **LUKS on the user-data volume, TPM-sealed, TPM required (D24 — decided).** The
  installer requires a TPM 2.0 and fails with a legible preflight when one is absent
  or the seal cannot be established. The alternatives — (b) a web-based boot passphrase
  workflow and (c) a hardware-attribute-derived key — are backlogged as
  *Alternatives to TPM* until needed.
- **Migration:** existing machines migrate by USB reinstall with data loss — both test
  boxes are rebuildable (D9); there is no in-place encryption path. The installer gains
  the partitioning/format step.
- **GRUB lockdown, sized to the no-KVM requirement (D25).** The password protects
  **editing and the GRUB console only**; choosing among existing boot entries — the
  rollback path — stays unrestricted (`--unrestricted` entries). A household can roll
  back from the boot menu without a KVM, but cannot hack boot parameters.
- **Install-time hardware secrets (D25).** Since we never know the admin's *user*
  password (SRP — the server cannot hold it), the installer generates **random
  passwords at install time** for the GRUB superuser and the local tty root and stores
  them in the **CP database**.
- **The Hardware password (D27).** It is generated at install time — reusing the
  **N-words-with-dashes** generator already built for the non-admin password-reset
  workflow, so it is readable and writable by hand rather than opaque — then sealed
  into the TPM, stored in the CP database, and **presented during the new-install
  flow, at the point the LUKS partition is formatted**, with copy that says what it is
  and when it matters: *it may be required if the trusted platform module (part of the
  motherboard) is replaced or erased; if decryption fails, a reinstall reloads the
  password into the TPM.* The SYSTEM panel exposes the same secret as a **Hardware
  password** row under an **Advanced** section, with an info button explaining what it
  is, where it is used, and when to reach for it — so it does not read as the login
  password. It may double as the **LUKS recovery passphrase**, so the user writes one
  thing down and can still recover from a TPM failure.
- **Preserve-reinstall must ask (D27).** When the user reinstalls with *preserve
  user-data* and the TPM does not contain the Hardware password — or decryption with
  the TPM-stored seal fails — the installer **prompts for it** before touching the
  volume.
- **No rotation (D27).** The Hardware password does not rotate in this milestone;
  rotation is a backlog item alongside the TPM alternatives if it ever earns its keep.
- **Recovery floor (D27).** If the TPM seal is gone *and* the Hardware password is
  lost, the only path is **USB reinstall with data loss** — the design doc must say
  this in as many words, next to the preserve-reinstall flow.
- **Caveat stated, not hidden:** the secrets live in the CP DB because they must be
  viewable, so encryption protects the disk against theft — not against someone holding
  admin credentials on the LAN. Passwords are never logged and are masked in audit
  detail (D-37). This does not protect a running, unlocked box, and it does not bring
  TLS back.

**Design-gate deliverable:** `ENCRYPTED-USER-DATA.md` — the migration
plan, the installer UX (Hardware password prompt and copy), the recovery story under
encryption (including the lost-hardware-password floor: reinstall with data loss), and
what the installer changes.

### Tests

- QEMU: installer provisions an encrypted user-data volume; first boot unseals via TPM;
  `@etc`/`@var`, Postgres, Valkey come up; the appliance is whole.
- Update flow on an encrypted box: the unattended reboot must not hang on a passphrase —
  TPM sealing is what makes M7 P5's self-rebooting update compatible; assert it.
- Preserve-reinstall with the reset marker under encryption; and when the TPM has been
  replaced or erased, the installer **prompts for the Hardware password** before
touching the preserved volume, and refuses a wrong one.
- TPM-absent install fails with a legible preflight (option (a), decided).
- Console: no root autologin; **boot-target selection needs no password** while
  editing/console does; the tty root password is the stored one, not empty.
- Hardware password: retrievable by an admin from Settings → Advanced, with the info
  explainer; masked in logs and audit.

### Gate

Design doc approved, then `make test` + QEMU green.
**BM-3 (yours):** install on bare metal, then the stolen-box check — pull the disk /
boot without the TPM and confirm nothing readable comes up, and confirm the hardware
password recovers the box.

---

## Bookkeeping (rides the phases, not its own phase)

- `DESIGN.md` updates as each phase lands: §8 (signing as built, GitHub channel, sharded
  layout, size gate, `RELEASE.md`), §9 (encryption, console lockdown, GH-compromise
  posture, hardware secrets), §3.2 (Release channel control, release notes), §4/§5/§6
  (LUKS, slot version records), and the §13 decision-log rows superseding **D2**
  (channel) and recording the P5 supersessions (Retry/Cancel on the failed-update
  dialog; write phase at 100 % + reboot grace).
- `BACKLOG.md`: the `[M8]` items leave as they land; the doc-map rot (DESIGN/AGENTS
  still claiming to hold the consolidated backlog; `(§11 …)` pointers) is fixed in the
  same pass.
- `scripts/push-image-to-website.sh` is **removed** (D23); the old channel remains
  reachable only via `--url`.

---

## Decisions

Settled before drafting and in review — recorded so they do not get re-opened
mid-build:

| | Decision |
|---|---|
| **D1** | M8 targets an **external beta install base**; therefore data-at-rest security is in scope: encrypted user-data, TPM keys, GRUB password, console lockdown (P7). |
| **D2** | N-1 rollback via **GRUB boot screen and/or CLI**; the rollback UI is deferred. Per-slot version tagging and a tested rollback case are prerequisites (P6). |
| **D3** | Registry reachability preflight hardening stays in M8 (P3). |
| **D4** | Ownership-report-as-build-artifact is backlogged (`[BL]`), not M8. |
| **D5** | The updater **scans GitHub releases** via the API and picks by channel: stable = newest non-prerelease, beta = newest including prereleases (P3). |
| **D6** | Channels are **stable** and **beta**. The **installer asks**, defaulting to stable (brand-new hardware may need beta); `--channel=(beta|stable)` covers unattended installs; the choice persists as a **CP DB setting** an admin can flip to beta (P5). Tags are `v$VERSION`; release name `$VERSION`. |
| **D7** | Tags are `v$VERSION`; release name `$VERSION`; channel labels flip as releases mature. |
| **D8** | All hardcoded `dev-server` references are removed from the appliance and installer; it remains reachable via `--url` only (private RFC1918 mirror). |
| **D9** | The download path is exercised end-to-end only after the repo flips **public**; no token or credential ever ships in the image. |
| **D10** | `--dev` disables signature verification and GitHub semantics, is CLI-only, and composes with `--url`/`--dir` (source × trust are orthogonal). Production is **fail-closed**. |
| **D11** | A dedicated CI + build + signing box holds a **single long-lived** signing key; the signing step is modular for a future hardware key (D15). |
| **D12** | Only `manifest.json` is signed; SHA-256 in the verified manifest is the end-to-end integrity chain. |
| **D13** | The public key is **baked into the SquashFS** (and installer) — that is the GH-compromise defense, since a GH attacker cannot forge the signature. lemonseed.ai publishes the key out-of-band for humans, never fetched at runtime. Rotation = new key embedded in an image signed by the old key; designed, not exercised. |
| **D14** | Releases are triggered by **manual `workflow_dispatch` on a git tag** (`v$VERSION`); CI checks out the tag, never HEAD. Nothing builds on every check-in. |
| **D15** | Self-hosted runner on the build/sign box; the signing interface is modular so a hardware key (e.g. YubiKey tap-to-sign) can replace the file key later. |
| **D16** | The USB installer ships as a release asset, with a **build-time 2 GiB size gate**; the newest **10** iterations are retained; its hash/signature is published on the release page and lemonseed.ai, with documented manual verification (`sha256sum -c` + offline signature check). |
| **D17** | GitHub artifact attestations: skipped, backlogged; the appliance never checks them. |
| **D18** | Ex-M7 backlog retags: LiteLLM → M9; group-attribute editor + content-filter consumer → M9; other group attributes → M10; re-auth on group change, user profiles, guard-surface expansion, OWUI-panel read-only → BL (the group-namespace-sync residual merges into the OWUI-panel item); A/B-rollback-across-the-auth-boundary → M8 (P6). |
| **D19** | Milestone title: **Beta readiness: signed GitHub releases, release channels, and data-at-rest security.** |
| **D20** | Release notes surface: the pending update gains a **"See release notes"** link opening a sanitized read-only box; notes come from the GitHub release body fetched in the same check (P5). |
| **D21** | Rollback ships **without** schema-boundary safety checks (deliberate; the CLI guard is backlogged, and no equivalent is possible from GRUB). Rollback is QEMU-tested, and the **rollback drill is part of every release's BM checklist** (P4/P6). |
| **D22** | **`RELEASE.md`** — developer docs for the release, signing, channel-flip, verification, and rollback process — is an M8 deliverable; key custody details stay private. |
| **D23** | The old flat-directory channel is gone: dev iterations use `--url`/`--dir`. **`make dev-httpd-start` / `-stop`** (renamed from the proposed `serve-out` on review) serves `./out` over HTTP for local testing — via Docker (busybox `httpd` (`busybox:latest`), static files only) bound to the primary interface's own address, high port 8083, `0.0.0.0` only as a warned fallback. |
| **D24** | P7 key storage must never require a KVM. **Decided: (a)** — require a TPM 2.0 and fail the install with a legible preflight when absent or the seal cannot be established. (b) web-based boot passphrase workflow and (c) hardware-attribute-derived key (SHA-256 over eth0 MAC + DMI serial + boot-device serial) are **backlogged as *Alternatives to TPM*** until needed. |
| **D25** | GRUB password protects **editing/console only**; boot-target selection stays unrestricted, so rollback works from the boot menu without a KVM. Random **install-time** GRUB/tty passwords live in the CP DB, viewable in Settings → **Advanced** as "Hardware password" behind an info explainer; may double as the LUKS recovery passphrase. |
| **D26** | GitHub rate limits (cited: GitHub Docs, *Rate limits for the REST API*, read 2026-10-19): unauthenticated REST requests are **60/hour per originating IP**, not per repository — clients never burn a repo-owner quota, and asset downloads ride the CDN without touching the API. Weekly + fuzzed checks stand; no polling. |
| **D27** | **Hardware password lifecycle:** generated at install with the existing **N-words-with-dashes** scheme; presented during the new-install LUKS-format step with copy explaining the TPM replacement/erasure case; stored in the CP DB and viewable in Settings → Advanced; the installer **prompts for it** on preserve-reinstall when the TPM cannot decrypt; **no rotation** in this milestone; and if the TPM seal is gone and the password is lost, the only recovery is **USB reinstall with data loss**. |
| **D28** | **Release-channel decisions (M8 P3):** the channel is *only* the GitHub prerelease flag — the signed manifest describes the payload, never the audience, so a beta → stable flip is a metadata edit and the re-sign/rebuild machinery originally proposed was dropped as too heavyweight for the narrow concern (residual risk accepted: repo write access can re-badge, but it could publish anything anyway). Switching channels **never downgrades**; the version comparison is the only update decision. The setting lives in the CP settings table (`cp_meta`) behind a minimal `get_setting`/`set_setting` wrapper, materialized to `/var/lib/lemonseed/channel` for the DB-free root jobs; the installer seeds the file (preserve) or the ESP command line (fresh), and the CP adopts the seed. Stable with only prereleases published is *up to date*, not an error. |
| **D29** | **Signing cutover (M8, post-P4 review):** the signature scheme is **ECDSA NIST P-256, SHA-256 over the exact manifest bytes, DER-encoded** — recorded as `detached-ecdsa-p256` and enforced by the verifier; the `detached-ed25519` generation was fully superseded in one cut (no install base, single trust-set generation — set `version` 2, key file `lemonseed-release.pub` with its public fingerprint in the JSON). Production signing is **hardware-backed via `SIGN_MODE=pkcs11`**: the key never exists as a file, and every signature requires the token's own authorization (PIN and/or physical presence — not bypassable). Where the signing environment is a separate host from the build machine, **`SIGN_MODE=offline`** finalizes the signing block without a signature; the detached signature is produced out of band and the completed set must pass `make release-verify` before publishing. `SIGN_MODE=file` with `keys/test` (via `make keys`) keeps milestone iteration independent of the production key; the verifier notes randomized ECDSA (no deterministic-nonce assumption, no `s` normalization). Key custody and the signing environment remain private; only the scheme and the generic openssl primitives are public. |
| **D30** | **Rollback floor (M8 P6, review):** the 30-day horizon became **`MIN_ROLLBACK_VERSION`** declared by the *release itself* (`rollback.min_rollback_version` in the signed manifest — written by `release_payload.py pack` and `package.sh`; a one-way-door release pins it via `MIN_ROLLBACK_VERSION=<v>` through `release.sh`). Default at pack time: the release's date minus the horizon; legacy manifests fall back to the same computed floor at rollback time. The refusal is hard; `--force` is the CLI override. This also amends **D2**: the safety checks (registry-gated eligibility) and the **web rollback UI** shipped in P6 rather than being deferred — the schema-boundary guard alone stays backlogged (D21). |

## Open questions

1. ~~Repo coordinates~~ — **answered:** `https://github.com/mattcorddry/lemonseed`; may
   move to a new owner later, so the base URL stays one constant per file.
2. ~~Key escrow~~ — **answered privately:** the signing key is escrowed; where and
   how is deliberately not documented in the public repo — `SIGNING.md` records only
   that escrow exists.
3. ~~Part size~~ — **answered:** 1 GiB parts; the final part is a remainder.
4. ~~Channel flip UX~~ — **answered:** an audit log entry only; no banner for beta.
5. ~~Installer channel prompt~~ — **answered:** the installer asks, defaulting to
   stable (D6).
6. ~~P7 key storage~~ — **answered:** option (a), TPM required with a legible
   install-time preflight; (b) and (c) are backlogged as *Alternatives to TPM* (D24).
7. ~~LUKS recovery without the hardware password~~ — **answered:** USB reinstall with
   data loss; the design doc must say so in as many words (D27).

No plan-time questions remain open. The two design gates (`SIGNING.md`,
`ENCRYPTED-USER-DATA.md`) still produce documents for approval before their phases
build.
