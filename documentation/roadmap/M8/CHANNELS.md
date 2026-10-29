# M8 P3 — Update & install channels: GitHub releases

**Status: approved and shipped.** Implements `PLAN.md` §P3 with the four
review decisions below. P1 (signed manifest, fail-closed verifier) and P2 (sharded
payload, `make release`) are prerequisites and are shipped; this phase moved the
channel from a flat HTTP directory on `dev-server` to **GitHub releases**,
added the **stable/beta channel concept**, and de-hardcoded the old origin (D8, D23).

**Decisions taken on review (2026-10-20):**

- **D-C1: the GitHub prerelease flag is the only channel marker.** `manifest.json`
  describes the *payload only* — it never names a channel, and a beta→stable flip is
  a GitHub metadata edit, never a rebuild or re-sign. The residual risk (an operator
  with repo write access re-badging a beta as stable by clearing the flag) is
  explicitly accepted: that actor can already publish anything.
- **D-C2: switching channels never downgrades.** A beta box moved to stable that
  already runs a newer version simply stays where it is until stable catches up. The
  channel only ever *filters candidates*; the version comparison (which already
  refuses equal/older offers) is the only update decision.
- **D-C3: `make dev-httpd`, Docker-based** — a small busybox `httpd` container
  (`busybox:latest`, per review; static files only, no CGI), bound to a high port
  on the primary interface's own address; no dependence on distro httpd paths.
- **D-C4: the channel setting goes through the CP settings table now**, via the
  existing `cp_meta` KV store — a thin `get_setting`/`set_setting` wrapper, no new
  storage, no web UI in this phase.

---

## 1. Problem

`IMAGE_BASE_DEFAULT = "http://dev-server/releases/stable/"` is hardcoded in
`scripts/lemonseed_artifacts.py` and `installer/install.py` (the two must agree). It is
an RFC1918 origin over plain HTTP: useless off that network, and one DNS-poisoning
sentence away from a malicious image for anyone on it. The updater
(`status|apply|cancel|check`) has no GitHub semantics and no channel concept; the
installer asks nothing about channels. Publishing a beta means "whatever is in the
directory", which cannot be a different artifact set per audience.

## 2. Discovery (D5)

- **One REST call.** `GET <RELEASES_API_DEFAULT>` where the constant is
  `https://api.github.com/repos/mattcorddry/lemonseed/releases?per_page=10`
  (`per_page=10` matches the retention decision — we keep the newest 10 releases). A
  distinctive User-Agent (`lemonseed-updater/<version>`); no token anywhere in the
  image (D26: the budget is per-IP, not per-repo, and assets ride the CDN).
- **Selection.** Filter out `draft` releases, then:
  - **stable** = newest release with `prerelease: false`;
  - **beta** = newest release regardless of the prerelease flag.
  A release that is *only* an RC therefore exists for beta boxes and is invisible to
  stable boxes; "no release for me" is **up-to-date**, not an error (a stable box with
  only prereleases published reports `latest=<running>`).
- **The release → base mapping is the whole integration point.** Every asset of a
  release shares the prefix
  `https://github.com/mattcorddry/lemonseed/releases/download/<tag>/`, so discovery
  returns exactly what `fetch_manifest`/`acquire_artifacts` already consume: a **base
  URL**. The sharded-payload machinery from P2 works against GitHub unmodified —
  parts, `manifest.json` and `manifest.json.sig` are plain assets off that prefix.
  `browser_download_url` is a plain GET (urllib follows the 302 to the CDN); no
  `Accept: application/octet-stream` dance needed outside the API listing itself.
- **Malformed answers are legible refusals**: non-200 (403 with the rate-limit shape →
  a message naming the 60/hour shared-IP quota and the `Retry-After`/`X-RateLimit`
  hint), non-list JSON, a release missing `tag_name` or the expected assets →
  `ArtifactError`, never a traceback (D3 preflight: the same "your WAN is down, the
  appliance is fine" tone as the registry preflight; the message now covers the GitHub
  API as well as the registry).

## 3. The channel marker is GitHub metadata only (D-C1)

`manifest.json` is untouched: it describes the payload (P2) and gains **no** channel
field. The channel is the release's **prerelease flag** and nothing else:

- publishing an RC as a GitHub *prerelease* keeps it out of stable;
- the flip to stable is `gh release edit <tag> --prerelease=false` — no rebuild, no
  re-sign, no asset churn (P4's `RELEASE.md` documents the one command; no script in
  this phase).

Accepted residual risk (D-C1): repo metadata is unsigned, so someone with repo write
access can re-badge a beta as stable by clearing the flag. The signature still pins
every byte of the payload, so the worst case is *which* of our signed releases a box
sees — never different bytes.

## 4. Channel transport on the box

Four sources, strict precedence (first wins):

1. `--channel=(stable|beta)` CLI flag (and the installer's form value, §6);
2. `LEMONSEED_CHANNEL` environment variable (tests, QEMU, one-off overrides);
3. the CP **settings table** (`cp_meta`, key `release_channel`) — the authority
   admins change from P5's UI onward, read by root jobs through a *materialized
   file* (below);
4. default **stable**.

Root jobs (the weekly/boot systemd checks) stay DB-free by design, so the setting is
**materialized** to `/var/lib/lemonseed/channel` — a one-line file the packaged
updater wrapper reads as its fallback:
`LEMONSEED_CHANNEL="''${LEMONSEED_CHANNEL:-$(cat /var/lib/lemonseed/channel 2>/dev/null)}"`.
Write paths, one direction each:

- the **installer seeds the file** at install time (the CP DB does not exist yet on a
  fresh box);
- the **CP adopts it once** at boot (`settings.get_setting('release_channel')` finds
  no row → import the file's value) and thereafter the **settings table is the
  authority**: `set_setting('release_channel', …)` rewrites the file in the same
  transaction path, so the timers never see a stale channel.

`lemonseed-update check` prints and records the channel with the finding
(`latest-update.json` gains `"channel"`), so the panel snapshot (P5) can render
"stable 20261020.05" honestly.

## 5. Source × trust flags (D8, D10)

| Invocation | Source | Signature |
|---|---|---|
| *(default)* | GitHub discovery, per channel | **verified** |
| `--url=BASE` | BASE (flat dir incl. `dev-server` mirrors and `file://`) | **verified** — production-strict, unchanged |
| `--dir=PATH` | local artifact set | verified; a bare unsigned dev set stays allowed |
| `--dev` | default source (discovery or `--url`/`--dir`) | **skipped** — no signature, no GitHub semantics enforced |

- `--dev` composes with both `--url` and `--dir` (the P1/P2 "unsigned dev set" path).
- `--dev` is **CLI-only**: the portal's install/update path is always
  production-strict, and `--url` keeps mirrors honest (a mirror serves signed bytes
  or it serves nothing).
- `--channel` + `--url` together is a **usage error** (same sentence style as
  "use either --url or --dir, not both"): channel only means something when discovery
  picks the source.

## 6. Installer onboarding (D6)

- The install page gains a **Release channel** radio — stable / beta, stable
  pre-selected — with one honest line under it ("beta gets new builds first, including
  hardware support for brand-new machines"). Same form POST as the preserve/reset
  options; `server.py` threads it into `run_install(..., channel=...)`.
- CLI: `--channel=(stable|beta)`, default stable, validated (anything else is a
  usage error).
- **Seeding:** during the post-write stage the installer writes the chosen channel to
  `/var/lib/lemonseed/channel` on the target's user-data — the same volume the reset
  marker already uses (the mechanism exists; a failed write fails the install for the
  same reason the marker's does). The CP imports it into `cp_meta` on first boot (§4)
  and owns it from then on.
- Git tags are `v$VERSION`; the release name is `$VERSION` — a pure convention, the
  only place it is enforced is the publishing step (§8).

## 7. De-hardcode (D8, D23)

- `IMAGE_BASE_DEFAULT` **leaves both files**. The new constant,
  `RELEASES_API_DEFAULT = "https://api.github.com/repos/mattcorddry/lemonseed/releases?per_page=10"`,
  appears in `scripts/lemonseed_artifacts.py` and the vendored `installer/install.py`
  under the existing "must agree" rule — and a unit test pins the agreement this time
  (the comment alone let it drift once already). The old `dev-server` origin
  survives only as a `--url` value in docs/proposals, never as a constant.
- `lemonseed.image=` on the kernel cmdline keeps working as a source override
  (rescues, kexec-style testing); it overrides discovery exactly like `--url`.

## 8. The beta → stable flip (D-C1)

One GitHub metadata edit — `gh release edit <tag> --prerelease=false` — is the whole
flip. No rebuild, no re-sign, no asset churn: the bytes the signature pins do not
depend on the channel. `RELEASE.md` (P4, D22) documents the command and the
verify-after-publish step; there is no `release-channel` script in this phase.

## 9. `make dev-httpd` (D-C3, was `serve-out`)

Serves `./out` over HTTP for local installer/updater testing of `--url` sets —
**via Docker**, so no distro httpd path is depended on:

- small image (`busybox:latest` — a couple of MB; deliberately not version-pinned,
  dev-tool-only, `DEV_HTTPD_IMAGE` overrides), `busybox httpd -f`: static files
  only, no CGI enabled;
- `--rm`, read-only bind of `./out`, high port **8083**;
- **binding:** busybox httpd has no subnet ACL, so the restriction comes from the
  Docker publish address: the *primary interface's* own IP (iproute2-detected, the
  interface the default route uses) — practical, and exactly the "local subnet" —
  rather than `0.0.0.0`. Detection failure falls back to `0.0.0.0` **with a printed
  warning**; `DEV_HTTPD_BIND` overrides both.
- two targets: **`dev-httpd-start`** (background container, named, idempotent —
  replaces a stale one and prints the URL) and **`dev-httpd-stop`** (removes it);
- `--url http://<primary-ip>:8083/release-<version>/` then exercises the strict
  signed path end-to-end without a mirror.

## 10. Rate limits (D26, cited)

Unauthenticated REST requests are **60/hour per originating IP** — clients never burn
a repo-owner quota, and adoption does not aggregate against us. A check costs one
`GET /releases`; asset downloads ride the CDN and do not touch the API. The fuzzed
weekly check plus manual checks sit orders of magnitude under the limit; the one real
scale risk (many appliances behind one shared egress IP) is already mitigated by the
fuzz + cadence, and the escape hatch (a tiny static channel file on lemonseed.ai's
CDN) is backlogged, not built. We never poll. Source: GitHub Docs, *Rate limits for
the REST API*, read 2026-10-19.

## 11. Tests

- **Discovery decision table** (unit, against recorded/stubbed API JSON): no releases;
  only prereleases (stable = up-to-date, beta takes it); newest-is-beta vs
  newest-is-stable; draft ignored; malformed answer; rate-limited (legible error,
  mentions the shared-IP quota).
- **Signed-channel enforcement** → replaced by **channel-switch semantics** (D-C1/D-C2):
  a beta box moved to stable that runs a newer version than latest-stable is
  **offered nothing** (no downgrade, ever), and simply tracks stable from there;
  the candidate filter itself is covered by the discovery table.
- **Flag matrix**: `--url` alone (strict), `--dev` alone (default source, no
  verification), `--url --dev` (explicit source, no verification), `--dir` unchanged,
  `--channel --url` = usage error, `--channel beta` picks the prerelease.
- **Channel precedence**: flag > env > settings-table-materialized file > stable;
  the wrapper's file read (grep-level + one integration test with a temp file).
- **CP settings wrapper** (`settings.get_setting/set_setting` over `cp_meta`):
  whitelist validation, boot-time adoption of a seeded file (file → table, once),
  and set-rewrites-file (table → file); both failure modes (DB down, unwritable
  file) degrade legibly, never silently.
- **Installer**: prompt defaults to stable, accepts beta, `--channel` validates, and
  the seeded file lands on user-data (marker-mechanism test extended).
- **QEMU** (extends the existing signing-smoke harness): a stubbed GitHub API on the
  host serving signed release sets — a stable-channel box never sees the prerelease;
  a beta-channel box does (including the seeded-file variant); a poisoned release
  (bad signature) is refused before any hash is trusted; a beta box switched to
  stable with a newer running version is offered nothing. The P2 `--dir` round-trip
  stays in the suite.
- **Bare metal (shares BM-1 with P4/P5)**: a real beta release consumed
  production-strict on both test boxes, then flipped to stable (`gh release edit`)
  and re-tested — the stable box picks it up, the beta box is not downgraded.

## 12. Gate

`make test` + QEMU green. Bare metal via the shared BM-1 drill above (the user runs
it; `make build-production` with a clean tree precedes it).

## 13. File inventory

| File | Change |
|---|---|
| `scripts/lemonseed_artifacts.py` | `RELEASES_API_DEFAULT` + `resolve_release_base()`, User-Agent, rate-limit legibility; `IMAGE_BASE_DEFAULT` removed |
| `installer/install.py` | same constants/rules, vendored (agreement pinned by test) |
| `scripts/lemonseed_update.py` | `--channel`, never-downgrade-on-switch, `check` records channel |
| `control-plane/settings.py` | **new** — thin `get_setting`/`set_setting` over `cp_meta`; release_channel whitelist; file materialization + boot-time adoption |
| `control-plane/updates.py` | check/install jobs pass the configured channel to the updater |
| `installer/server.py` | channel radio → `run_install(channel=...)` |
| `installer/install.py` | channel seeding on user-data |
| `nixos/packages/lemonseed-update.nix` | wrapper exports `LEMONSEED_CHANNEL` (file → stable) |
| `Makefile` + `scripts/dev-httpd.sh` | **new** — `make dev-httpd` (Docker busybox httpd, static-only, 127.0.0.1:8083) |
| docs | `DESIGN.md` §3.2/§8/§13, `README.md` channel note, `BACKLOG.md` resolved entry, `PLAN.md` D23 wording + new D28 |

## 14. Open questions — resolved 2026-10-20

1. `make serve-out` → **`make dev-httpd`, Docker-based** (D-C3) — wanted now.
2. Channel store → **through the CP settings table** (`cp_meta`) with the minimum
   abstraction: a thin `get_setting`/`set_setting` wrapper; the file on `/var` is
   its materialization for DB-free root jobs (D-C4).
3. Repo constant `mattcorddry/lemonseed` — **confirmed**.
4. The flip is a **`gh release edit`** documented in P4's `RELEASE.md`; no script
   (D-C1) — CI wiring stays in P4.
5. Stable with only prereleases published = **up to date**, same as candidates no
   newer than the running release — confirmed.
