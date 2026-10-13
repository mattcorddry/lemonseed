# M6.6 step 5 — Gateway error page & image-pull contract

**Status:** **S5.1 → S5.9 built and green on 2026-10-12**; bare-metal checks
green the same day. **S5.8 (shared stylesheet) is open and stays in M6.6** — see
*Build log*. Decisions T5-A … T5-H settled the same day (*Decision log*).
The design in §1/§2 changed during the build: nginx does not behave the way the
first draft assumed, so §1 and the work-breakdown rows describe the pattern that
was actually shipped, and the *Build log* records what was wrong with the plan.
**Scope:** the two things that make Lemonseed look broken at boot — an unbranded
error page with no retry, and slow/unpinned container-image pulls. No session,
policy, or portal-page changes (steps 3–4 own those).

Approvals already given: **T5-A** (static branded page), **T5-B** (JS retry loop
that reloads `_top` when the CP answers), **T5-C** = **(i) static page +
(ii) bounded nginx start ordering**, **T5-D** (skip the pull when the image is
local, and pin data-plane images at build time).

## Why (boot-window evidence)

Appliance up at 10:33:54; captured from `~/tmp/oct12/boot-window.txt`:

| Time | Event |
|---|---|
| 10:33:56 | seed writes OWUI upstream + auth-gate files |
| 10:34:07 | Docker engine started |
| **10:34:08** | **nginx started** → port 80 open to the LAN |
| **10:34:08** | **control plane "Started"** — `Type=simple`: it forked, it has **not** bound `:8080` |
| 10:34:09 | `/_app/version.json`, `/api/chat`, `/api/status` ×2, `/ws/socket.io/` all **502**, 552-byte replies |
| 10:34:12 / :13 | Valkey ready / Postgres ready |
| 10:34:26 | OWUI container created (healthy ~10:36) |

The browser had `/?embed=1` (the chat iframe) open from before the reboot, so
the **first thing the user saw was nginx's default page** — 552 bytes, not
Lemonseed-branded.

Two claims in `gateway.nix` turned out to be wrong, and they are the whole bug:

1. *"the `error_page 502 → @cp_interstitial` guarantees there is always a
   response."* Both `@cp_interstitial` and `@owui_loading` **`proxy_pass` to the
   CP**. When the CP is the thing that is down, the fallback re-fails and nginx
   serves its own page. A fallback that depends on the failed upstream is not a
   fallback.
2. *"the browser sees a `500`."* It does not, at this phase. A subrequest
   **connection failure** surfaces as **502** (17 of them in this window); the
   **500** path requires verify to *answer* 503 (session store down). Both
   statuses appear in the real stream, so both need a branded page. `DESIGN.md`
   corrected on 2026-10-12.

## Rules

- **R1 — every HTTP status the browser can see has a Lemonseed answer.** If the
  CP is reachable, the CP's interstitial. If it is not, a **static** page from
  the read-only store. Never nginx's own page.
- **R2 — a retryable error page retries.** It polls a public, unauthenticated
  endpoint and reloads the **whole window** when the appliance is actually useful
  again. It never reloads onto a login page or a still-unready shell.
- **R3 — the listener is the last thing to start.** nginx must not open :80
  until the CP answers its health check, or an instantly-failing connection
  replaces a friendly page. Bounded wait, then start anyway (R1 covers that case).
- **R4 — never pay for a pull we don't need.** If the image is already local, do
  not contact the registry.
- **R5 — production runs exactly what was tested.** Every in-tree image reference
  carries a digest, tag kept for humans. Upstream tag movement cannot outrun the
  control plane.

## Current state (verified 2026-10-12)

### Four pull paths; only the boot path gets it wrong

| Path | Skips when local? | Timeout | Owner of the slow case |
|---|---|---|---|
| `data_services.py::_pull_image` | **yes** (`:221` `if _image_present(image): return True, "present"`) | 600 s | registry-reachability gate, friendly error, retried every `SUPERVISE_INTERVAL_SEC = 10` |
| `modules.py::_pull_image` (OWUI) | **yes** (`:1232`) | 600 s | same pattern, plus a 30 s TCP probe and a 60 s failure cooldown |
| `data-services.nix` postgres `ExecStartPre` | **no** | **1800 s** (`TimeoutStartSec`) | nobody — it blocks the unit |
| `data-services.nix` valkey `ExecStartPre` | **no** | **1800 s** | same |

Both **CP** paths already implement R4. The only violators are the two systemd
`ExecStartPre` pulls, which ran on the Oct 12 boot and reported `Status:
Downloaded newer image for …` — work we did not need.

Note the boot-path pull is **not** an oversight; the in-tree comment says so:
*"Cold pulls on slow links / TCG QEMU easily exceed a few minutes."* It was the
right answer before the CP supervisor existed to own it. Measured cost of what
that comment is protecting against (store tarball sizes on this machine, so
upper bounds on the registry transfer, which ships compressed layers):

| Image | Store tarball | At 1 MB/s | At 5 MB/s |
|---|---|---|---|
| `pgvector/pgvector:pg16` | 425.6 MB | ~7 min | ~85 s |
| `valkey/valkey:8-alpine` | 40.8 MB | ~40 s | ~8 s |
| `ghcr.io/open-webui/open-webui:v0.11.0` | not in this store to measure; known-large | — | — |

### Pin state (this is an AGENTS.md violation, not a nice-to-have)

AGENTS.md: *"Pin an explicit version tag in-tree … never `:latest` / `:main`."*

| Image | Reference today | Digest? |
|---|---|---|
| Open WebUI | `control-plane/modules.py:29` `OWUI_IMAGE_DEFAULT` | tag only (`v0.11.0`) |
| PostgreSQL | `nixos/modules/data-services.nix:15` `pgvector/pgvector:pg16` | **floating** |
| Valkey | `nixos/modules/data-services.nix:16` `valkey/valkey:8-alpine` | **floating** |
| Postgres (QEMU tarball) | `nixos/modules/qemu-docker-cache.nix` `imageDigest` | digest, **different copy of the truth** |
| Valkey (QEMU tarball) | same file | digest, different copy |
| Open WebUI (QEMU image) | `flake.nix` `qemuOwuiImage` `imageDigest` | digest, third copy |

Worse, the intent is duplicated in three languages: `POSTGRES_IMAGE` /
`VALKEY_IMAGE` are **also** hardcoded in `data_services.py:33-34`, with a comment
saying "must match `data-services.nix`". A pin bump today means editing Nix twice
and Python once and hoping they agree.

### What the portal already does well (keep it)

The Getting-Ready gate in `stub.py` is a real state machine: title / workflow /
task text driven by `/api/chat` and `/api/owui`, with dedicated branches for
`rebooting`, `awaiting-choice`, the settings-reset state, `ui === 'error'`, and a
progress bar (`startup_percent`) on the startup page. `chat_ready.py` produces
honest model-download copy ("Downloading {friendly} for your hardware…", and
`awaiting-choice` explicitly warns these "are large files and may take a long
time to download on slower internet connections"). Registry-unreachable reports
**"Cannot reach the container registry — check this appliance's internet
connection. Retrying automatically."** So a slow *CP* is a good experience.

Three honest gaps, all in the *wait copy*, none of them a rendering failure:

- **No ETA anywhere.** lemond emits no download-rate data and we deleted the
  speed-probe machinery for exactly this reason. Copy may say "this happens once
  and can take a few minutes" and must stop there — we will not invent progress.
- **The OWUI pull is under-described.** While the image downloads, `modules.py`
  reports `{"state": "pulling", "image": …}` with no `workflow`/`task`, so the
  gate's `!uiReady` fall-through shows **"Configuring chat / Preparing the chat
  interface…"**. Not a lie, but a first-boot multi-GB download looks like a
  two-second job.
- **`/api/data` is not part of the chat gate.** The gate reads chat + OWUI +
  policy, never data-plane state, so while Postgres is still starting the user
  reads "Preparing the chat interface" — branded and harmless, but it blames the
  wrong component.

Both copy gaps are one-branch fixes in the gate; they are **T5-F**, not free work
hidden in another row.

## Design

### 1. The not-ready page: static, chosen by the failing location (T5-A + T5-B)

**As built** — and this is not how the first draft had it. Two nginx rules broke
the original design, both found with a live nginx rather than by reading docs
(`make test-gateway` keeps them from being re-forgotten):

1. `alias` is **rejected** inside a named location: `the "alias" directive
   cannot be used inside the named location`. That is a config that reads fine
   and does not parse — nginx would have refused to start, i.e. no gateway at all.
2. **`error_page` does not run inside a location reached *through* `error_page`.**
   nginx blocks error-page recursion. So the shipped pattern —
   `error_page 502 → @cp_interstitial → proxy_pass http://127.0.0.1:8080` — could
   never render a friendly page when the CP was the thing that was down: the
   nested fallback was dead code, and the browser got nginx's stock page. Which
   is precisely the bug this row was opened for. Nesting cannot fix it; the
   fallback has to be picked by the **originating** location.

So the fallback is an `internal` location (not a named one), and the choice of
fallback for `/` is a map for the same reason the upstream is a map — for a
document request the upstream *is* the CP, so only a static page can render:

```nginx
map "$http_sec_fetch_dest:$arg_embed" $lemonseed_error_target {
  default      /_lemonseed/cp-down;   # document: the CP is the upstream
  "iframe:"    /_lemonseed/interstitial;   # embed: OWUI is, so the shell can talk
  …
}
location /_lemonseed/cp-down {
  internal;
  root ${cpStatic};                   # store path; no CP involved
  try_files /cp-down.html =503;       # `alias` is fine here, not in @name
  default_type text/html;
  add_header Cache-Control "no-store" always;
}
location /_lemonseed/interstitial {
  internal;
  proxy_pass http://127.0.0.1:8080$request_uri;   # redirect rewrote $uri
  …
}
# server level: covers every location with no error_page of its own
error_page 500 502 503 504 =503 /_lemonseed/cp-down;
```

`=503` rather than `=`: the body is a friendly page but the response really is
"unavailable", so the status stays honest for curl, monitoring and the retry JS.
Note that `proxy_intercept_errors` stays off, which is what keeps a **500
produced by the CP** passing through untouched instead of being disguised as
"still starting" — a real bug stays visible.

`@cp_interstitial` is **gone**: all 17 CP locations that referenced it were
re-pointing at the service that had just failed. `@owui_loading` stays for the
two locations whose upstream is OWUI, where the CP genuinely has news. Every
gated location needs its own 5xx clause rather than relying on inheritance,
because `auth-gate.conf` contributes a 401 `error_page` at location level and any
`error_page` at that level replaces the server-level set — that gap was found by
the test, not by reading (`test_every_cp_location_brands_five_xx`).

The page is self-contained: inline CSS, and its only subresource is the brand
mark, which nginx serves from SquashFS rather than from the service that is
currently down. Retry loop per T5-B: poll **`/api/status`** (approved over
`/healthz`, because "useful again" means the CP has its data plane, not merely
that it answered a socket), every 4 s with a ±25 % jitter, then

```js
if (data && data.status === "ok") window.top.location.reload();
```

Any well-formed JSON answer is the signal, **not** a readiness field: there is no
`phase` on `/api/status` (the draft assumed one), and the CP shell owns the finer
"still starting" states, so waiting for anything more here would keep the static
page on screen while the shell already had something better to show. Text: **"Lemonseed is starting up"** + the elapsed time we
have been waiting + a note that a cold start takes a minute or two, plus
`<noscript>` with a manual reload link.

### 2. Ordering: bounded wait, not a hard dependency (T5-C ii)

On `lemonseed-gateway.service`:

```nix
gatewayWaitSeconds = 20;                      # a hang must not become a boot stall
gatewayWait = pkgs.writeShellApplication { … };  # curl -fsS /healthz, 0.5s apart
services.nginx.preStart = "${gatewayWait}/bin/lemonseed-gateway-wait";
```

As built: `services.nginx.preStart` is the nginx module's own hook into
`ExecStartPre`, so the poll lands in the unit without replacing the module's
`nginx -t` check. The waiter is a real script rather than an inline snippet
because systemd expands `$VAR` in an `ExecStart*` line — `$(date +%s)` belongs
where the shell, not the unit parser, reads it. **`Restart=` was not added:** the
nginx module already sets `Restart=always` / `RestartSec=10s`, which is strictly
stronger than the `on-failure` this section asked for, and a test asserts we do
not quietly downgrade it.

A hard `After=` is explicitly rejected: `lemonseed-control-plane` is
`Type=simple` + `Restart=always`, so "unit started" ≠ "port bound" (that is the
10:34:08 line above), and a hard dependency on a service that can loop on boot
would turn the friendly page into `ERR_CONNECTION_REFUSED`. The bounded poller
gets the good case; the static page gets the rest. Both are needed; that is why
T5-C is (i) **and** (ii).

### 3. Take the pull out of the boot path entirely (T5-D, part 1)

**Not** "skip the pull when local" — that still leaves a cold first boot pulling
inside a systemd unit for up to 30 minutes with the data plane down and the CP
unable to provision. Instead the boot units stop owning pulls at all:

```nix
# data-services.nix, both units
ExecStartPre = deleted;              # no registry contact during boot
TimeoutStartSec = "1800" → "120";    # only docker start/run of a local image now
```

and the start scripts gain one branch (shared `image_present()` helper):

```sh
# Reuse a stopped container as today; but when there is nothing to reuse and the
# image is not local, hand off instead of pulling in the boot path.
if ! image_present "$IMG"; then
  echo "image not local; control-plane supervisor will pull and start it"
  exit 0
fi
```

Why this is safe (all verified, not assumed):

- The CP supervisor **already** does exactly this job better: skip-when-present,
  reachability-gated, 600 s timeout, friendly `waiting` error, retried every
  10 s (`supervise_data_once` → `_supervise_one` → `_start_container`).
- `ExecStartPre` already had a leading `-`, so its failure was already ignored —
  the pull was never load-bearing, only its **duration** was.
- **Nothing `Requires=` or `BindsTo=` these units** (grep of `nixos/` +
  `control-plane/`): only `before=`/`after=` ordering from
  `lemonseed-data-init` and `lemonseed-docker-network`, plus `systemctl
  is-active` reads for diagnostics. Exiting 0 without starting is functionally
  inert.
- The units are `Type=oneshot` with `RemainAfterExit=true` and **no**
  `Restart=`, so a failed unit was never coming back anyway; the CP was always
  the healing mechanism.

One cosmetic artifact to expect: after a hand-off, `systemctl is-active
lemonseed-postgres` says **active** (`RemainAfterExit`) while the container is
not running. `/api/data` stays authoritative — `postgres_snapshot()` probes the
port, not the unit — so the portal and diagnostics tell the truth.

**The 600 s pull cap is too small for the big images** and is the remaining slow
link. A 425 MB download needs ~700 KB/s sustained to fit in 600 s; below that the
attempt fails and retries (Docker keeps completed layers, so retries do converge,
but slowly). Fix: a per-image `_PULL_TIMEOUT_SEC` (valkey 300 s, postgres 900 s,
Open WebUI 1800 s). These pulls are off the boot path, so a long timeout costs
nothing but a later heal. Bounded wart we accept: the data supervisor is one
daemon thread, so during a slow Postgres pull Valkey waits up to one attempt.
First-boot only, self-clearing.

### 4. One pins file, digest + tag separate (T5-D, part 2)

**`control-plane/container-images.json`** becomes the single source of truth:

```json
{
  "open_webui": { "name": "ghcr.io/open-webui/open-webui",
                  "tag": "v0.11.0",
                  "digest": "sha256:72c0ba641ba75e7aa52655cb242570906ececd09b1140fb736483038a22b3228" },
  "postgres":   { "name": "pgvector/pgvector", "tag": "pg16", "digest": "sha256:ccc6e83d…" },
  "valkey":     { "name": "valkey/valkey", "tag": "8-alpine", "digest": "sha256:e0eb7c48…" }
}
```

Two consumers, one file:

- **Nix at eval time** — `builtins.fromJSON (builtins.readFile …)`:
  `data-services.nix` builds its image refs from it, and
  `qemu-docker-cache.nix` / `flake.nix` take `imageDigest`/`finalImageTag` from it
  instead of hand-copied digests. (The nix fixed-output `hash` still has to be
  refreshed by a build; that is unavoidable and already the workflow.)
- **Python at runtime** — `data_services.py` and `modules.py` read the copied
  store path (`cpRoot` already receives named files; add this one to the list in
  `control-plane.nix` and to `PackagingTests`). Tag and digest are **separate
  fields**, which is what makes this compatible with the rest of the product:
  `_owui_version_label()` and the "Open WebUI v0.11.0" row need the human tag,
  and Docker wants `name@sha256:…` for the actual reference.

**Resolution rule** (used by the start scripts and the CP supervisor):

1. `name@digest` present locally → use it. *(strongest)*
2. else `name:tag` present locally → use it, and if that image has a
   `RepoDigest` that differs from the pin, log a warning.
3. else `docker pull name@digest` → use it; if the digest pull fails, fall back
   to `docker pull name:tag`.

Step 2 is not a loophole: it is exactly today's behavior, and it is **required**
for QEMU, where images arrive as `dockerTools.pullImage` tarballs. `docker save`
carries `RepoTags` but not `RepoDigests`, so a loaded image **cannot** satisfy a
`name@sha256:` reference. **Verification item:** confirm with the QEMU run that
the fallback fires and the containers still start; if digest references do work
with loaded images here, simplify the rule.

**`make refresh-image-pins`** (packaging-time, never at boot): for each entry,
resolve the current digest for the pinned tag with `docker manifest inspect
--verbose`, and rewrite the file with an explicit per-entry opt-in
(`REFRESH=open_webui make refresh-image-pins`), refusing to write anything if
Docker or the registry is unavailable. Pin bumps then go through review like any
other dependency change, validated by `make test` + QEMU before they ship — the
workflow AGENTS.md already asks for.

### 5. Hygiene that rides along (both small, both found this week)

- **`sessions.ping()` has no caller.** Session-store health must be visible,
  because "Valkey is down" now means "nobody can sign in". Report it in
  `/api/status` (`session_store: "ok" | "unavailable"`) and in the advanced
  panel.
- **The CP leaves prior-boot OWUI containers behind.** A stopped
  `lemonseed-owui-18081` from Oct 8 was still on the box on Oct 12. Add a reap of
  `lemonseed-owui-*` containers that are not in the current desired set, run once
  at supervisor start.

## Work breakdown (each row is `make test` green; the last row adds QEMU)

| Row | What shipped | Tests |
|-----|--------------|-------|
| S5.1 ✅ | `container-images.json` + `image_pins.py`; readers in Nix eval (`data-services.nix`, `qemu-docker-cache.nix`, `flake.nix`) and CP runtime (`data_services.py`, `modules.py`); duplicated refs deleted | `test_image_pins.py` (schema, malformed pins, cache, one-source-of-truth scan, Nix-reads-the-JSON); digest/tag tests in `test_db.py` + `test_modules.py` |
| S5.2 ✅ | `scripts/refresh-image-pins.py` + `make refresh-image-pins` / `make check-image-pins` | `scripts/test_refresh_image_pins.py` (20 tests, registry injected; no docker, no network) |
| S5.3 ✅ | Both `ExecStartPre` pulls deleted; `TimeoutStartSec` 1800→120; `resolve_image` + hand-off in the start scripts; image-drift recreate on the boot path; per-image pull budgets | source-text tests in `test_image_pins.py` (`BootPathContractTests`) + per-key timeout/ref tests in `test_db.py` |
| S5.4 ✅ | `cp-down.html`; `/_lemonseed/cp-down` + `/_lemonseed/interstitial`; map-driven target for `/`; server-level fallback; `@cp_interstitial` deleted | `test_gateway_config.py` (18) + **live nginx probe**: `scripts/gateway-error-probe.sh` / `make test-gateway` |
| S5.5 ✅ | Bounded `preStart` poll; `Restart` left at the module's `always` | `test_gateway_config.py` (`GatewayOrderingTests`) |
| S5.6 ✅ | `sessions.store_status()` in `/api/status`; `reap_stale_containers()` before the first worker tick | `test_sessions.py` (cache + unavailable), `test_control_plane.py` (field present), `test_modules.py` (reap rules + ordering) |
| S5.7 ✅ | Storage-vs-chat copy in `_owui_progress_copy` (the "pulling" branch already existed) | `ProgressCopyTests` |
| S5.8 ⬜ | **Open, stays in M6.6** — the shared stylesheet root. Not built with this row on purpose; see *Build log*. Extract `stub.STYLE` → `control-plane/static/lemonseed.css`, serve it at `/_lemonseed/static/` exactly as `/_lemonseed/brand/` is served (`alias` from SquashFS, `gateway.nix:580`), link it next to `brand_head_links()` and drop the inline `<style>` from both `stub.py` and `cp-down.html` | no test asserts inline CSS today (checked: no `test_*.py` matches a CSS literal), so this is a rendering review, not a test rewrite |
| S5.9 ✅ | Boot-log assertions, plus the fix they earned: both start scripts announce their image reference on **all four** paths | new tier `test_boot_scripts.py` (7 tests, runs the *rendered* shell under a stub `docker`); smoke-test assertions for the announcement, the gateway poll and the fallback's 404 |

S5.8 is the stylesheet root the user asked for (one CSS file for CP pages *and*
nginx pages; prerequisite for dark/light themes). **Decided 2026-10-12: it stays in
M6.6** rather than moving to M7 — the milestone that owns the error pages owns the
sheet they link. Scope correction found while folding it in: this row was described
as touching "every portal test that asserts inline CSS", but **no test does** (no
`test_*.py` contains a CSS literal), and `stub.STYLE` is only interpolated into
four page templates. The work is the move itself plus a visual pass over the four
pages, not a test rewrite.

## Tests

**Already covered — do not rewrite**
- `test_db.py:417` `test_pull_skips_docker_when_image_present` already pins the
  CP supervisor's skip (`data_services._pull_image` → `"present"`), and
  `test_db.py:406-411` covers the unreachable-registry branch. Note there is no
  `test_data_services.py`; `data_services.py` is tested from **`test_db.py`**.

**New unit tests**
- `test_modules.py`: `ensure_ready` skips `docker pull` when the ref is local.
  (The pull-when-absent branch is already covered — `test_modules.py:82/95/109`
  patch `_image_present` to `False`; the `True` case is the gap.)
- New `test_image_pins.py`: parse `container-images.json`; every entry has
  `name`/`tag`/`digest`; digest matches `^sha256:[0-9a-f]{64}$`; **no `:latest`
  and no un-pinned reference anywhere in `nixos/` + `control-plane/`** (grep the
  tree in-test). Same file asserts the boot path is pull-free by reading
  `nixos/modules/data-services.nix` as text — no `ExecStartPre` line, and
  `TimeoutStartSec = "120"`. This follows the existing precedent in
  `test_control_plane.py` `PackagingTests`, which already reads
  `control-plane.nix` as text.
- `test_data_services` cases **in `test_db.py`**: per-image `_PULL_TIMEOUT_SEC` is
  what the `docker pull` call actually receives (assert the `timeout=` kwarg).
- `test_control_plane.py`: `/api/status` reports `session_store`
  `"ok" | "unavailable"` (S5.6 — `sessions.ping()` finally gets a caller), and
  `test_modules.py`: the boot-time reap removes `lemonseed-owui-*` containers
  outside the desired set and leaves the active slot alone.
- `test_control_plane.py` `PackagingTests`: `container-images.json` is in the
  `cpRoot` copy list in `control-plane.nix` (the step-2 guard already covers
  `sessions.py`; a missing entry here boots-loops `FileNotFoundError`, exactly
  the failure mode that guard exists for).

**Not unit-testable — goes to QEMU.** `make test` has **no Nix-eval tier**
(`scripts/run-unit-tests.sh` runs check-untracked, lemonseed-update, installer,
gen-repart, control-plane only). So assertions about *rendered* config belong in
`scripts/qemu-api-smoke-test.sh`, which already greps live files on the guest:
  - the rendered nginx config contains the `@cp_down` location and its static
    `alias`, and the `@cp_down`/interstitial blocks contain **no** `proxy_pass`
    that could re-fail;
  - `systemctl cat lemonseed-postgres lemonseed-valkey` shows no `ExecStartPre`
    and `TimeoutStartSec=120`;
  - the nginx unit shows the bounded `ExecStartPre` health poll;
  - the digest→tag fallback fires (containers start from preloaded tarballs).

**Obsolete tests to delete or rewrite**
- `test_modules.py:51` `test_owui_image_default_is_pinned_release` asserts
  `image.startswith("ghcr.io/open-webui/open-webui:v")` and `":v0.11." in image`.
  A digest reference (`name@sha256:…`) breaks both. Rewrite against the pins file:
  the **run** reference carries a digest, and the **display** label still yields
  `v0.11.0`. Delete the old tag-prefix assertion rather than loosening it into
  meaninglessness.
- `test_control_plane.py` portal tests were expected to pin the literal CSS string
  from `stub.STYLE`; **none do** (verified 2026-10-12), so nothing here went
  obsolete and S5.8 inherits no test rewrites either.
- Nothing from steps 1–2 becomes obsolete here.

## Decision log (review 2026-10-12)

- **T5-E — digest→tag fallback: kept.** Because `docker save`/`load` carries
  `RepoTags` but not `RepoDigests`, the offline QEMU profile *needs* rule 2.
  Obligation: **S5.9 must show the fallback firing** (containers start from
  preloaded tarballs), not merely that the boot succeeded.
- **T5-F — wait-copy honesty: both approved.** OWUI `pulling` gets its own copy;
  the gate consults `/api/data`. Row **S5.7**.
- **T5-G — answered, and bigger than the question asked.** The `ExecStartPre`
  pulls are **deleted**, not shortened: skipping-when-local still leaves a cold
  first boot pulling inside a unit for up to 30 minutes with the data plane down
  and the CP unable to provision. See *Take the pull out of the boot path*
  (§3) for the branch that replaces it and the evidence that nothing else
  depends on these units. `TimeoutStartSec` 1800 → 120.
  First-boot-on-a-slow-link answer: the CP **cannot** fail on a slow pull —
  nothing on its path waits for one; the pull runs in a daemon thread that
  reports `waiting` + the registry message and retries every 10 s. The real
  user-visible consequence is narrower and honest: **login and admin setup need
  Postgres**, so on a cold boot they are unavailable until the data plane is up,
  which is exactly what the new wait copy will say. Bounded wart accepted: one
  supervisor thread, so a slow Postgres pull delays Valkey by at most one attempt.
- **T5-H — no digest in the UI at all.** Principle adopted: *don't show the user
  data they cannot act on.* The UI row stays "Open WebUI v0.11.0" (derived from
  the tag). Digests live only in a diagnostics API response (e.g.
  `/api/diagnostics`), for us and for support — never in the settings page or
  any user-facing panel.

## Build log (2026-10-12, S5.1 → S5.9)

**What the machine told us that the plan got wrong.** Three nginx rules assumed
incorrectly, all caught by running nginx instead of reasoning about it — worth
keeping because each one would have been a boot-time failure, not a cosmetic one:

| Assumption in the draft | Reality | Consequence had we shipped the draft |
|---|---|---|
| `alias ${page}` in `location @cp_down` | `alias` is rejected in a named location | nginx refuses to parse the config → **no gateway at all** |
| `error_page … = @cp_down` nested inside `@cp_interstitial` / `@owui_loading` | error_page does not fire in a location reached via error_page | fallback silently dead; users keep getting nginx's stock page (the original bug, unfixed) |
| `/api/status` has a `phase` field | it does not (it has `startup_ready`, `setup_complete`, …) | retry JS waits for a field that never appears → the page never reloads |

**Also learned:** `proxy_intercept_errors` is off, so `error_page` only fires for
errors nginx *generates* (connect refused, timeout, no live upstreams) and never
for a status the backend produced. That is a feature here — a genuine CP 500
stays visible instead of being painted over with "still starting" — and the probe
asserts it.

**`@cp_interstitial` was doing nothing, and never was.** All 17 gated CP
locations rewrote 5xx to a proxy to the service that had just failed; under the
recursion rule above, the second failure always fell through to nginx's own page.
Deleting it is a simplification, not a regression. The reboot-interstitial story
in the old comment is better served now: during a reboot the CP is by definition
unreachable, so the static page with a self-reloading retry is the only thing
that could ever have rendered.

**Pin state after S5.1–S5.3, reported honestly.** `make check-image-pins` on
2026-10-12 says:

```
open_webui  ghcr.io/open-webui/open-webui:v0.11.0  72c0ba641ba7…  same
postgres    pgvector/pgvector:pg16                 7b822b0aac60…  moved
valkey      valkey/valkey:8-alpine                 081c2f5cb575…  moved
```

The pins file deliberately carries the **Nix/QEMU-validated** digests, so the
appliance runs exactly what CI tested and no fixed-output `hash` had to move in
this row. The consequence is that on a box that had already pulled the newer
tag, the pinned Postgres/Valkey is a slightly older patch release of the same
major (data-dir compatible). Forward path is one command plus a validated
rebuild: `make refresh-image-pins`, then update the two `pullImage` `hash`
values the tool points at, then `make test && make build-qemu && make test-qemu`.

**Image drift converges one boot late.** The supervisor never recreates a
running data-plane container (that would SIGKILL a live Postgres mid-session);
only the boot path recreates on a reference mismatch, and it compares
`.Config.Image` as a **string** — never the image ID, because the classic store
reports the config digest while the containerd store reports the manifest
digest, and an ID compare can loop forever. So a pin bump takes effect on the
next boot after the supervisor has pulled the new digest. Deliberate: an extra
reboot is cheap, a killed database is not.

**Test tiers, and what each can actually see.**

| Tier | Sees | Gateway coverage |
|---|---|---|
| `make test` | Python + repo source text + the **rendered** start scripts run under a stub `docker` | pins, boot-path directives, which boot path a real start takes, error-page routing, the not-ready page's copy and subresources, ordering of the nginx unit |
| `make test-gateway` (new) | a **real nginx**, live | status codes and bodies for every branch of the contract, plus the negative control that `alias` in a named location is still rejected |
| `make test-qemu` | a booted appliance | the tag fallback firing from preloaded tarballs, the announced image reference on the path a real boot takes, the gateway polling the CP before `:80`, the fallback not reachable from the LAN |
| bare metal | a real upgrade | stop the CP and reload `/` (the one check no automated tier can do without a guest shell); upgrade an existing box so `ALTER TABLE` and the no-boot-pull hand-off run against real state |

Two things the source-text assertions caught that a careful reading missed: a
gated location (`= /index.html`) with no 5xx clause at all — invisible under
inheritance because the included 401 clause suppresses it — and a new script
quoting an image reference instead of reading the pins file.

**S5.8 pulled back into M6.6, with a correction.** The draft assumed the shared stylesheet had
to be inlined because a page served while the CP is down cannot depend on the CP.
That part is right, but the conclusion is not necessary: nginx can serve
`/_lemonseed/static/lemonseed.css` straight from SquashFS the same way it serves
`/_lemonseed/brand/`, so an error page *can* link one shared sheet. Deleting
`stub.STYLE` (196 lines, 72 selectors, four page templates) in the same change as
the gateway error contract would put a visible regression risk on every CP page
inside a row that is about boot-window behaviour. Proposed as its own row: extract
to `control-plane/static/lemonseed.css`, serve via `/_lemonseed/static/`, have
`cp-down.html` link it and drop its inline copy, then the palette duplication
disappears. Until then `cp-down.html` is intentionally self-contained.
On 2026-10-12 the deferral was reversed: the milestone that owns the error pages
owns the sheet they link, so S5.8 stays in M6.6 as open work. Note also that the
row's original justification ("touches every portal test that asserts inline CSS")
was wrong — no such test exists — so this is a small, low-risk row.

## Build log: S5.9, the assertion that could not fail

The first QEMU run of this row failed with *no* `running pgvector/pgvector:<tag>`
line. The echo existed — it sat after the `exit 0` and `exec docker start`
branches. Both units are `Type=oneshot` while the containers are
`--restart unless-stopped`, so dockerd has them running again ~20 s before the
unit executes: the reuse path is what **every** boot takes except the first, and
it never said which image it was reusing. On a pin bump that is the only question
anyone has.

The fix announces the reference on all four paths (`reusing the running`,
`starting the existing`, `created`, `image unknown` → hand-off), reading
`.Config.Image` back from the container when the unit did not start one itself.
From the log of the passing run:

```
lemonseed-postgres-start[2408]: lemonseed-postgres: image pgvector/pgvector:pg16 (reusing the running container)
lemonseed-valkey-start[2409]:   lemonseed-valkey: image valkey/valkey:8-alpine (reusing the running container)
nginx-pre-start[1016]:          lemonseed-gateway: control plane is serving; opening :80
```

The tag form is the digest→tag fallback firing for real: this profile loads its
images from `dockerTools.pullImage` tarballs, which carry `RepoTags` and no
`RepoDigests`. The third line confirms a unit's `preStart` stdout does reach the
serial console, which the ordering assertion depends on.

**Process lesson, kept because it generalises.** The original assertion grepped
the module's *source text*, so it checked that a string appeared in a Nix file
rather than that a line would ever be printed. A test that cannot fail is worse
than no test — it takes the check off the list of things to verify.
`test_boot_scripts.py` now renders the shell out of the module, substitutes the
interpolations and **executes** it against a stub `docker`, covering all four
branches offline in 0.2 s instead of one 30-minute boot. Checked by mutation:
deleting the reuse-path echo from a copy of the module makes two of its seven
tests fail.

## Out of scope (do not drift here)

- Guard-order changes (`/login` before `_require_login`) — deferred by decision 1.
- The `/_lobudi` 404 (upstream OWUI bug; workaround in `gateway.nix`).
- Making the appliance boot without a network, offline install, registry mirrors.
- `:8080` access contract — **M6 close-out item**, still open.
- Login redirect (`/login?rd=`) and the JSON-403 contract: step 3, not here.
