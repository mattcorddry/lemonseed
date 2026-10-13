# M7 — Household identity, power controls, and the friendly-model layer

Draft for approval. `DESIGN.md` is the as-built product contract; this document is
the **sprint pull** from its §11 backlog, per the project rule that a milestone plan
is the only place upcoming work is tracked.

Everything below uses `file:symbol` rather than line numbers, because line numbers
rot. Every claim in the *Problem* sections was re-read against the working tree while
writing this.

## Scope and boundary

M6 closes with the session/error contract (**M6.6**) plus the hygiene and
build-cycle-time work. Everything in this plan is **M7**. Three items moved out of M6
by decision: **users & groups management** (formerly M6.2b), **LiteLLM friendly model
names**, and **portal power controls**.

| Phase | Title | Design gate | Bare metal |
|-------|-------|-------------|------------|
| ~~P1~~ | Session and error contract → **now M6.6**, see below | decided | BM-1 |
| P2 | Users & groups **design** | **yes — approval required before P3** | no |
| P3 | User management + **My Profile** | no | BM-2 |
| P4 | Groups, attributes, OWUI namespace | no | BM-2 |
| P5 | Power, update, and the SYSTEM panel | no | BM-3 |
| Later in M7 | LiteLLM, modules PoC, security | see "Beyond P2–P5" | — |

**Boundary, settled.** P1 became **M6.6** and stays in M6; P5 (power / update / logout
controls) moves to M7. The phase content below is unchanged by that split.

**Phase gate (unchanged from our working pattern).** A phase ends when `make test`
**and** `QEMU_API_TIMEOUT=… make test-qemu` are both green, its new tests exist, and
its obsolete tests are deleted. No phase starts before the previous one passes.
Bare-metal checkpoints are yours; I flag them explicitly.

---

## ~~P1~~ → **M6.6**, executed as M6 close-out

Session and error contract **stays in M6** (your call: it fits M6's natural scope).
It is not an M7 phase; the design record, decisions, work breakdown and test matrix
live in
[`proposals/M6/SESSION-AND-ERROR-CONTRACT-M66.md`](../M6/SESSION-AND-ERROR-CONTRACT-M66.md).
Everything P2–P5 below assume it has landed.

Decisions you made, restated so M7 can depend on them:

| | Decision |
|---|---|
| Rule | Same-origin portal XHRs get JSON status codes and never a redirect; HTML navigations get the login redirect. **401** → `window.top.location = '/login?rd=…'`; **403** → swallowed, or surfaced as "signed in, but not permitted"; **5xx** → retry with backoff. |
| D1 revocation | Per-user **generation counter in Postgres**, checked on every verify. **No 30 s cache** unless it turns out to be trivial — we are not building for scale, and Postgres plus block IO already cache this read. |
| D2 expiry | **Sliding window**, held in the **already-running Valkey** (the way Authelia does it). State need not be persisted — logging everyone out on reboot is fine, which is precisely what makes it cheap. |
| D3 login page | **Frame-aware login page is acceptable.** The only future traffic that bypasses our auth is a bearer-token OpenAI-compatible endpoint (LiteLLM), and that can run on an alternate port. |
| D4 `/api/status` | **Split it.** There is enough in it that should not be exposed to an anonymous LAN client (e.g. admin user count); unauthenticated clients like the installer get a subset endpoint. |
| New scope | nginx's stock error pages get replaced with **Lemonseed-styled, user-friendly** ones. Partially done: M6.6 step 5 made every 5xx from a dead CP render a branded static page at 503 that reloads itself (`proposals/M6/GATEWAY-ERROR-AND-PULL-CONTRACT-M66.md`). **Still open here:** styled text for the statuses the gateway still answers itself. The shared stylesheet root (`/_lemonseed/static/lemonseed.css`) was **moved out of this plan into M6.6 row S5.8** on 2026-10-12 — the milestone that owns the error pages owns the sheet they link. |

Two consequences worth knowing before reviewing M6.6's diff, both from measuring the
Valkey option rather than assuming it:

1. Sessions in Valkey make **login depend on a container the CP itself starts**
   (`nixos/modules/data-services.nix` owns `valkeyStart`). The gate must therefore
   **fail closed** when Valkey is down, and QEMU must keep asserting boot order.
2. `valkey-server --save 60 1` **does** snapshot, so an RDB would survive a reboot and
   "reboot logs everyone out" would quietly be false. Session keys get namespaced by
   `/proc/sys/kernel/random/boot_id` rather than disabling persistence on a container
   the data plane also uses.

It also carries one prerequisite that M7 depends on: **a real schema migration
runner**. `db.SCHEMA_VERSION` is currently decorative (every statement is `CREATE
TABLE IF NOT EXISTS`, no `ALTER TABLE` anywhere in the tree), so `token_generation` —
and the group-attribute columns in P4 — would otherwise work in CI and silently do
nothing on a deployed box.

## P2 — Users & groups: design document only

**This phase produces a document and an approval gate. No product code.** It exists
because the decisions here are cheap to make on paper and expensive to reverse in
schema.

### Problem

We have a users *table* and no users *feature*.

- `db.py` creates `users(username, display_name, email, password_hash, disabled,
  created_at)`, `groups(name, created_at)` and `user_groups(username, name)` with
  `ON DELETE CASCADE`. `groups` has **no attribute columns at all**, so there is no
  place to put "this group enables content filtering".
- There is **no migration mechanism**: `db.SCHEMA_VERSION = 3` is written and reported
  but never compared, and every statement is `CREATE TABLE IF NOT EXISTS` with **zero
  `ALTER TABLE`** in the repo. A deployed box keeps whatever tables its first boot
  created — so adding `token_generation` (P1) or group attributes (P4) to the `CREATE`
  block silently does nothing on existing appliances. **This must be solved as part of
  P2's design, not discovered in P3.**
- `users.disabled` **is** already honored: `auth.get_user()` treats a disabled row as
  no-match at sign-in, and the group-membership queries join `AND u.disabled = FALSE`.
  What does not exist is any way to *set* it — no CRUD anywhere.
- The only user creation is `auth.create_admin`, reached from the ungated `POST /setup`.
- Roles are **derived, never stored** (`auth.role()`, admin ⟺ membership in
  `auth.ADMIN_GROUP`), which is the right model and should stay.
- `nginx`'s `auth.ADMIN_SNIPPET` is `""` — the "admin gate" at the edge is a no-op, and
  admin-ness is actually enforced inside the CP from `X-Forwarded-Groups`. Fine, but it
  should be documented as intentional rather than looked at sideways.
- On the OWUI side we already have the client verbs — `owui_client.user_create`,
  `user_update`, `user_delete` — and **`user_delete` has zero call sites**. The policy
  manifest declares `entities.users.scope: ["membership", "is_admin"]`, but
  `UsersHandler` diffs only `is_admin`; `membership` is validated and inert.
- Four places pin the current split as an intentional tripwire, all mentioning the
  retired "M6.2b" label: `owui-policy.json`'s two advisory-guard `reason` strings,
  `test_owui_policy.py` (`assertIn("M6.2b", spec.reason…)`), and
  `scripts/qemu-api-smoke-test.sh` (two assertions that the account guards stay
  advisory). P4's job includes flipping those guards, which will fail all of them
  on purpose.
- Open blocker already in the backlog: OWUI's trusted-header sign-in calls
  `Groups.sync_groups_by_group_names`, which **deletes** memberships absent from
  `X-Forwarded-Groups`. Until we decide who owns OWUI groups, anything an admin creates
  in OWUI's own UI loses our users at their next sign-in.

### Deliverable: `proposals/M7/USERS-AND-GROUPS.md`

Answering, in writing, with the trade-offs and the decision recorded:

1. **Schema + migration.** A real `SCHEMA_VERSION`-gated migration runner in `db.py`
   (ordered `ALTER`s, transactional, fail boot loudly rather than limp along), plus the
   column set: `users.token_generation` (P1), `users.disabled` (exists), group
   attributes as either typed columns on `groups` or a `group_attributes(group, key,
   value)` table. My recommendation: a key/value table now — attributes are the thing we
   expect to grow, and a JSONB column invites untyped drift.
2. **Role model.** Admin / User / Disabled as (group membership) + (the `disabled`
   flag), not a third group. What "Disabled" can and cannot do. Who may demote or
   disable, and the **last-admin guardrail** (today nothing prevents disabling the only
   admin — see DESIGN.md §11).
3. **Password lifecycle.** Reset by an admin without knowing the old password, and
   whether the user must change it on next sign-in. bcrypt cost already in `auth.py`.
4. **Reserved identities.** `admin` and the service account (`Lemonseed Control Plane`,
   `lemonseed-cp@…`) are un-deletable and un-editable by the portal UI. Which fields of
   each are read-only, and how that shows up in the UI.
5. **The delete flow.** Immediate OWUI delete on delete (your call); the tombstone /
   7-day-undelete idea stays in the backlog with the jurisdictional note attached.
6. **OWUI group ownership — the blocker.** Two options on the table: stop sending
   `X-Forwarded-Groups` (OWUI owns its groups, our `groups` table is portal-only), or
   declare a closed, CP-provisioned namespace so the upstream sync is always additive.
   Recommendation: closed namespace, because we want group attributes to drive model
   selection, which needs the mapping to be authoritative.
7. **Attribute semantics.** Booleans combine with logical **OR** across a user's groups
   (already decided); lists append *if* lists ever appear; how the content-filter
   attribute reaches the model actually selected (this is the LiteLLM coupling — see
   "Beyond P1–P5").
8. **Screen name vs display name vs email.** Today `auth.create_admin` writes the
   *username* into `display_name`. Decide the real field set and the synthetic-email
   policy (`@localhost` is an internal join key only; the real-email + SMTP idea stays
   in the backlog).
9. **Explicit non-goals for M7**: per-user quotas, per-user model allowlists beyond the
   content-filter boolean, directory auth (LDAP/OAuth/PAM), TOTP.

**Gate: you approve this document, then P3 starts.** Anything left undecided here
becomes a P3 blocker rather than an improvised choice mid-implementation.

---

## P3 — User management

### Problem

Nothing exists: no `/api/auth`-style routes, no portal Users panel, no CRUD, and
`owui_client.user_delete` is unused. `scripts/qemu-api-smoke-test.sh` asserts
`users == 1 and admins == 1` from `/api/status`'s auth block, which is the exact
assertion that must change when a second user can exist.

### Proposal

- `control-plane/users.py`: the CRUD surface (`create` / `rename` / `set_password` /
  `enable` / `disable` / `delete`), each mutating one transaction and each bumping
  `token_generation` where relevant. `auth.py` keeps tokens/roles and stops owning
  writes.
- Admin-gated CP routes under `/_lemonseed/api/users*`, following the pattern already
  used by the OWUI reset: `authGate` + `adminGate` + `cpProxy` +
  `error_page 500 502 503 504 =503 /_lemonseed/cp-down`, with `self._require_admin()` in the
  handler, non-blocking single-flight lock → `409 {"accepted": false, "reason": …}`.
- Portal **Users** panel (admin only): list, add, rename, reset password,
  enable/disable, delete with a confirmation. Per the rule we settled for data-safe
  actions, this does **not** get a typed interstitial — deleting a user *is*
  data-destroying, so it keeps a typed confirm; disabling does not.
- **A separate `/profile` "My Profile" page, available to every signed-in user.** Most
  web products split "my account" from "the system control panel", and doing so here is
  what lets non-admins stay out of Settings entirely. Self-service, own-row-only:
  **full name**, **password change** (current password required), **email address**,
  and later TOTP. Read-only on the same page: **role**, and **last login time**,
  which needs a new `users.last_login` column (M6.6's migration runner makes that
  cheap) written on successful login.
- **Logout lives here** (or as a dropdown on a generic person icon in the nav), not in
  the control panel — every user needs sign-out, only admins need Settings.
- OWUI reconciliation for identities: **enforce the user list and `is_admin` only**.
  Create-if-missing, rename/email/admin updates, and delete only when the *portal*
  deleted the account — never delete users as routine reconciliation (your
  constraint), and never touch other OWUI user settings so users keep their own API
  keys and the per-user OpenAI endpoint story.
- Flip nothing in the guard manifest yet; P3 must remain usable while P4 is in flight.

### Decisions I need

Both previously-open questions are answered; recorded so the diff has somewhere to
point.

- **Non-admins get My Profile, not Settings.** Chat plus their own profile. The nav
  shows the control panel only to admins, so `adminGate` stays on `/settings/*` and
  every admin API, and `/profile` gets `authGate` with own-row authorization checks in
  the handler.
- **Self-service password change is in M7**, on My Profile, with the current password
  required. Admin-resets-another-user's-password is the admin path in the Users panel.

### Tests

- **Add:** `test_users.py` for the CRUD layer with mocked `db`; OWUI sync unit tests
  with `owui_client` mocked (including "reconciliation never deletes an unknown OWUI
  user", and "portal delete does delete in OWUI"); HTTP-level tests for every new route
  (401 unauthenticated, 403 non-admin, 409 single-flight); migration tests proving an
  old-schema DB is upgraded rather than skipped.
- **Add (My Profile):** a non-admin can read and change **their own** name / password /
  email and gets **403** on another user's row; changing your own password bumps your
  own `token_generation` (so other tabs/devices are revoked) and does **not** bump
  anyone else's; `/profile` is reachable by non-admins while `/settings/` is not;
  `last_login` advances on a successful login and is exposed only to the owner.
- **Change:** the QEMU `users == 1 and admins == 1` assertion becomes an explicit
  scenario — create a second non-admin, assert the list and admin count, assert the new
  user can sign in, **can reach `/profile`, cannot reach an admin route**, then delete
  and assert OWUI has no leftover.
- **Delete:** any test asserting `create_admin` is the sole writer to `users`.

### Gate

`make test` + QEMU. **BM-2 (with P4):** create/rename/disable/delete a real second
account on the NVIDIA box, sign in as it, confirm OWUI sees the right identity and
admin-ness both ways.

---

## P4 — Groups, attributes, and the OWUI namespace

### Problem

`groups(name, created_at)` cannot hold an attribute; `UsersHandler` ignores
`membership`; the six `groups.*` entries in `owui-policy.json`'s `api` map are declared
and unused (backlog says: use them or delete them, so the manifest keeps meaning "what
we do"); and the OWUI group-sync semantics are undecided (P2 §6).

### Proposal

- Portal **Groups** panel: create/rename/delete groups, add/remove members, set boolean
  attributes. Default groups: `admin`, `users`, plus an example `Family`/`Kids` pair
  shipped as *examples in the docs, not seeded rows*.
- Effective-attribute resolution: `effective_attributes(username) = OR over groups`,
  exposed on the session identity so downstream consumers (model selection first) read
  one place.
- OWUI side: implement `membership` in `UsersHandler` under the namespace decided in
  P2, using the `groups.*` api-map entries — **or** delete those six entries if P2
  decides OWUI owns groups. Either way the manifest stops lying.
- Flip the two account guards from `advertise` to `enforce` (`POST /api/v1/auths/add`,
  `DELETE /api/v1/users/.*`) now that a safe replacement exists, and update the
  manifest `reason` strings to drop the "M6.2b" label.
- Content-filter attribute is *stored, resolved, and surfaced* here; it **drives model
  selection only once LiteLLM lands**. State that limit plainly in the UI so nobody
  believes the toggle filters anything yet.

### Tests

- **Add:** attribute-resolution tests (multi-group OR, empty groups, unknown group);
  namespace-sweep tests (our groups survive an upstream sync; a foreign group inside the
  reserved namespace is claimed); guard-enforcement tests for the two flipped guards,
  including that the CP's own service-account path still works.
- **Delete:** the advisory-split tripwires — `assertIn("M6.2b", spec.reason…)` in
  `test_owui_policy.py`, the `advisory`-set assertion in
  `scripts/qemu-api-smoke-test.sh`, and their manifest strings. These are *expected* to
  fail the moment the manifest flips; converting them is part of this phase, not a
  regression to fix.
- **Add QEMU:** group CRUD through the portal API, a user in two groups, and an
  assertion that the account guards now answer 403-JSON for OWUI-UI writes while the
  CP's own path still succeeds.

---

## P5 — Power, update, and the SYSTEM panel

### Problem

- The local console (`nixos/modules/console.nix`) is a tty4 status splash plus a tty2
  `agetty --autologin root` shell. There is **no menu** — everything today is typing on
  a console you have to find, which is clumsy for the three things a home user
  actually wants: reboot, shut down, update.
- Updates are CLI-only: `scripts/lemonseed_update.py status|apply --url|--dir
  --reboot --force|cancel` writes the inactive slot and arms `boot_once`, and refuses
  when an attempt is already armed. **Nothing in the tree triggers an update from the
  running system** — no HTTP route, no unit or timer, no CP import, no nginx location.
- The CP cannot even exec the updater: `nixos/modules/control-plane.nix`'s
  `runtimeInputs` has `postgresql, systemd, coreutils, docker, nginx, avahi, iproute2,
  util-linux, pciutils, lemonadePackage` but **not** `lemonseed-update` (defined in
  `nixos/modules/update.nix`), and `LEMONSEED_GRUB_CFG` is exported while
  `grub-editenv` is not packaged for the CP.
- Slot state lives in the GRUB env (`boot_default` / `boot_once` / `boot_attempted`,
  read by `scripts/lemonseed-ab.sh`), and **only GRUB writes `boot_attempted`**. So a
  user-initiated reboot while an update is armed silently completes the upgrade —
  the button you pressed says "reboot". §12 Q4, now answered by design (below).
- Rebooting mid-work is unguarded: `chat_ready._prepare_lock` / `_pull_lock` /
  `_pull_process`, `modules._lock`, `_reconcile_request_lock`, `_owui_reset_lock`, and
  `owui_policy._reconcile_lock` all exist and none of them are consulted by a shutdown
  path.
- The existing "box is going away" machinery is reusable and should be reused, not
  rebuilt: `chat_ready.REBOOT_PENDING_FILE` + `chat_snapshot`'s `state="rebooting"` +
  the branded `/_lemonseed/cp-down` page in nginx + the shell's `landingCopy()` reboot branch.
- Logout (`#nav-logout`) has no confirmation, no `rd`, and **does not tear down the
  OWUI iframe** — `retireChatFrame()` is only called on an OWUI settings reset, so a
  stale chat frame survives sign-out.
- Settings today is **two URLs** (`/settings/` shell + `/settings/content/` inner
  iframe) with panels: Lemonade AI Engine, Large Language Model, **Status**, Chat, API.

**Resolved before this plan was written, so P5 does not have to re-litigate it:**

- **Graceful shutdown already works.** You verified `shutdown -h`, `shutdown -r`, and
  ACPI power-button graceful shutdown on the current image. So "what must stop, in
  what order" (§12 Q5) is no longer a design question — the sequencing risk that
  dominated the earlier draft is off the table, and P5 is about *reaching* systemd
  actions safely, not inventing a shutdown protocol.
- **The `boot_once` collision has a product answer**, not a technical one: the UI
  never offers a bare "reboot" when an update is armed. See the staged-update flow
  below.

### Proposal

**One SYSTEM panel, formed by merging the existing STATUS panel into it.** They are
both "about the box", and merging is what lets the current version sit next to the
latest available — which is the whole point of an update affordance. Everything you
already reviewed and liked (Lemonade AI Engine, Large Language Model, Chat, API) stays
exactly where it is.

The SYSTEM panel shows, in order:

1. **Version** — running version, and when known, the latest available.
2. **Update controls** — *Check now*, and *Download and install* armed only when a
   newer version exists. Never automatic (below).
3. **Reboot** and **Shut down** — admin-only.
4. Existing status content: setup state, timezone, GPU, health.

**Confirms are sized to the damage.** Reboot, shutdown and update are data-safe, so
they get an explainer plus a plain confirm — **no typed interstitial**, which is
reserved for things that destroy data (the settings reset, user delete). The wording
you gave is the spec:

> **LEMONSEED SHUTDOWN** — This will power off the lemonseed appliance, disconnecting
> any active users. Would you like to proceed?

Same shape for reboot. Accepting hands off to the existing going-away path
(`REBOOT_PENDING_FILE` → `landingCopy()` → the branded 503 page).

**A reboot while an update is staged is a choice, not a surprise.** If
`boot_once`/`boot_default` says an update is armed, the reboot dialog offers two
actions instead of one — **"Reboot into the new version *X*"** (default, highlighted)
and **"Reboot without updating the system"** — which answers §12 Q4 by construction:
the user picks whether their reboot consumes the armed slot, and a reboot is never
secretly an upgrade.

**systemd owns the timing.** The CP asks for `systemctl reboot` / `systemctl poweroff`
and gets out of the way; it does not wrap systemd in a second timer/watchdog script.
Timeouts and the kill-hierarchy are systemd's job; a CP-side countdown would be a
second, worse implementation of the same thing.

**Interlock before acting.** Acquire-or-refuse the locks listed above and check
`_pull_process`; on refusal return `409` naming the activity ("a model download is in
progress"). The UI can then say *why* and offer a forced path only where losing work is
safe.

**Update checks: one workflow, two triggers.**

- **(a) Weekly automatic**, with a large fuzz so a thousand appliances do not
  synchronously hammer the release endpoint:
  `sleep $(( $RANDOM * 86400 / 32768 )); check-for-update` under a weekly timer.
- **(b) "Check now"** in the SYSTEM panel.
- Both invoke **the same** script/workflow, so there is one definition of "an update
  exists".
- When it finds one: **(1)** a **red dot on the settings icon for admins**, and
  **(2)** the *Download and install* button arms.
- **Never auto-download and never auto-install.** Flag only. A silent download can
  compete with a model pull for the disk guard and the WAN link, and a silent install
  removes the user's choice about when the box restarts.

**Logout** lives in **My Profile** (P3), or as a dropdown on a generic person icon in
the nav bar — every user needs it, while reboot/shutdown/update are admin-only. P5
supplies the fix (confirm, `rd`, and always `retireChatFrame()`), P3 supplies the
placement.

**Release channel.** The update path moves from `http://test.lemonseed.ai/releases/
stable/` to **GitHub releases**; whatever flow we build must work with GitHub release
downloads. Practical shape: one release with four assets
(`vmlinuz`, `root.squashfs`, `initrd.img`, `manifest.json`), which the existing flat
`IMAGE_BASE_DEFAULT` layout in `scripts/lemonseed_artifacts.py` already matches, and
`scripts/lemonseed_update.py apply --url` already accepts an explicit base URL. The
CP needs `lemonseed-update` (and a grub tool) in `runtimeInputs` before any of this is
reachable from HTTP.

### Decisions (settled)

| | Decision |
|---|---|
| Update check cadence | Weekly timer **with heavy fuzz** + a manual **Check now**, both running the same workflow. |
| Update affordance | Red dot on the settings icon for admins + arm the install button. |
| Auto-install | **Never.** Not on a schedule, not at night, not "just the download". |
| Reboot / shutdown scope | **Admin-only.** Sign-out is for everyone. |
| Confirm shape | Explainer + confirm for data-safe actions; typed interstitial only for data-destroying ones. |
| Staged update + reboot | Two-option dialog, "reboot into version X" as the default. |
| Shutdown timing | systemd's problem, deliberately. |
| Channel | Must be compatible with GitHub release downloads. |

### Tests

- **Add:** interlock unit tests (each lock held → `409`, with the reason surfaced);
  the update-check workflow's decision table (up-to-date / newer available / no
  network / malformed manifest / a GitHub-releases-shaped base URL); "never auto" —
  no code path may reach `apply` without a request; the staged-update dialog logic
  (`boot_once` armed + reboot requested → the choice, never an implicit upgrade);
  logout tests asserting frame teardown and `rd`.
- **Add (QEMU):** `power/update` **dry-run** — the CP computes the command without
  executing it; one real **reboot round-trip** asserting the appliance comes back,
  Postgres is healthy on `/api/data`, and the interstitial rendered while it was down.
- **Delete/replace:** any test asserting logout merely clears the cookie (it now also
  revokes, per M6.6), and any assertion that `/api/status` alone is the readiness
  signal for a reboot — M6.6's status split changes that shape, and the going-away
  signal is `chat_snapshot`, not status.

**BM-3 (yours):** on the NVIDIA box — reboot, shut down, sign out with the chat frame
open, *Check now* against a GitHub release, and the case where an update is armed and
you press **Reboot** (expect the two-option dialog with the new version as default).

---

---

## M6 close-out (executed while this plan was under review)

Results, so this document stays the complete picture.

- **Hygiene — done.** `make lint` exists (ruff from nixpkgs, curated rule set in
  `ruff.toml`, CP packaging untouched) and passes clean over `control-plane/`,
  `scripts/`, `installer/`. It is deliberately **not** wired into `make test`, which
  must stay offline. Real defects fixed along the way, not just style: five
  `Optional[LemonadeClient]` annotations in `model_store.py` named a type that was
  never imported (harmless only because `from __future__ import annotations` defers
  evaluation), `stub.log_message` renamed the `BaseHTTPRequestHandler` parameter
  (`format`), and all ten CP `subprocess.run` calls now state `check=False` because
  each one inspects `returncode` itself.
- **Flake — disproven, and the real defect was different.** 36 perturbed runs of the
  control-plane suite (canonical, shuffled with fixed seeds, reversed,
  `PYTHONHASHSEED` variants, per-module isolation, and a targeted leak probe) were
  all green: the suite is order- and hash-deterministic. What *was* real is the
  cross-test leak noted above — `test_owui_policy.py`'s reconcile `setUp` mutating
  `owui_policy._last_good_manifest` and `_status` without restoring them — now
  snapshotted and restored via `addCleanup`. A symbol named in earlier notes as the
  leak source (`db._connection_factory`) does not exist in the tree, and is retired
  here as a phantom.
- **Build cycle time — root-caused and fixed.** The GPU stack is *not* invalidated by
  the version string; `appliance-artifacts` simply did not pin the system closure,
  and `scripts/package.sh` garbage-collects after every build. Full measurements,
  mechanism and remaining follow-ups:
  [`proposals/M6/BUILD-CYCLE-TIME-P7.md`](../M6/BUILD-CYCLE-TIME-P7.md).
- **`make release`** (sign + publish a GitHub release) is **not** done and is now
  coupled to the P5 decision to move the channel to GitHub releases — do it there,
  once, with the real URL scheme in hand.

---

## Beyond P2–P5

Tracked here so the ordering is explicit, not decided later by accident.

- **LiteLLM friendly model names.** Exposes `Lemonseed AI` and `Lemonseed AI with
  Content Filters` in front of lemond, so model name/path changes stop churning OWUI's
  model config. The manifest side is already prepared and deliberately empty:
  `owui-policy.json` reserves ids `lemonseed-ai` / `lemonseed-ai-filtered` and the
  `^Lemonseed.*$` name space with `"definitions": {}` and the reason *"Definitions
  arrive with Phase 5 (LiteLLM)"* — so today OWUI's model list is whatever the runtime
  produces and Lemonseed owns no model entries yet. (The old env-era
  `DEFAULT_MODEL_METADATA` / `DEFAULT_MODEL_PARAMS` keys were retired in the M6.3b env
  shrink and are asserted absent by `test_modules.py`; they do not come back.)
  **Coupling: P4's content-filter boolean has no effect until this lands.** If you want
  the toggle to mean something in the same sprint as P4, LiteLLM moves ahead of or
  beside P4.
- **Modules: define how they work, then ship one PoC.** The module catalog spec
  (execution params, UX, dependencies) and the CP lifecycle around it; the existing
  Open WebUI module is the reference implementation to generalize from.
- **Security milestone: block-device crypto + TOTP.** LUKS for user-data with a migration
  plan, TPM where available, GRUB password, and closing the empty-root-password tty2
  bring-up hole (DESIGN.md §9) so the encryption story is real. `proposals/AUTH-TOTP.md`
  for TOTP.

## Also true, and worth not losing

**M6.4 AMD support is untested on AMD hardware.** The Vulkan backend is unit-covered,
but `nixos/modules/qemu-test.nix` sets `lemonseed.lemond.llamacppBackend = "cpu"`, so no
automated test has ever loaded a model through Vulkan, and no AMD GPU has been available
for the tier-D check in `proposals/M6/AMD-VULKAN-M64.md`. Anyone reading that file
should treat the AMD path as *implemented, unvalidated*. If an AMD box becomes
available, that tier-D checklist is the whole test plan and needs no new code.
