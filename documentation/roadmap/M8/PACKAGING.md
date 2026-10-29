# M8 P2 — Packaging: sharded payload, installer size gate, `make release`

**Status: approved and shipped.** Implements `PLAN.md` §P2. P1's signing
root of trust is a prerequisite and is shipped; this phase defines the **release asset
format** the signature protects and the **assembly path** consumers run before trusting
anything.

---

## 1. Problem

GitHub caps each release asset under **2 GiB** and `root.squashfs` is ~3 GB, so the
payload must ship as ordered parts. Nothing asserts the installer ISO stays under the
cap. There is no `make release`; publishing is a hand-run SCP (`push-image-to-website.sh`,
removed this iteration — D23). The updater/installer can only consume flat artifact
directories.

## 2. Release asset format

A release is a **flat set of assets** with `manifest.json` outside the payload — it is
both the assembly plan and the root of trust (signed, P1):

| Asset | Content |
|---|---|
| `manifest.json` + `manifest.json.sig` | the plan + the signature over its exact bytes |
| `payload-000.part`, `payload-001.part`, … | fixed-size byte slices (1 GiB) of one canonical tar; the final part is a remainder |
| `lemonseed-installer.iso` | the USB installer, shipped as a **direct asset** recorded in the manifest like everything else (D16) |

### 2.1 Canonical tar

All appliance payload artifacts (`vmlinuz`, `root.squashfs`, `initrd.img` — the
manifest's `artifacts` keys, in that fixed order) go into one tar built with:

```
tar --format=pax --sort=name --mtime=@0 --owner=0 --group=0 --numeric-owner
```

plus a fixed member order (explicitly listed files, never a directory scan). Identical
inputs therefore produce a byte-identical tar. **Direct-asset shipping for small
artifacts was rejected**: one assembly path, always exercised, no special cases.

### 2.2 Manifest schema (additive)

```json
{
  "version": "20261020.01",
  "artifacts": { "vmlinuz": {…}, "root.squashfs": {…}, "initrd.img": {…} },
  "payload": {
    "tar":    { "length": 3223813120, "sha256": "…" },
    "chunks": [
      { "name": "payload-000.part", "length": 1073741824, "sha256": "…" },
      { "name": "payload-001.part", "length": 1073741824, "sha256": "…" },
      { "name": "payload-002.part", "length": 1074632960, "sha256": "…" }
    ]
  },
  "installer": { "name": "lemonseed-installer.iso", "length": …, "sha256": "…" },
  "signing": { … }
}
```

- `chunks` are listed **in order**; part names are conservative ASCII
  (`payload-000.part`) and **positional** — stable across releases so a future updater
  can keep parts whose SHA is unchanged and refetch only the rest. (GitHub renames
  assets containing special characters; these names never will.)
- `payload.tar` itself is **not an asset** and carries no `name` — it never appears on
  a release page and is never stored as a file (§4 streams it); its `length`/`sha256`
  are purely the reassembly check.
- The installer ISO is covered by the manifest signature like everything else (D12);
  no separate `.sig` for it. The installer image is **under the cap today (D16)** and
  the size gate (§4) now asserts it.
- `signing.public_key` names the key in the baked trust set; unchanged from P1.

### 2.3 No compression

The tar is **not gzipped**. `root.squashfs` is already `zstd`-compressed at the squashfs
level, and the payload's dominant bytes are exactly that file — a second compression
layer has nothing left to squeeze and would cost assemble-time CPU for no size win.
(Also noted in DESIGN §8 when this lands.)

## 3. `make release` (local)

One target, the single place the asset set is defined:

```
make release                      # VERSION override supported, like build-production
```

Pipeline (`scripts/release.sh`):

1. `SIGN=1 make build-production` (release builds are signed; fail if no ceremony key).
2. `make build-installer`.
3. **Pack** (`scripts/release_payload.py pack`): canonical tar → cut into 1 GiB parts →
   **size gate** → write the `payload`/`installer` blocks into the manifest.
4. **Sign** (`scripts/sign.sh`) — the signature covers the final manifest, so packing
   strictly precedes signing. `SIGN_MODE` picks the backend: `file` (local test keys,
   self-verified against the signing key's own public half), `pkcs11` (production,
   self-verified against the baked trust set), or `offline` (finalizes the signing
   block, writes no signature — the detached signature arrives out of band and the
   completed set must pass `make release-verify`).
5. **Stage**: the complete, upload-ready asset set lands in `out/release-<version>/`.
   Publishing is P4's job (`push-image-to-website.sh` is removed — D23).

### Size gate (D16, build-time)

The pack step fails the build when any would-be release asset exceeds **2 GiB**:
every chunk (1 GiB by construction, asserted anyway) and the installer ISO. It also
asserts the chunk count is ≤ **1000** (GitHub's per-release asset limit) and names are
ASCII. A synthetic oversize artifact must fail with the offending name (test §6).

### `make release` and signing

`make release` **always produces a signed-or-signable set**: `file` mode signs with
the local test keys and self-verifies against them (self-consistent for local
iteration, but appliances refuse it — the baked trust set carries only the production
key); `pkcs11` signs with the production key and self-verifies against the baked trust
set; `offline` stages the finalized, unsigned set for an external signing environment
and completes only via `make release-verify`. And because `make release` stages but
does not upload, the P4 publisher must **re-verify the signature before and after
upload** (re-download and run the appliance's own verifier) — a release is not done
until the publisher's check passes (PLAN §P4 self-verify).

## 4. Consumer changes: verify → assemble → verify → apply

`acquire_artifacts` (both the updater and the installer's vendored copy) gains a
payload stage that runs whenever the manifest carries a `payload` block, replacing the
per-artifact downloads:

```
verify manifest signature            (P1, already wired)
→ free-space preflight (§4.1)
→ for each chunk, in order: download/copy + stream-verify sha256, checkpoint after
  each verified part (GitHub documents no Range support for release assets)
→ pass 1: stream-concatenate the verified parts, hashing the assembled tar —
  verify its length + sha256
→ pass 2: stream the same bytes again, extracting members to staging
  (members validated: regular files only, exact expected names, no traversal)
→ re-verify every extracted file against the manifest's artifacts sha256
→ hand the files dict to the existing write/apply path (unchanged)
```

Failures are legible and fail-closed: corrupt part, short part, chunk-count mismatch,
tar length/hash mismatch, unexpected tar member, extracted-file hash mismatch — each
names the asset. A manifest without a `payload` block (flat sets: `--dir` raw dirs, the
current `dev-server` channel) keeps the existing path unchanged, so the channel
keeps working until P3 moves it.

### 4.1 Staging: 1× disk, `/var`-backed, cleaned up

The assembled **tarball is never written to disk** — the parts are streamed twice
(once to hash, once to extract), so peak usage is **1× the extracted payload** and
nothing else. Staging lives under **`/var`** (the SSD-backed user-data volume —
`/var/lib/lemonseed/download/payload/`, alongside the part cache), never tmpfs: a 3 GB
staging area in RAM would be absurd on an appliance.

- **Free-space preflight before starting:** the manifest states everything needed —
  required bytes = Σ `artifacts[*].length` (the extracted payload) plus a fixed margin;
  refuse with the numbers in the message when `statvfs` free space on the staging
  filesystem is short. This runs before any part is downloaded or assembled.
- **ENOSPC is handled, not fatal-by-crash:** a write failure mid-extraction is caught,
  the staging area is cleaned up, and the error names the file and the shortfall.
- **Cleanup:** the staging directory is removed on success *and* on failure. The
  verified **parts** are kept under the download dir until a successful apply (that is
  the resume cache), then removed with the staging area.

Note the enforced order subsumes P1's: signature → manifest hashes → per-part
verification → assembly → re-verification → *then* the A/B slot write. Nothing is
trusted before its hash checks pass, and nothing touches a partition before the whole
payload has verified.

Both sources work identically: `--dir` reads parts from a local directory,
`--url` fetches them over HTTP — and since the fetch machinery is `urllib`-based,
`--url file:///…` works natively too, making a local directory reachable through the
same code path as a release page. Only GitHub *discovery* (which release is latest,
per channel) remains for P3.

## 5. Determinism

Chunks are pure byte slices of a canonical tar, so determinism follows from tar
determinism: identical inputs → byte-identical parts. Pinned flags plus explicit member
order; no timestamps, owners, or directory iteration order.

## 6. Tests

1. **Byte-determinism:** pack the same payload dir twice → `cmp` every part.
2. **Round-trip:** assemble → verify tar → untar → compare extracted bytes against the
   originals, including a **remainder final part** (force one in tests).
3. **Refusals:** corrupt part byte; truncated part; chunk-count mismatch; tar length or
   hash mismatch; unexpected tar member; extracted-file hash mismatch. Each is a
   legible failure naming the asset.
4. **Size gate:** synthetic oversize artifact fails the pack with the offending name.
5. **Space:** free-space preflight refuses (with the required/free numbers in the
   message) when the staging filesystem reports less than Σ artifacts + margin; an
   `ENOSPC` raised mid-extraction is caught, cleans the staging area, and names the
   file — never a raw traceback into the updater's job state.
6. **Updater `--dir` end-to-end:** `acquire_artifacts` consumes a `make release`-shaped
   set (signed sharded payload) and the apply path proceeds — unit-covered, and in QEMU
   by extending the signing smoke's `good` set to the sharded layout (PASS marker) and
   adding a flipped-**part**-byte refusal scenario alongside the existing
   flipped-payload one.

### Gate

`make test` + `QEMU_API_TIMEOUT=… make test-qemu` green, including the sharded
`--dir` round-trip in QEMU. No bare metal (per the plan).

## 7. File inventory

| File | Change |
|---|---|
| `scripts/release_payload.py` | **new** — `pack` (tar/split/manifest/size gate) + `assemble` (verify/concat/untar/re-verify), one implementation |
| `scripts/lemonseed_artifacts.py` | payload stage in `acquire_artifacts` (§4) |
| `installer/install.py` | same stage, vendored copy |
| `scripts/release.sh` | **new** — the `make release` pipeline (§3) |
| `Makefile` | `release` target |
| `scripts/qemu-api-smoke-test.sh` | signing-smoke sets move to the sharded layout |
| `scripts/test_release_payload.py` | **new** — unit tests (§6.1–6.4) |
| `scripts/run-unit-tests.sh` | register the new test file |
| `scripts/push-image-to-website.sh` | **removed** (D23) |
| `DESIGN.md` §8 | release asset set as built |
| `BACKLOG.md` / `PLAN.md` | GitHub-releases entry status; P2 status |

## 8. Decisions (review feedback, resolved)

1. **Part size 1 GiB with positional names** — confirmed.
2. **Schema:** `payload.tar.name` dropped (the tar is never a stored file, §4); the
   rest of the schema stands.
3. **Consumers:** both `--dir` and `--url` are wired now; `--url` also accepts
   `file:///…` natively (urllib), so a local directory is reachable through the same
   code path as a release page.
4. **`make release` requires the ceremony key** — a release set is always signed; the
   P4 publisher additionally re-verifies the signature when uploading to GitHub
   releases (§3).
5. **No gzip** on the payload tar — squashfs is already compressed (§2.3).
6. **Staging is 1× disk, on `/var`, cleaned up;** free-space preflight from the
   manifest + `ENOSPC` handling and tests (§4.1, §6.5). No test exercises 1000 parts —
   the pack asserts the GitHub cap but the test suite does not build a terrabyte.
