# RELEASE.md

This document provides the runbook to build, sign, and publish new releases for
Project Lemonseed. It is structured in five parts: an overview of the release
rules; preparing a new build machine; building and testing a release candidate;
signing and publishing a release; and the release lifecycle after publication.
The release signing steps can only be performed by users with access to the
Lemonseed signing keys, which are stored securely.

# Part 1 — Overview

Project Lemonseed establishes the following rules to govern the process for
every Lemonseed release.

- **One signing VM, one purpose.** Releases are built only on the dedicated
  build/sign VM, only from a reviewed `v<version>` tag, and only by an
  operator. No everyday development happens on this machine.
- **The tag is the release.** Builds happen from a clean checkout of the tag —
  never from an untagged or dirty tree (`package.sh` builds with `--impure`, so
  a dirty tree pollutes the build inputs).
- **A version's bytes are immutable.** Once published, never re-publish
  different bytes under the same tag. A bad release is fixed by cutting a new
  version; the old one is deleted or superseded.
- **Verify before you publish, and verify what you published.** The build
  self-verifies with the appliance's own verifier; the published copy is
  re-verified in full from the CDN after upload (Part 4, step 3).
- **The channel is the prerelease flag.** Publish as a prerelease for beta,
  clear the flag to promote to stable. The payload never changes on a flip.
- **Switching channels never downgrades.** The channel only filters candidates;
  the version comparison decides. A stable box with only prereleases newer than
  its running version is *up to date*.

# Part 2 — Preparing a new build machine

Project Lemonseed is built on NixOS. This requires the build machine to have a
full Nix installation. The following process describes the installation and
configuration of Nix on a Debian Linux machine. Builds on other Linux
distributions should be possible but are untested; builds on Windows or macOS
are not supported or recommended.

1. **Install dependencies using the native OS package manager.**

   ```bash
   sudo apt update && sudo apt upgrade
   sudo apt install build-essential git gh curl sudo python3 jq
   ```

   `python3` is what `make test` runs under (the control-plane tests are
   stdlib-only); `gh` is needed later for publishing (signing VM only).

2. **Install the Nix framework.** The
   [Nix manual](https://nixos.org/manual/nix/stable/installation) describes the
   installation modes; multi-user (daemon) is the recommended mode on a shared
   build box. On Debian/Ubuntu:

   ```bash
   sudo apt install nix-setup-systemd   # creates the nix-daemon service and nixbld users
   systemctl status nix-daemon          # expect active
   ```

   **`/nix` space:** the appliance closure includes the CUDA toolchain compiled
   from source, so 20 GB is quickly outgrown once `out/` accumulates release
   sets — size for **≥50 GB**. If `/` is small, mount or bind-mount a larger
   filesystem on `/nix` **before starting the daemon**; check with `df -h /nix`.

3. **Configure Nix, and test.**

   ```bash
   # Flakes are still an experimental feature; the build needs them:
   echo "experimental-features = nix-command flakes" | sudo tee -a /etc/nix/nix.conf

   # The build itself is a flake with a pinned flake.lock, so nixpkgs comes from
   # the lock. But `make lint` resolves nixpkgs#ruff through the user registry,
   # which a fresh multi-user VM leaves empty:
   nix registry add nixpkgs github:NixOS/nixpkgs/nixos-26.05
   nix doctor      # expect no fatal findings
   ```

   **Pin freshness:** before each release cycle, confirm the pinned upstream
   (`nixos-26.05`, matching the repo's `flake.nix` and the registry entry
   above) is still the latest stable via
   [status.nixos.org](https://status.nixos.org/), and bump the pin when a new
   stable lands. **Version caveat:** Debian's packaged Nix can lag upstream; if
   `nix flake` commands fail to parse the repo's `flake.lock`, fall back to the
   official multi-user installer from [nixos.org](https://nixos.org/download/).

   Build as your regular user — the daemon's `nixbld` users perform the actual
   builds; nothing here needs a root shell except package setup.

4. **Clone the repository and run the gates.**

   ```bash
   git clone git@github.com:mattcorddry/lemonseed.git && cd lemonseed
   make test    # Tier A: update, repart, control-plane — no root, no GPU
   make lint    # exercises the nixpkgs registry configured in step 3
   ```

## Setting up the signing environment (build/sign VM only)

Most developers will not have the signing keys — nor should they. The steps in
this subsection are performed once, on the dedicated build/sign VM only;
everyone else proceeds directly to Part 3 for local testing of their work.

1. **Make the release key available to the signing environment.** The private
   half of the production key never exists as a file on the machine; it lives
   in signing hardware and is used through the `pkcs11` signing backend (see
   [proposals/M8/SIGNING.md](./proposals/M8/SIGNING.md)). Setup means: the
   hardware is present and authorized, and the openssl pkcs11 provider and its
   PKCS#11 module are installed (`sign.sh` discovers both by globbing the
   usual library directories and fails legibly when they are missing).
2. **Confirm the key matches the public key the images bake.** The tracked
   trust set must carry exactly the public half of the signing key:

   ```bash
   openssl pkey -pubin -in keys/lemonseed-release.pub -text -noout | head -1
   ```

   and `keys/release-keys.json` must list `lemonseed-release.pub` under
   algorithm `ecdsa-p256`. A mismatch means you would sign releases no
   appliance can verify.
3. **Authenticate the GitHub CLI** (`gh auth login`) — asset upload and the
   channel flip need it.
4. **Note the version convention: always pass `VERSION=<version>`** to
   `make release`. The default (date + local `.build-counter`) starts at `.01`
   on any new VM, so the explicit override is what keeps versions unique and
   meaningful.

# Part 3 — Build and test

The goal of this part is to produce a complete, verified release candidate
from a reviewed tag. Everything here runs without the ceremony keys: without
them, `make keys` generates local test keys so the signing step still executes
and the build is exercised end to end. A release signed with test keys
verifies locally but **an appliance will refuse it** — the baked trust set
only carries the production key — so test-key builds are for build validation
(including the QEMU smoke below), not for bare-metal updates.

1. **Cut the version and run the gates.** Pick `<version>` (`YYYYMMDD.NN`),
   confirm `main` is reviewed, and cut the tag on the commit you intend to
   release:

   ```bash
   git tag "v<version>" <commit>
   git checkout "v<version>"
   make test      # Tier A: update, repart, control-plane — no root, no GPU
   make lint      # optional but expected for a release
   ```

2. **Build, pack, sign, self-verify.** On a machine without the ceremony key,
   generate local test keys first (`make keys`, once):

   ```bash
   VERSION=<version> make release
   ```

   This runs `SIGN=1 build-production` + `build-installer`, packs the sharded
   payload (fixed 1 GiB `payload-NNN.part`, installer ISO as a direct asset),
   signs `manifest.json` and re-verifies the whole set with the appliance's own
   verifier. Output: `out/release-<version>/`. The first build on a new VM is
   slow (the GPU stack is compiled from source); later builds are incremental.

   Check the output: the self-verify must print `OK: manifest signature
   verifies`, `OK: N parts verified`, and `OK: release set verifies`.

3. **Run the QEMU smoke.** The appliance boots in QEMU and the HTTP API smoke
   probes `/healthz`, `/api/status`, `/api/boot`, `/api/data`, `/api/modules`:

   ```bash
   make build-qemu          # if artifacts are missing or Nix inputs changed
   QEMU_API_TIMEOUT=1200 make test-qemu
   ```

   Without `/dev/kvm` the guest runs under TCG and is slow — raise the timeout
   rather than declaring failure early. Iterate (fix → rebuild → re-test)
   until green or escalate with the log (`out/qemu-api-smoke.log`).

# Part 4 — Sign and release

> **This part requires the release keys and can only be performed in the
> secure Lemonseed production build environment** — the build/sign VM prepared
> in Part 2 with the signing setup. Every step here consumes or produces the
> production-signed release set; none of it can be run on an ordinary
> development machine.

The goal of this part is to take the release candidate from Part 3 through
bare-metal validation, publish it to GitHub as a beta, promote it to stable,
and confirm both channels on real hardware. The bare-metal lifecycle threads
through the whole part: a box on the last stable tests the local build, rolls
back; the same box on the beta channel tests the published beta, rolls back;
and a box on the stable channel tests the promoted release and keeps it.

1. **Sign with the production key, then test on bare metal from the dev
   server.** First produce the production-signed release set — the Part 3
   candidate was signed with local test keys, which appliances refuse.

   If the signing hardware is attached to the build machine, one command does
   it — the token's own authorization (typically a PIN prompt plus physical
   presence) is required for the signature and cannot be bypassed:

   ```bash
   SIGN_MODE=pkcs11 VERSION=<version> make release
   ```

   If the signing environment is a separate machine, split the step:

   ```bash
   SIGN_MODE=offline VERSION=<version> make release
   ```

   This stages `out/release-<version>/` with the manifest's signing block
   finalized but **no signature**. Hand the staged `manifest.json` to the
   signing environment by whatever transfer it accepts, have it signed there,
   and return the detached signature as
   `out/release-<version>/manifest.json.sig`. Then prove the completed set
   before publishing:

   ```bash
   VERSION=<version> make release-verify
   ```

   This runs the appliance's own verifier — signature first, then every part
   and asset by SHA-256 — so the transferred bytes are proven identical to
   what you publish.

   Then serve the release locally and update a bare-metal box **that is
   running the current stable release from GitHub**:

   ```bash
   make dev-httpd-start    # serves ./out; prints the URL
   ```

   On the appliance:

   ```bash
   lemonseed-update status                                            # which slot am I running?
   lemonseed-update check --url http://<vm-ip>:8083/release-<version>/
   lemonseed-update apply --url http://<vm-ip>:8083/release-<version>/ --reboot
   ```

   Exercise the release — chat, Open WebUI, the portal — before trusting it.
   Then roll **back to the last stable** via the GRUB boot menu: the
   pre-release sits in the inactive slot, so booting the previous slot restores
   it. If the pre-release booted and locked in, the panel may show the update
   as failed after a manual rollback — that is the failed-update detector doing
   its job; `lemonseed-update cancel` clears it.

   (The USB installer ISO does not carry the appliance image — it downloads
   one from the resolved release channel. To install an unpublished set, boot
   the ISO and point it at the staged release: at the GRUB menu press `e`,
   append `lemonseed.image=http://<dev-ip>:8083/release-<version>/` to the
   `linux` line, and press F10 to boot. A locally mounted release directory
   works too, via `lemonseed.artifacts=<dir>`. Production releases need no
   override — the channel resolves them.)

2. **Publish as a beta (prerelease).**

   ```bash
   gh release create "v<version>" out/release-<version>/* \
     --title "<version>" --notes "<release notes>" --prerelease
   ```

   - The **notes are the changelog** — the control plane surfaces them
     verbatim as "release notes" in the update flow, so write them for the
     household, not for developers.
   - Asset uploads are resumable-by-retry; a mid-upload `502` leaves an empty
     starter asset that is safe to delete and re-upload. Re-uploading an
     existing filename is a `422` — delete the old asset first.

3. **Verify the whole published release.** Download everything published —
   the manifest, its signature, every payload part, and the installer ISO —
   into a clean directory. `gh release download` fetches each asset from the
   same CDN URL an updater would hit, so this proves what is actually served,
   not what you staged locally:

   ```bash
   rm -rf /tmp/verify && mkdir -p /tmp/verify
   gh release download "v<version>" --repo mattcorddry/lemonseed \
     --dir /tmp/verify \
     --pattern "manifest.json" --pattern "manifest.json.sig" \
     --pattern "payload-*.part" --pattern "*.iso"
   ```

   Then run the appliance's own verifier against that directory — from any
   checkout of the released source (the tag itself, or `main` if unchanged
   since):

   ```bash
   git -C <repo> checkout "v<version>"   # or any checkout of the released source
   python3 <repo>/scripts/release_payload.py verify \
     --release-dir /tmp/verify --trust-dir <repo>/keys
   ```

   The verifier checks the signature first, then every part and asset by
   SHA-256 against the manifest — the same fail-closed path the appliance
   runs. A missing asset or a byte mismatch is a hard failure: fix the release
   before promoting it.

4. **Bare-metal test against the GitHub beta.** On a box **on the beta
   channel** running the last stable:

   ```bash
   lemonseed-update check          # no flags — the channel resolves the prerelease
   lemonseed-update apply --reboot
   ```

   Exercise the release. A box **on the stable channel** sees nothing — that
   is the channel filter doing its job. When the test is done, roll the box
   back to the last stable via the GRUB menu, as in step 1.

5. **Promote to stable.** One metadata edit — the payload never changes, so
   nothing is rebuilt or re-signed:

   ```bash
   gh release edit "v<version>" --prerelease=false
   ```

   Then re-verify the published manifest signature (step 3) — the bytes are
   unchanged, so this is a sanity check that the flip touched only metadata.
   Stable boxes see the release on their next check; a beta box already
   running it is never downgraded.

6. **Bare-metal test against the stable release.** Repeat step 4 on a box
   **on the stable channel**: check, apply, exercise. This box then **stays**
   on the promoted stable release — no rollback this time; the stable channel
   has caught up.

# Part 5 — Release lifecycle

Once a release is published, it is governed by three goals: keep the **newest
10** releases on GitHub and delete older ones at leisure (deleting a release
never affects boxes already updated); keep every published version's bytes
**immutable** (never re-publish different bytes under an existing tag); and
treat **rollback as physical** — a box recovers via its GRUB boot menu, never
by re-publishing over a broken release.

**Removing a release from production** (a beta that never earned promotion, or
an old release past retention):

1. Confirm no box depends on it being *newer* than what it runs — removing a
   release only moves boxes back to the next-older candidate, never removes
   what they already installed.
2. Delete it: `gh release delete "v<version>" --yes` (add
   `--repo mattcorddry/lemonseed` when not run from a clone). The tag
   reference remains in git — that is fine; the next release must use a
   *new* version regardless, because a published version's bytes are
   immutable.
3. A bad beta can simply be deleted; boxes that installed it keep running it
   until the next release.

**Recovering from a bad release** (a release that should not have shipped, or
one that boxes already consumed):

1. On any affected box, roll back to the previous slot from the **GRUB boot
   menu**, then `lemonseed-update cancel` to clear the failed-update marker.
   Rollback is physical, not remote — this is the point.
2. Revert the git tree to the last known good: `git revert` the offending
   commits on `main` (or reset to the prior `v<version>` tag and re-apply good
   work), so the fix is a *forward* change in history, not a rewritten tag.
3. Cut a **new version numbered higher than the bad release** (`YYYYMMDD.NN`
   with a fresh `.NN`), and run Parts 3–4 again from the reverted tree. Boxes
   that rolled back then take the corrective release like any update; boxes
   still on the bad release receive it on their next check.
