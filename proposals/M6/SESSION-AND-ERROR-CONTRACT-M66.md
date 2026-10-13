# M6.6 — Session & error contract

**Status:** **built 2026-10-13.** Steps 1–5 are all in, plus the S5.8 stylesheet
extraction. `make test` and `make lint` green at every step; `make test-gateway`
and `make test-qemu` green (see the *Gate* section for how the QEMU runs were grouped).
Step 5's error-page half did **not** ship as drafted here — read
[`GATEWAY-ERROR-AND-PULL-CONTRACT-M66.md`](./GATEWAY-ERROR-AND-PULL-CONTRACT-M66.md)
for what actually happened (a static `/_lemonseed/cp-down` page chosen by
`error_page … =503`, because nginx will not run `error_page` inside
`@cp_interstitial`). The deltas from the work-breakdown table are recorded under
*What changed while building* so this stays the accurate record of the design.
**Scope:** M6 close-out. Everything here is portal/CP/gateway behavior; no model,
data-plane, or Open WebUI policy changes.

## Review outcomes (2026-10-12)

1. **Guard ordering: keep as-is, deferred.** No session-state check is added in
   front of the guards; that complexity stays out until the guard surface grows.
   See *Deliberately not here*.
2. **The stylesheet is extracted to a static file.** Approved, with a second
   reason that outlives this milestone: a **single stylesheet root for the whole
   system** is what makes user themes (dark/light) tractable later, so the static
   gateway pages and the CP must share one CSS file rather than each owning a
   copy. Promoted from a sub-decision to a step-5 deliverable.
3. **Reboot behavior is a tradeoff, not a goal.** Sessions surviving a reboot is
   the better UX; logging everyone out is merely acceptable. Keying is provisional
   — see D2.
4. **Docker-dependency framing corrected.** There is no Postfix in this project
   (zero matches repo-wide; outbound mail is backlog only, and expected to use the
   user's own provider credentials rather than a :25 relay, most ISPs block it).
   The real precedent is PostgreSQL and Valkey themselves — see D2.

## Why this is in M6

Three separate bug reports turned out to be one missing design:

1. An admin whose session expires mid-form gets a **"managed by Lemonseed"**
   denial (or silently dead UI) instead of a sign-in prompt.
2. Disabling or demoting a user has **no effect** until their cookie ages out
   (up to 10 h).
3. At appliance startup the browser shows a **raw nginx error page**, not a
   Lemonseed one.

Each has a different owner and a different layer, which is why none of them got
fixed by fixing one of them. The common root: **nothing in the system states what
an HTTP status code means to the portal, and nothing can revoke a session.**

## Current state (verified)

- **No session store.** `auth.issue_token` mints a stateless HMAC-SHA256 token
  whose only expiry is `exp = now + SESSION_ABSOLUTE_SECONDS` (36000 s).
  `SESSION_INACTIVITY_SECONDS = 3600` is defined and read by nothing.
- **Verify never touches Postgres.** `stub._handle_session_verify` answers from
  `auth.verify_token` alone, so the token's embedded groups are authoritative
  until it expires. `auth.rotate_session_secret()` exists and has **zero**
  production call sites — `_handle_logout` clears the caller's cookie and revokes
  nothing.
- **A gated endpoint cannot return 401 to `fetch()`.** The gate is
  `error_page 401 = @auth_unauthorized` → `return 302 /login?rd=$request_uri`.
  `fetch()` follows the redirect and hands back **login HTML at HTTP 200**, so
  `if (!res.ok)` never fires.
- **403 means two unrelated things.** An OWUI guard denial is JSON with `detail`
  (`stub.py` guards, `default_type application/json`); "you are not an admin" is
  `stub._require_admin` → **403 `text/plain "forbidden"`**. Clients cannot
  distinguish "not you" from "not yours".
- **The one existing handler that gets this right is dead code.**
  `stub.refreshStatus` maps `401 || 403` to
  `window.location.replace('/login?rd=…')`, but it polls `/api/status`, and
  `gateway.nix` defines `locations."= /api/status"` with `cpProxy` and **no**
  `authGate` — an unauthenticated location that never answers 401 or 403. The
  branch is unreachable twice over, and `test_control_plane.py:1728` pins it with
  a string assertion (`assertIn("window.location.replace('/login?rd='", html)`),
  which is how it survived.
- **The `rd` round-trip is broken at all three steps.** nginx passes
  `$request_uri` **unencoded** (`/login?rd=/settings/?tab=x` truncates at the
  second `?`); `stub.page_login` does emit a hidden `rd` input, but the form
  submits by `fetch()`, follows the server's 302, cannot read `Location`, and
  falls back to `'/'`; and the sanitizer
  `if not rd.startswith("/") or rd.startswith("//")` accepts `/\\evil.com`.
- **`@cp_interstitial` cannot render when the CP is down** — it `proxy_pass`es to
  `127.0.0.1:8080`. nginx being up while the CP is still booting is precisely the
  case that triggers it, so the branded-page claim in §3.2 was aspirational.
- **`/api/status` is fully open** (no `authGate`) and its payload includes
  `auth.auth_status()` (`stub.py`'s status handler), which returns `users` and
  `admins` counts from `auth.account_counts()`. So any device on the LAN can read
  household account counts without signing in.
- **Sessions have nowhere to live yet.** Valkey is real, but it is a **Docker
  container published to `127.0.0.1:6379`**, started by the CP's own oneshot
  systemd units (`nixos/modules/data-services.nix`, `valkey/valkey:8-alpine`,
  `valkey-server --save 60 1`). The CP currently only *probes the port*
  (`data_services.valkey_ready`); it has no Valkey client.
- **There is no schema migration path.** `db.SCHEMA_VERSION = 3` is recorded into
  a metadata row, but every statement in the repo is `CREATE TABLE IF NOT EXISTS`
  and there is **not one `ALTER TABLE`** anywhere. A new `token_generation` column
  would therefore silently no-op on every already-deployed box.

## The contract

One rule for every portal request, applied consistently:

| Status | Meaning | Client behavior |
|--------|---------|-----------------|
| **401** | No valid session | `window.top.location = '/login?rd=' + encodeURIComponent(<current path>)`. `_top`, not `location`, because the portal lives inside an iframe. |
| **403** | You are signed in; this is not yours | Surface "signed in, but not permitted" in place. **Never** redirect — a redirect would throw away typed input and hide the reason. |
| **403 (managed)** | Signed in, but Lemonseed owns this setting | Show the guard's `detail`. Distinct from the row above by a JSON body carrying a managed-marker, not by a new status code. |
| **5xx** | The appliance is broken or starting | Retry with capped exponential backoff; after N failures show the degraded state. Never redirect. |

Server-side, the same discipline:

- Every CP JSON endpoint answers with a **JSON body and a real status code**, and
  never redirects on behalf of an XHR. `_require_admin` becomes JSON with
  `detail`, matching the guard shape (guards already set
  `default_type application/json` for exactly the reason OWUI's "Server
  connection failed" message taught us).
- Gated HTML navigations may still 302 to `/login`. The distinction is *XHR vs
  navigation*, and the portal sends `X-Requested-With: fetch` on its own requests
  so the CP never has to guess.
- `rd` is URL-encoded by nginx (`$request_uri` → escaped when composed), accepted
  only if it starts with a single `/` and contains no `\`, no `//`, and no scheme.

## Decisions

### D1 — Revocation: per-user generation counter in Postgres

*Chosen over:* rotating the per-box session secret.

`auth.rotate_session_secret()` is a **global** logout — the right hammer for "the
appliance was stolen", useless for "revoke Priya". A per-user counter
(`users.token_generation`, embedded as `gen` in the token and compared on every
verify) revokes exactly one account and makes disable / demote / delete /
logout-all take effect on the next request. Cost is one indexed primary-key read
per verify.

**No cache.** Household scale is a handful of concurrent users, and Postgres plus
the OS page cache already sit under this; a TTL cache would only add a window in
which a revocation has not taken effect — the exact bug being fixed. If profiling
ever says otherwise, a ~30 s TTL is ~5 lines and can be added then.

**Prerequisite: a migration runner.** `SCHEMA_VERSION` is currently decorative, so
adding the column needs real `ALTER TABLE` execution at startup — without it the
feature works in CI and silently does nothing in the field.

### D2 — Session state in Valkey, keyed by boot id; gate fails closed

*Chosen over:* staying stateless, and storing sessions in Postgres.

A **sliding** window (touch the TTL on each authenticated request, keep an
absolute ceiling) needs server-side state. Valkey is already running for the data
plane, this state is disposable, and an in-memory store keeps the verify path fast
— the same shape Authelia uses.

Two consequences we accept deliberately:

1. **Login depends on a container the CP starts.** This adds no *new* class of
   dependency: PostgreSQL and Valkey are already containers, already started by
   the CP's own oneshot units, and already supervised by `ensure_data_worker`.
   M6.6 makes the CP *use* a container it already owns instead of only probing
   its port. The circularity check (CP starts containers / CP depends on a
   container) is satisfied because the loop is self-healing rather than
   deadlock-capable: the supervisor recreates Valkey, and until it is back the
   gate fails closed instead of blocking startup.

   If Valkey is down, `/_lemonseed/session-verify` returns **503** and the browser
   gets the branded gateway error page. The gate **fails closed**: serving an
   authenticated surface without being able to check the session is not an
   acceptable fallback, and chat is already down in that state anyway.
2. **A reboot logs everyone out — an accepted tradeoff, not a goal.** Keeping
   sessions across a reboot is the better UX and remains worth having; it is
   simply not a requirement, and it is not worth buying at the cost of disabling
   persistence on a container the data plane shares (`valkey-server --save 60 1`
   does snapshot, so surviving a reboot would otherwise be the default). Session
   keys are therefore namespaced by boot id (`/proc/sys/kernel/random/boot_id`),
   which discards them.

   **Provisional, and cheap to revisit before M7.** Two ways to keep sessions
   across reboot, both undecided: (a) drop the boot-id prefix and accept that an
   RDB restore resurrects live sessions; (b) keep the prefix but derive it from a
   secret stored in Postgres instead of `boot_id`, so a reboot re-derives the same
   namespace. Pick one before anything else starts reading session keys — the
   prefix is in the key, so changing it later means either a dual-read window or
   logging everyone out once more.

Packaging: the CP needs `python3.pkgs.redis` (8.0.1 in nixpkgs) added to
`python3.withPackages` in `nixos/modules/control-plane.nix`. That package list
must not grow testing dependencies; unit tests mock the client.

### D3 — Frame-aware login page is acceptable

*Chosen over:* forcing every entry point through a top-level navigation.

The portal is one iframe, and the login page can be reached inside it when a
redirect fires from within. So the login page is **frame-aware**: if
`window.top !== window.self`, it breaks out to `window.top.location` rather than
rendering a page inside a page (which is what users see today, and what the dead
`refreshStatus` branch was reaching for).

This is safe because the only traffic that will ever bypass the session gate is a
bearer-token OpenAI-compatible endpoint (LiteLLM, M7), which can run on its own
port rather than weakening the cookie story.

### D4 — Split `/api/status`

*Chosen over:* gating all of it, or leaving it open.

`/api/boot` stays open for the installer handoff. `/api/status` splits into an
unauthenticated subset — only what the landing page and installer need (chat
readiness phase, model family, setup state) — and the existing full status behind
the gate. This is not hypothetical: `/api/status` is ungated today **and** embeds
`auth.auth_status()`, whose `users` / `admins` counts come straight from
`auth.account_counts()`. Household account counts do not belong in a pre-login
response, and today they are all in one open endpoint.

## Work breakdown

| Step | Change | New tests | Obsolete tests removed |
|------|--------|-----------|------------------------|
| **1. Schema** | Migration runner in `db.py` (execute-on-startup `ALTER TABLE` keyed to `SCHEMA_VERSION`); `users.token_generation`; `last_login` | Migration is idempotent; fresh DB and already-v3 DB both reach current; column survives a second run | — (nothing pins "no migrations exist") |
| **2. Sessions** | `auth`: session read/write/refresh in Valkey, boot-id keying, fail-closed on connection error; `control-plane.nix` gains `python3.pkgs.redis`; CP config for the Valkey URL | Sliding TTL extended on verify; absolute ceiling still enforced; revocation via counter takes effect immediately; Valkey-unreachable ⇒ **503**, not 200; boot-id change invalidates an existing session | Assert `SESSION_INACTIVITY_SECONDS` dead-code claim is gone rather than re-pinning the constant |
| **3. Contract** | `_require_admin` → JSON 403 with `detail`; one `require_admin`-shaped error everywhere; `rd` encode/sanitize fix (reject `\`, `//`, schemes); `/login` honors `rd` after `fetch()` (respond 200 with a JSON target so the caller can navigate `_top`) | 401 vs 403 distinguished end-to-end over real HTTP; `rd` with an embedded `?` and with `/\\evil.com`; logout clears the cookie and drops **that** session (see the deviation note below — it does *not* bump the counter) | **Delete** the `assertIn("window.location.replace('/login?rd='", html)` string pin in `test_control_plane.py` (currently ~L1730, in the login-page HTML test), replaced by behavioral HTTP tests |
| **4. Client** | One `portalFetch()` in `stub.py` used by every portal call (put the JS in a plain-string block à la `OWUI_CONFIG_JS`; `page_shell` is one giant f-string and JS braces are already doubled); frame-aware `/login`; `#nav-logout` gains frame teardown (`retireChatFrame()` exists and is currently only called on OWUI reset) | Shell does not keep polling a dead session; re-login does not nest a second shell | Any test asserting the old `refreshStatus` status-handling shape |
| **5. Status split + error pages** | `status_public()` subset vs gated full; `gateway.nix` nested `error_page 500 502 503 504 = @cp_down` inside `@cp_interstitial` serving a **static** page | Anonymous `/api/status` returns the subset and **no** admin counts; full status requires a session; CP-down probe asserts a Lemonseed page (not nginx's) with the appliance's own markup | Update the `/api/status` payload assertions to the split |

### What changed while building

**The gated half of the status surface is a new path, `/api/system`.** The table said
"subset vs gated full" without naming a path, and one URL cannot be both open and
gated. `/api/status` keeps the open name and the open subset, so an **installer USB
already in the field** still recognizes the appliance by `role` and still finds the
portal by `portal_url` after reboot — the alternative (keeping `/api/status` full and
gating it) breaks every installer built before this release. `status_public()` lists
its keys instead of subtracting them from the full snapshot, so a field added to the
full snapshot later cannot leak by default.

**`portalFetch()` retries GET only.** The draft said "retry 5xx with backoff". A reset
or a reconcile whose response was lost in transit would otherwise run a second time,
and neither is safe to repeat.

**Three portal calls stay outside `portalFetch`, deliberately,** and each reason is
written in the JS beside it: the setup page's `/api/boot` poll and the login form's
own `POST /login` (both pre-auth — routing a failed sign-in through `portalFetch`
would reload the login page forever, since a bad password *is* a 401), and the
`/_app/version.json` reachability probe (retrying and auth-navigating would hide the
exact failure it exists to detect). `PortalCallSiteTests` keeps that set closed.

**The contract is tested by executing it, not by matching text.**
`control-plane/test_portal_fetch.py` loads `stub.PORTAL_FETCH_JS` into a V8 context
with `fetch`/`window` stubbed and asserts whether the code navigated the top frame,
how many times it retried, and what it returned. A string assertion over rendered HTML
passes happily while the neighboring branch is broken, which is the failure mode this
table kept producing.

**The shared stylesheet is scoped where it is shared.** `/_lemonseed/lemonseed.css`
carries the portal theme *and* the not-ready page's rules, the latter namespaced under
`body.cp-down`: unscoped, the error page's bare `p` and `button` rules would restyle
every portal page that now reads the same file.

**Ordinary logout drops one session; it does not bump the generation counter.** The
table above originally said logout does both. Bumping the counter on logout would
sign the user out of every other device because they clicked *Sign out* on one, which
is not what a logout button means. `auth.revoke_sessions()` (the counter bump) is
therefore reserved for the events that genuinely mean "this identity is no longer
trusted": disable, demote, delete, and an explicit *sign out everywhere*. Logout calls
`auth.drop_session()`, which is deliberately best-effort — if Valkey is unreachable,
logout still returns success and clears the cookie, because the cookie is the part the
user can act on and the session record expires on its own TTL regardless.

**Steps 1 and 2 share one QEMU run** rather than one run each. Step 1's QEMU signal is
strictly weaker than step 2's: the migration runner is real-Postgres exercise, but no
session path is reachable from the smoke test until step 2 lands, so running QEMU twice
would buy nothing but an hour of TCG. The shared run must assert `/api/data` provisioning
reports `schema_version: 4` (the migration actually executed against real Postgres, not
just against the fake cursor in `test_db.py`).

### Gateway error pages, concretely

`@cp_interstitial` proxies to the CP, so it cannot be its own fallback. Inside it,
nest `error_page 500 502 503 504 = @cp_down;` where `@cp_down` serves a static file
from the store (`alias`), the way brand assets already do
(`brandAssets = ../../control-plane/static/brand`).

**500 is not a throwaway in that list.** nginx's `auth_request` maps a subrequest
status other than 2xx/401/403 onto **500** for the main request. So when Valkey is
down and `/api/session/verify` answers 503, the gated location does not see 503 — it
sees 500. Omit 500 and a Valkey outage renders nginx's default error page at exactly
the moment we are fail-closed for every user, which is the worst possible time to
lose the branded page.

That forces one real decision: the stylesheet currently exists **only** as
`stub.STYLE` inside the CP, and `control-plane/static/` contains only `brand/`.

**Decided: extract `control-plane/static/lemonseed.css`**, have the CP serve the
same file it links, and `alias` it from nginx the way brand assets already come
out of the store (`brandAssets = ../../control-plane/static/brand`). Duplicating
the design in two languages is how a branded page ends up looking unbranded — and
the forward-looking reason is stronger than the immediate one: when we add user
themes (dark/light), there must be **one** stylesheet root for the whole system,
or the gateway error pages will be the one part of the appliance that ignores the
user's choice. This is the first step toward that root, so the CP's inline
`stub.STYLE` should be deleted rather than left alongside the file.

It also means adding a tracked file, so `git add` it before any `make build-*`.

## Test matrix

**Unit (`make test`).** HTTP-level tests are already the house pattern — this file
boots `ThreadingHTTPServer((stub.HOST, stub.PORT), stub.Handler)` and drives it
with `urlopen`, so the contract is testable end-to-end without mocks:

- 401 with **no** cookie; 401 with a **malformed** cookie; 401 after the counter
  bumps; 403 for authenticated non-admin; 403-managed distinguishable by body.
- Sliding-window arithmetic and the absolute ceiling as pure functions (no clock
  mocking in the HTTP path).
- Valkey client **mocked** — `make test` must keep passing with no Valkey, no
  Docker, no Postgres (AGENTS.md).
- Migration runner: v2→v3→v4 shape, and running the same migration twice.
- `rd` sanitizer table (each rejected shape as a case).
- `/api/status` public subset contains no key starting with a gated prefix.

**QEMU (`make test-qemu`).** The load-bearing assertions:

- `/healthz`, `/api/status` (public subset) answer **before** login during boot.
- Unauthenticated `/` redirects to `/login?rd=%2F`; the login page then reaches
  `/` after a real login.
- A gated path with a **stale/absent** cookie yields a 401 or a redirect to
  `/login` — the exact regression that motivated this milestone phase.
- **CP-down path:** stop the CP service, request `/`, assert the body is the
  static Lemonseed page and not an nginx stock page. The most direct proof of the
  reported startup symptom, and possible only in QEMU.
- Valkey down ⇒ gated request gets 503 (fail closed), and the public subset of
  `/api/status` still answers.
- Existing boot-order assertions must stay green; adding a Valkey dependency to
  the gate is the one change that could disturb them.

**Bare metal (flag for the user).** Anything the QEMU harness can't see: whether
the client contract actually holds in a real browser — an expired-session tab, a
second tab noticing a logout, and the settings panel rendering once the theme left
Python. QEMU can prove the server answers `401` with renderable JSON; only a browser
can prove a tab reacts to it by showing the login page *in the top frame*.

## Deliberately not here

- **Users & groups CRUD** — M7. M6.6 only *removes the obstacles* (the migration
  runner and the revocation path are prerequisites for disabling a user at all).
- **Guard ordering.** Guards run in the nginx rewrite phase and beat
  `auth_request`, so an anonymous-or-expired caller on a *guarded* path still
  sees 403-managed before the gate can offer a login redirect. Fixing that means
  either a per-guard cookie predicate or moving enforcement to the access phase —
  real changes to a shipped mechanism. **Decided: do not do this now.** The extra
  complexity is not justified by a symptom that only shows on admin write paths,
  so it stays in §11 to be decided with the guard-surface expansion work. The D3
  frame-aware login plus the D4 status split are accepted as sufficient for
  M6.6's reported symptoms, with the known residue that a guarded write while
  signed out reads as "managed by Lemonseed" rather than "sign in".
- **Session revocation on delete** — needs the user-delete path, which does not
  exist yet (`owui_client.user_delete` has zero call sites).

## Gate

Per the standing rule, no step advances until **both** are green:

1. `make test`
2. `make test-qemu` (`QEMU_API_TIMEOUT=1800` without `/dev/kvm`)

Step 1 (schema migrations) and step 2 (Valkey sessions) change boot and CP HTTP
behavior, so the QEMU gate is mandatory, not optional. Steps 1 and 3–5 also change
what an upgrade does to an existing box, which is the bare-metal checkpoint.

### How the runs were actually grouped

Steps 1 and 2 shared one QEMU run, and steps 3–5 shared another. The rule used is the
one stated for step 1: **a step earns its own run only if it carries QEMU signal that
the neighbor's run would not also produce.** Steps 3 and 4 are CP-internal and portal
JS, but step 3 changed the gateway's answer to an expired XHR, which is only
observable through real nginx on a booted appliance, so 3 and 4 rode along with 5
rather than covering less than a shared run would.

`make test` and `make lint` ran after every step, not once at the end.

### Gate record

| Check | Result |
|-------|--------|
| `make test` | 632 tests OK, up from 584 when this work started. 50 new tests, 2 deleted. New: the executed `portalFetch` contract (17), `rd` sanitization (10), the status split (7), the XHR sign-in and frame-breakout contract (5), the theme surface (7), the auth-gate mirror (2), the open/gated API surface (2). Removed: the `window.location.replace('/login?rd='` string pin, `test_api_status_role` (superseded by the exact-subset test, and its `boot_snapshot` mock no longer had a caller), and `test_api_status_reports_the_session_store`, which became `test_api_system_reports_the_session_store` when the answer moved behind the gate. The settings panel's `/api/status` diagnostics link was retargeted to `/api/system` rather than dropped, and now asserts the open subset is *not* linked. |
| `make lint` | clean |
| `make test-gateway` | passes against live nginx, now including the XHR-401 branch, a negative control on the header match, and the shared theme |
| `make test-qemu` | passed for steps 1–2 and for 3–5. The 3–5 run asserts, against the booted appliance: unauthenticated `/api/status` carries the public subset and **no** gated keys; auth state is read from `/api/system` with a session; an unauthenticated **XHR** on a gated path gets a real `401` with renderable JSON through real nginx (the motivating regression); the shared theme is readable with no session |
| Bare metal | three checks, on the upgraded box. **(1) Two tabs, logout in one:** the other flipped to the login page within a few seconds — this is the load-bearing result, because it is an active tab polling `/api/system`, taking a real `401`, and navigating `window.top`; nothing in the HTTP tests can cover that last hop. **(2) Settings page** renders as expected after the theme moved from Python strings to `static/lemonseed.css`. **(3) A tab left open overnight** with an expired session stayed put on the Open WebUI prompt until reloaded, then landed on the login page with no frame nesting of the login page or an error page. (3) is expected: a hidden tab left running overnight is throttled or frozen by the browser, and no page script runs while frozen. The two-tab case is the control that proves the mechanism, since it exercises the same poll-and-navigate path on a tab the browser was still servicing. Reloading a frozen tab revalidates immediately, which is the behavior the user sees. |

One caveat on the 3–5 run: `portalGotoLogin()` was tightened after that image was
built (an expired session from inside the settings iframe now sends `rd` to the
shell's `/settings/` rather than to the bare `/settings/content/` fragment). It is
browser-only JavaScript with no HTTP surface, so nothing in the smoke test can reach
it; the executed JS suite covers it. The image the user ran bare metal on was built
after that tightening, so the shipped JS was what the unit suite pins. The specific
case — session expiring *while the settings iframe is the visible view* — was not
exercised; the overnight check was the shell's default chat view.
