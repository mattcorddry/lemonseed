# M7 — Household identity and system controls

> **CLOSED — 2026-10-19.** P1–P5 all shipped and bare-metal verified (BM-1 through the
> final update-flow pass on both the Strix Halo and NVIDIA boxes). Gates at close:
> `make test` 952 OK, `make lint` clean, QEMU API smoke PASSED,
> `make build-production` clean. The "Later in M7" items in "Beyond P1–P5"
> (LiteLLM friendly model names; modules definition + one PoC) were **not** built
> and move to future milestones (BACKLOG.md). Supersessions recorded during the
> sprint: the failed-update dialog offers **Retry/Cancel** (D4's "no Cancel in the
> UI" no longer holds), the write phase finishes at 100% with a short reboot grace
> so the panel can show the finish, and long hashes wrap inside the dialog.

Draft for approval, revised 2026-10-13 against your review notes. `DESIGN.md` is the
as-built product contract; this document is the **sprint pull** from its §11 backlog.

Every *Problem* claim was re-read against the working tree. `file:symbol` is used
rather than line numbers, because line numbers rot.

**Phase numbering.** The old P1 (session and error contract) shipped as **M6.6** inside M6,
so it is gone rather than left as a stub. **SRP-6a sign-in is now its own phase (P2)**,
sequenced before the user-management CRUD — the reasoning is in D1. Release work split out
to **M8** (see "Split out to M8").

## Scope and boundary

| Phase | Title | Design gate | Bare metal |
|-------|-------|-------------|------------|
| P1 | Users & groups **design document** | **yes — approval required before P3** | no |
| P2 | SRP-6a sign-in: no password on the wire | no (D1 is decided) | BM-1 |
| P3 | User management + My Profile | no | BM-1 |
| P4 | System groups, custom groups, membership | no | BM-1 |
| P5 | Power controls, SYSTEM panel, update affordances | no | BM-2 |
| Later in M7 | LiteLLM, modules definition + PoC | see "Beyond P1–P5" | — → **moved to future milestones at close** |
| **Split to M8** | Release & distribution: updater refactor, signing, GitHub releases | — | — |

**Why P2 is not waiting on P1.** D1 is decided and its schema is fully specified in the P2
section below, so there is nothing for the design document to settle first. You can review
P1 while P2 is being built. P3 and later are hard-blocked on P1 approval.

**Phase gate (unchanged).** A phase ends when `make test` **and**
`QEMU_API_TIMEOUT=… make test-qemu` are both green, its new tests exist, and its
obsolete tests are deleted. No phase starts before the previous one passes. Bare-metal
checkpoints are yours; I flag them explicitly.

**Permission model, simplified per your note.** `/settings/*` is **admin-only as a
whole**. There are no per-panel or per-control permission decisions inside it, and
`adminGate` stays at the nginx edge plus `self._require_admin()` in each handler.
Non-admins get chat and **My Profile** and nothing else. The one page-level gate and
own-row checks inside `/profile` are the entire authorization surface M7 adds.

---

## P1 — Users & groups: design document only

**This phase produces a document and an approval gate. No product code.** It exists
because the decisions here are cheap on paper and expensive to reverse in schema.

### Problem

We have a users *table* and no users *feature*.

- `db.py` creates `users(username, display_name, email, password_hash, disabled,
  created_at, token_generation, last_login)`, `groups(name, created_at)` and
  `user_groups(username, name)` with `ON DELETE CASCADE`. `groups` has **no attribute
  columns at all**, so there is no place to put "this group enables content filtering".
- **The two-system-group model you described already exists**, which is good news:
  `auth.ADMIN_GROUP = "admin"`, `auth.BASE_GROUP = "users"`, and `auth.create_admin`
  inserts both group rows and joins the first admin to **both**. `auth.role()` returns
  `admin` iff `admin` ∈ groups, else `user` — so "membership in both grants the higher
  authz level" is already the shipped rule. What is missing is that neither group is
  *marked* as a system group, so nothing stops the UI renaming or deleting them.
- `users.disabled` **is** already honored (`auth.get_user` treats a disabled row as
  no-match; membership queries join `AND u.disabled = FALSE`). No way to *set* it
  exists — no CRUD anywhere. The only user creation is `auth.create_admin`, reached
  from the ungated `POST /setup`.
- **Login rate limiting is in-process memory.** `auth._rate_failures` /
  `_rate_lockouts` are plain dicts guarded by `_rate_lock`: they reset whenever the CP
  restarts, and there is exactly one CP process today, so it works — but a restart
  clears a lockout. Worth knowing before we reason about password security.
- `nginx`'s `auth.ADMIN_SNIPPET` is `""` — the edge admin gate is a no-op and admin-ness
  is enforced inside the CP from `X-Forwarded-Groups`. Fine, but document it as
  intentional rather than look at it sideways.
- On the OWUI side we already have `owui_client.user_create`, `user_update`,
  `user_delete` — and **`user_delete` has zero call sites**. The policy manifest
  declares `entities.users.scope: ["membership", "is_admin"]`, but `UsersHandler` diffs
  only `is_admin`; `membership` is validated and inert.
- Four places pin the current split as a tripwire, all naming the retired "M6.2b"
  label: two `owui-policy.json` advisory-guard `reason` strings,
  `test_owui_policy.py` (`assertIn("M6.2b", spec.reason…)`), and two
  `scripts/qemu-api-smoke-test.sh` assertions that the account guards stay advisory.
  P4 flips those guards and will fail all of them on purpose.
- Open blocker: OWUI's trusted-header sign-in calls `Groups.sync_groups_by_group_names`,
  which **deletes** memberships absent from `X-Forwarded-Groups`. Until we decide who
  owns OWUI groups, anything an admin creates in OWUI's own UI loses our users at their
  next sign-in.

### Deliverable: `proposals/M7/USERS-AND-GROUPS.md`

Answering, in writing, with trade-offs and the decision recorded:

1. **Schema + migration.** The migration runner shipped in M6.6 (`db.SCHEMA_VERSION =
   4`, one additive step, `MIGRATIONS` contiguous with the version, and
   `test_db.test_only_additive_migration_sql` machine-bans `DROP`/`RENAME`/`ALTER
   COLUMN`/`TRUNCATE`/`DELETE`/`UPDATE`). P1 designs *on top of* it: a `groups.system
   BOOLEAN NOT NULL DEFAULT FALSE` column, group attributes, and a `users.last_login`
   write path. Recommendation unchanged: attributes as a `group_attributes(group, key,
   value)` key/value table, not a JSONB column — attributes are the thing we expect to
   grow, and JSONB invites untyped drift.
2. **System groups (your model, adopted).** `admin` and `users` are seeded, flagged
   `system = TRUE`, and **cannot be renamed or deleted** by any path — the API refuses
   with `403 {"detail": …}`, the UI does not offer the control, and QEMU asserts both.
   authz level derives from system-group membership **only**; custom groups can carry
   attributes and can never change authz level. That is the right design and it is the
   smallest thing that prevents the lockout you are worried about. Three guards, and
   the third is the one that actually matters:
   - a `system` group cannot be deleted or renamed;
   - the **last admin** cannot be demoted, disabled, or removed from `admin`
     (`auth.admin_exists()` already answers the question and already returns `None` for
     "Postgres not ready", which must be treated as *refuse*);
   - an admin **cannot delete or disable themself** — your note, and it is what stops
     the two-tab race where admin A demotes admin B who is mid-demote.
   Also to decide: whether `users` membership is a stored row (my recommendation —
   group joins are the read path, and effective-attribute resolution wants it
   explicit) with a backfill migration for rows that predate it, or implied for every
   non-disabled account.
3. **Password and authentication.** **Decided (D1): SRP-6a, and it is built in P2, before
   this document's other decisions are implemented.** The design document still *records*
   the auth contract — which rows carry a verifier, what an admin password reset writes,
   how a disabled account interacts with a pending SRP challenge — but it does not get to
   re-open the mechanism. See D1 below the deliverable list.
4. **Reserved identities.** `admin` and the service account (`Lemonseed Control Plane`)
   are un-deletable and un-editable by the portal UI. Which fields of each are
   read-only, and how that shows up in the UI.
5. **The delete flow.** Immediate OWUI delete on delete (your call). The tombstone /
   7-day-undelete idea stays in the backlog with the jurisdictional note attached.
6. **OWUI group ownership — the blocker.** Two options: stop sending
   `X-Forwarded-Groups` (OWUI owns its groups; our `groups` table is portal-only), or
   declare a closed, CP-provisioned namespace so the upstream sync is always additive.
   Recommendation: closed namespace, because group attributes must eventually drive
   model selection, which needs the mapping to be authoritative.
7. **The identity contract for modules (your note, adopted).** We push the **minimum**
   OWUI or another module needs to know who someone is and how much authority they have,
   not our full user model. Today that is exactly the set the gate already forwards —
   `X-Forwarded-User`, `-Email`, `-Name`, `-Groups`, `-Role` — and it should stay that
   narrow. The design doc should state it as a contract with a named minimum set, so a
   future module definition can *declare* which of those headers it needs rather than
   inventing new ones.
8. **Screen name vs display name vs email.** Today `auth.create_admin` writes the
   *username* into `display_name`. Decide the real field set and the synthetic-email
   policy (`@localhost` is an internal join key only; real email + SMTP stays in the
   backlog).
9. **Explicit non-goals for M7**: per-user quotas, per-user model allowlists beyond the
   one content-filter boolean, directory auth (LDAP/OAuth/PAM), TOTP enrollment, profile
   pictures, and any attribute other than the ones P4 actually ships.

### D1 — authentication method. **Decided: no TLS is possible, so remove the password from the wire.**

Your constraints are correct and they close off the option I was steering toward, so
this section is rewritten rather than defended.

**What the status quo actually is** (read from the code, not assumed): **we ship the
plaintext password in the request body.** The login page does
`body: JSON.stringify({username, password})` with `Content-Type: application/json`, and
`do_POST` does `self.rfile.read(length)` and hands the raw bytes to bcrypt. **There is
no challenge/response, no client-side hashing, nothing between the keyboard and the
wire.** Anyone on the Wi-Fi can capture it with tcpdump.

Three things are already right, and worth keeping in perspective:

- **The password is used once and never replayed.** On success the appliance issues an
  opaque session token (`lemonseed_session`, `HttpOnly`, `SameSite=Lax`), so the password
  crosses the wire once per sign-in, not once per request.
- **It is not logged.** `stub.log_message` prints only the request line and status; the
  body is never rendered. `journald` therefore holds no passwords.
- **Guessing is rate-limited** by `auth.rate_limit_check` — though that state lives in a
  process dict, so a CP restart clears a lockout (see below).

What is wrong is exactly what you suspected, and its blast radius is **outside this
product**: a captured password is a permanent credential, and people reuse passwords on
their email and banking. That is the case for fixing it even though an active on-path
attacker can still hijack a session regardless.

**Correcting my earlier objection.** I argued SRP needs a JS build pipeline. That was
true of pulling in an npm library and false in general: SRP-6a needs SHA-256 plus
modular exponentiation, and **Python has native `pow(g, x, N)`** while **browsers have
native `BigInt`**. No dependencies, no bundler. I was wrong, and the change of your
framing from "SRP vs TLS" to "SRP *because* there is no TLS" is the right one.

**The blocker nobody mentions about client crypto on HTTP** — and it will bite any
library you try to adopt, including `thinbus-srp`: **`window.crypto.subtle` exists only
in a secure context.** Over `http://lemonseed2.local/` it is `undefined`, because
SubtleCrypto is `[SecureContext]`. So a browser SRP client must carry its own pure-JS
SHA-256 (~60 lines, verifiable against published test vectors). `BigInt` and
`crypto.getRandomValues` are *not* restricted and are fine. **Worth 10 seconds on the
box to confirm before we commit:** open the portal in Chrome, run `window.crypto.subtle`
in the console, expect `undefined`.

So the mechanism ranking, given HTTP forever:

| Option | Against a passive LAN eavesdropper | Against an active on-path attacker | Cost |
|---|---|---|---|
| **A. Status quo, hardened** | **Fails completely** — hands over the password. | Session hijack, plus the password permanently. | Near zero. Rate-limit state moves to Valkey. |
| **B. Client-side PBKDF2/scrypt before POST** | Defeats offline cracking of a pcap. | Session hijack. | Tempting and wrong: the derived hash *becomes* the password server-side, so anyone who captures it replays it forever. Obfuscation, not protection. |
| **C. SRP-6a (PAKE)** | **Succeeds** — the password never leaves the browser, and the stored verifier is not replayable. | Session hijack only; the password and the verifier both stay safe. | ~200–300 lines total, stdlib Python + vanilla JS, RFC 5054 test vectors. Plus a migration (below). |
| **D. TLS** | Would be the real answer. | Would fix session capture too, which C cannot. | Impossible under your constraints: no FQDN, Chrome rejects self-signed certs and re-blocks them, and importing a private CA is not a home-user task. |

**Recommendation: A + C.** C removes the credential from the wire, which is the exposure
that matters beyond our own product; A is a few hours and strictly improves what C
leaves open. Explicitly accept what C does **not** buy: the session cookie is still a
bearer token on plaintext HTTP, so an eavesdropper who captures it owns that session
until it expires. Mitigate by keeping the idle window short. TLS stays in the backlog
labelled *blocked on FQDN/CA UX*, not as a recommendation.

Scope and safety notes for C, because this is crypto in the login path:

- Use a **fixed published group** (RFC 5054 2048- or 3072-bit safe prime), never a
  generated one, and validate `B % N != 0` and `u != 0` — the known SRP-6a pitfalls,
  including the small-subgroup check that is the whole difference between 6 and 6a.
- Verify against **RFC 5054 test vectors** in `make test`, plus the pure-JS SHA-256
  against known-answer tests, and a full handshake round-trip in-process. This is
  ~150 lines of pure math with published vectors, which is more auditable than it sounds.
- **Migration:** we store bcrypt hashes, and an SRP verifier must be computed from the
  plaintext, so existing accounts adopt on their **next successful sign-in** — the client
  computes and submits a verifier one last time over the old path, and the server marks
  the row as migrated. New accounts and admin resets adopt immediately. The last
  plaintext transmission is the migration itself.
- **Blast radius is CP-only.** OWUI uses trusted-header auth and holds no user passwords,
  so nothing in the container changes.
- This is why SRP is **its own phase (P2), sequenced before** the user-management CRUD:
  setting a password is a seam in both, and the admin-resets-a-password flow should be
  born writing a verifier rather than retrofitted to write one.

**On passwordless TOTP**, since you raised it: I would not rule it out, but it is not a
substitute for fixing the password path, for two reasons. TOTP still needs an
*identifier* — you must say which user you are before a code means anything — and it
demands a recovery path, and printed backup codes are passwords wearing a different
name. It also puts a shared-hardware problem on a household box. Decide it in the
security milestone next to TLS, where `proposals/AUTH-TOTP.md` already lives.

**Gate: you approve this document, then P3 starts.** Anything left undecided here becomes a
P3 blocker rather than an improvised choice mid-build. **P2 (SRP-6a) is not blocked on it**
and can be built while you review P1.

---

## P2 — SRP-6a sign-in: no password on the wire

No design gate: D1 is decided above, and the schema this phase needs is specified here
rather than deferred to P1's document.

### Problem

`POST /_lemonseed/api/login` receives `{"username": …, "password": …}` as plaintext JSON and
bcrypt hashes those bytes directly. Over `http://<box>.local/` the credential is readable by
anyone on the LAN, and a captured password is a permanent credential for whatever else that
person reuses it on. Three properties are already correct and must survive the change: the
password is used **once** (sessions are opaque tokens afterwards), it is **never logged**
(`stub.log_message` renders only the request line and status), and guessing is
**rate-limited** — though `auth._rate_failures` / `_rate_lockouts` are process dicts, so a
CP restart clears a lockout.

The client constraint that rules out off-the-shelf libraries: **`window.crypto.subtle` is
`[SecureContext]` and therefore `undefined` over HTTP**, so a browser SRP client must ship
its own SHA-256.

### Proposal

- **SRP-6a, stdlib Python + vanilla JS.** `pow(g, x, N)`, `hashlib`, `hmac` on the CP; the
  existing login page script on the client. **No new dependency and no change to
  `control-plane.nix`'s `runtimeDependencies`**, and no bundler — `BigInt` is native.
- **A ~60-line pure-JS SHA-256** in the portal's static JS, used only by the SRP client.
- **Fixed RFC 5054 2048-bit group**, never generated, with the two SRP-6a checks enforced:
  reject `B % N == 0` and `u == 0`.
- **Two routes** under the existing auth namespace: `srp/challenge` (username → `salt`, `B`)
  and `srp/verify` (`M1` in, `M2` out). On success the CP issues **the same session cookie it
  issues today**, so `session.py`, `token_generation`, the Valkey session store, the logout
  broadcast and the gate's `X-Forwarded-*` headers are all untouched and cannot regress.
- **Additive migration only**: `db.SCHEMA_VERSION` 4 → 5, adding nullable `users.srp_salt`
  and `users.srp_verifier`. A row with a verifier authenticates by SRP; a row without one
  still authenticates by bcrypt, so nothing bricks on upgrade and
  `test_only_additive_migration_sql` keeps passing.
- **Migration on next successful sign-in.** A bcrypt hash cannot yield a verifier, so a
  legacy account signs in the old way once, the client then submits a verifier which the
  server stores, and the row is marked migrated. New accounts and admin resets write a
  verifier from day one — which is exactly why this phase precedes P3.
- **Rate-limit state moves to Valkey** (TTL keys, reusing M6.6's client) with the current
  dicts as the fallback when Valkey is down, and the failure direction stays **allow** —
  identical to today, where one CP process means Valkey being down means no portal anyway.
  The fix is that a CP restart no longer clears an active lockout.
- **Non-goals:** no TOTP, no password-policy fields, no OWUI-side change (it holds no
  passwords), and no client-side PBKDF2/scrypt pre-hashing, which is obfuscation, not
  protection (see option B in D1).

### Tests

- **Add:** RFC 5054 known-answer vectors for the group parameters and for `M1`/`M2`; a
  known-answer test for the pure-JS SHA-256 (FIPS 180-4 vectors) run alongside the Python
  one so the two implementations are proven equal; a full in-process handshake — happy
  path, wrong password, replayed `M1`, `B % N == 0`, `u == 0`, expired pending challenge;
  an assertion that **no plaintext password appears in any request body or log line**;
  lockout surviving a simulated CP restart with Valkey, and still allowing access when
  Valkey is absent.
- **Dropped before building (P3, D-29/D-31):** the planned legacy path — "an assertion that
  no plaintext password appears … for a **migrated** row", "the migration path (legacy
  bcrypt row → one old-style sign-in → verifier present → SRP-only thereafter)", and the
  note that plaintext-body tests "stay valid for **un-migrated** rows". There is no
  un-migrated row: `verify_credentials` and `POST /login` are **deleted** (the route answers
  405), `users.password_hash` is gone from the schema, and `migrate_cp_schema` raises for any
  database below the v5 baseline. The obsolete assertion is any test claiming bcrypt is the
  terminal storage format, or that a pre-M7 row can be coaxed forward.

### Gate

**Built.** `make test` + QEMU both cover the handshake, and the QEMU auth scenarios now pass
over SRP with `POST /login` answering **405**. **Corrected after the fact:** the scenario this
gate originally demanded — "create a user on a legacy-schema DB, sign in over the old path,
assert the verifier appears, assert the second sign-in never sends a password" — describes a
migrate-on-sign-in path that P3 deleted (D-29/D-31). The scenario that actually matters is the
opposite one: **a database below the v5 baseline must refuse to come up and say
reinstall**, and no code path may accept a plaintext password. **BM-1:** before we build,
confirm `window.crypto.subtle` is `undefined` on the portal (verified — D-9, which is why the
portal ships its own SHA-256); after, sign in twice and capture the traffic to show the
password is absent on the second sign-in.

---

## P3 — User management + My Profile

### Problem

Nothing exists: no user routes, no portal Users panel, no CRUD, and
`owui_client.user_delete` is unused. `scripts/qemu-api-smoke-test.sh` asserts
`users == 1 and admins == 1` from `/api/system`'s auth block — the exact assertion that
must change when a second user can exist.

### Proposal

**Admin side — the Users panel in Settings.**

- `control-plane/users.py`: the CRUD surface (`create` / `rename` / `set_password` /
  `enable` / `disable` / `delete`), each in one transaction, each bumping
  `token_generation` where relevant (which M6.6's session store already honors, so
  revocation is free). `auth.py` keeps tokens and roles and stops owning writes.
- **`set_password` writes an SRP verifier, not a bcrypt hash.** This is the seam P2 exists
  to close before P3 opens; if P2 has not landed, this phase does not start.
- Admin-gated routes under `/_lemonseed/api/users*`, following the pattern the OWUI
  reset already uses: `authGate` + `adminGate` + `cpProxy` +
  `error_page 500 502 503 504 =503 /_lemonseed/cp-down`, `self._require_admin()` in the
  handler, non-blocking single-flight lock → `409 {"accepted": false, "reason": …}`.
- **The three guards from P1's decision are enforced in the write layer, not the UI**,
  so a direct API call cannot bypass them: system groups immutable, last admin
  protected, no self-delete/self-disable.
- Deleting a user *is* data-destroying, so it keeps a typed confirmation. Enabling,
  disabling and renaming do not.

**User side — My Profile, per your UX.**

- A **person icon in the upper corner of the nav bar** for every signed-in user.
  Clicking opens a **popover dropping from the icon** showing their full name and
  username, with **Log out** and **Edit Profile**.
- **Edit Profile** navigates to `/profile`, a page every signed-in user can reach:
  Full Name, email address, and change-password (current password required). Role and
  last-login-time are **read-only** on the same page — `last_login` needs a write on
  successful login, cheap with M6.6's migration runner.
- Named as future fields on this page, **not built in M7**: profile picture, birth date,
  gender identity, TOTP enrollment.
- **Logout lives in the popover.** P3 supplies the fix the power phase's notes previously
  owned: always `retireChatFrame()` before navigating, so a signed-in OWUI frame cannot
  linger after sign-out. `retireChatFrame()` exists and today runs only on an OWUI settings
  reset.

**OWUI reconciliation for identities.** Enforce the **user list and `is_admin` only**:
create-if-missing, rename/email/admin updates, and delete **only** when the portal
deleted the account — never delete users as routine reconciliation (your constraint),
and never touch other OWUI user settings, so users keep their API keys and the
per-user OpenAI-endpoint story. Nothing in the guard manifest flips in P3; it must stay
usable while P4 is in flight.

### Tests

- **Add:** `test_users.py` for the CRUD layer with `db` mocked; OWUI sync tests with
  `owui_client` mocked, including "reconciliation never deletes an unknown OWUI user"
  and "a portal delete *does* delete in OWUI"; HTTP tests for every new route (401
  unauthenticated, 403 non-admin, 409 single-flight); a migration test proving an
  old-schema DB is upgraded rather than skipped.
- **Add (guards):** deleting or renaming a `system` group is refused; the last admin
  cannot be demoted, disabled, or removed from `admin`; an admin cannot delete or
  disable themself; `admin_exists()` returning `None` (Postgres not ready) is treated as
  refuse, not allow.
- **Add (My Profile):** a non-admin can read and change **their own** name / password /
  email and gets **403** on another user's row; changing your own password bumps your
  own `token_generation` and nobody else's and **stores a verifier, not a bcrypt hash**;
  `/profile` is reachable by non-admins while `/settings/` is not; `last_login` advances on
  successful login and is exposed only to its owner.
- **Change:** the QEMU `users == 1 and admins == 1` assertion became a **roster** check
  instead of a bare count: it reads `/_lemonseed/api/users`, subtracts the two smoke
  identities, and asserts the remaining household is exactly `[admin]`, that the admin
  set is exactly `[admin]`, and that `first_admin` agrees. Subtracting by name is the
  point — a run that aborted part-way can leave its smoke account on a persistent
  user-data disk, and a count cannot tell that debris apart from a boot that invented an
  account. The scenario the old assertion stood in for runs near the end of the script:
  create a second non-admin, prove it must rotate before it gets a session, sign in,
  assert it **can reach `/profile` and cannot reach an admin route**, then delete and
  assert Open WebUI has no leftover and the deleted account's sessions are dead.
- **Delete:** any test asserting `create_admin` is the sole writer to `users`, and any
  test asserting logout merely clears the cookie.

### Gate

**Built, `make test` green; BM-1 still open.** `make test` covers the CRUD layer,
the guards and every new route. Two corrections to what this gate asked for: "a migration test
proving an old-schema DB is upgraded rather than skipped" is now the **opposite** assertion
(below the v5 baseline the CP refuses to migrate and says reinstall — D-29), and there is no
`409` single-flight on these routes — the write layer refuses with a `ValueError` that the
handler renders as `400`, and the last-admin/self-action guards run inside the transaction (D-2).

The portal UI this gate assumed **is built**: **People** in Settings (accounts, groups, and the
audit log on demand) and the self-service **My Profile** page at `/profile/`, opened from an
account popover that every signed-in page carries and that hides the gear for members. Both
password-change screens call the same `stub.ROTATE_JS`, so they cannot drift apart. QEMU
exercises the whole surface through nginx and **passes**, on a persistent user-data disk and
on a freshly created one: the gate redirect, create → forced rotation → session, the Open
WebUI mirror identity against `users.email`, membership and attribute validation, the guard
refusals, the audit actors, and delete. `last_login` is written on sign-in and rendered on My
Profile.

Reaching that result needed two changes outside the feature itself. The script's household
check was a **bare count** (`users == 1 and admins == 1`), so debris from an aborted run on a
persistent disk looked like a boot that had invented an account; it is now a roster check that
subtracts the smoke identities by name. Proving *that* change needed a genuinely cold disk,
and the cold disk then failed the pre-existing M6.6 S5 check: with no
containers on disk yet, the control plane provisions the data plane before the QEMU
image-preload unit has finished, and both containers came up referenced **by digest**
(`name@sha256:…`) rather than by tag — the boot-path pull S5 exists to catch. A digest
reference can only arrive that way through `docker pull`, because `resolve_image` prefers the
digest solely when `docker image inspect name@digest` succeeds and this profile's
`pullImage` tarballs carry `RepoTags` with no `RepoDigests`. S5 had never fired before
because every earlier run reused a warm disk. `lemonseed-qemu-docker-load-data` now orders
itself `before appliance-control-plane.service` as well as before the two data-service units,
so the preload wins the race it was written to win.

One thing this gate did **not** anticipate, found by writing the QEMU scenario: `create` was
leaving `password_reset_required` at its `FALSE` default, so an account the admin created could
sign in with the code the admin itself had chosen — no rotation, full session. D-11 says that
credential must rotate first, the People panel promises it in as many words, and `README.md`
apologises for it to the reader; only the `INSERT` disagreed. `create` now sets the flag, and
`test_usermgmt.CreateTests` pins it.

**BM-1 (with P4):** still open — create/rename/disable/delete a real second account on the
NVIDIA box and check the popover + Edit Profile flow in a browser.

---

## P4 — System groups, custom groups, membership

Deliberately **smaller** than the previous draft, on your "if it gets too complicated,
start with just the two system groups." The attribute *engine* ships; the attribute *UI*
does not, because the only attribute we can define today (content filtering) has no
effect until LiteLLM lands, and shipping a toggle that filters nothing violates our own
rule that a control must be something a home admin can act on.

### Problem

`groups(name, created_at)` cannot hold an attribute; `UsersHandler` ignores
`membership`; the six `groups.*` entries in `owui-policy.json`'s `api` map are declared
and unused (the backlog rule is: use them or delete them, so the manifest keeps meaning
"what we do"). The OWUI group-sync question that P1 §6 left open is **decided below**:
the CP owns groups and OWUI mirrors them.

### Proposal

- **System groups are surfaced, not redefined.** `admin` and `users` are flagged
  `system = TRUE`, shown in the panel as membership rosters with add/remove members, and
  immutable otherwise. The first admin already sits in both. **Corrected as built:** the flag
  is asserted by the **write layer** (`auth.ensure_system_groups`, an `INSERT … ON CONFLICT DO
  UPDATE` that re-asserts `system = TRUE` on every call), **not by migration SQL** — the
  additive-only migration guard requires `IF NOT EXISTS` on every non-`ALTER` statement, which
  an `INSERT … ON CONFLICT` cannot satisfy. Migration v6 only adds the `system` **column**.
  The upsert also means a hand-edited row converges on the next boot with no backfill pass.
- **Custom groups:** create, rename, delete, add/remove members. They can carry
  attributes but can **never** change authz level — enforced where it is computed
  (`auth.role` reads `ADMIN_GROUP`, nothing else), so the rule cannot drift.
- `effective_attributes(username)` = **OR over the user's groups**, exposed on the
  session identity so downstream consumers read exactly one place. Lists append *if*
  lists ever appear. This ships as a working engine with **no attributes defined**, and
  the group panel does **not** show an attributes editor yet.
  **Not built as written (P4 outcome).** There is no `effective_attributes()` and no
  per-user OR-resolution: `usermgmt.list_groups()` returns each group's attributes verbatim
  on the groups payload, and **nothing resolves them onto a user or onto the session
  identity.** That is consistent with this bullet's own "no attributes defined" caveat only
  in spirit — `content_filter` *is* defined and writable today, so the value exists with no
  reader. The OR-resolution arrives with the LiteLLM consumer that needs it (§11).
- **OWUI side — decided: the control plane owns groups, OWUI mirrors.** The CP is the
  only writer of groups and membership; the authority on the wire is already
  `X-Forwarded-Groups`, which nginx sets from the CP's session-verify response
  (`gateway.nix` `proxy_set_header X-Forwarded-Groups $auth_groups`) and OWUI consumes via
  `WEBUI_AUTH_TRUSTED_GROUPS_HEADER`. OWUI learns group membership at sign-in from that
  header; it is never asked to be the source of truth.
- **Delete the six `groups.*` api-map entries** (`owui-policy.json` lines 328–333:
  `groups.list`, `groups.create`, `groups.update`, `groups.users_add`,
  `groups.users_remove`, `groups.delete`). Verified: `owui_policy.py` references none of
  them, so they declare management intent the CP does not have. This is the backlog rule
  applied — use them or delete them, so the manifest keeps meaning "what we do".
  **DONE.** They are gone; the api map is **17 entries**, which also narrows the CP's own
  `sk-` key allow-list, since the map is what that key is restricted to (§3.5).
- **Hard constraint: OWUI stays a vanilla upstream container.** No source modification,
  no fork, no patching `UsersHandler`. Group sync is a mirror of CP state on sign-in,
  which the trusted-groups header already gives us. Any group behaviour we cannot get
  from vanilla OWUI plus the header is a CP-side feature or an upstream PR, not a local
  edit.
- **OWUI's own group UI is ADVISORY.** Because membership is CP-owned and re-derived on
  every sign-in, edits made in OWUI's group screens are not durable and must not be
  presented as the way to manage groups. The portal is the only group admin surface.
- Flip the account guards from `advertise` to `enforce` and drop "M6.2b" from the
  manifest `reason` strings. **Done, and the third guard went with the People panel.**
  `DELETE /api/v1/users/.*` enforces, `POST /api/v1/auths/add` enforces, and no
  `reason` names the retired label any more — none of it could, because nothing is
  advisory. `POST /api/v1/auths/add` waited for exactly one thing: a way to create an
  account that did not go through Open WebUI. The People panel is that way, so
  enforcing it now removes drift instead of removing capability. A brand-new OWUI
  database still bootstraps its first user, on the `signup` route, which is a
  different path. Separately, `POST /api/v1/users/%CP_OWUI_ID%/update` was
  **widened** to `POST /api/v1/users/[^/]+/update` (D-17): one rule now covers every
  e-mail, role and password edit in Open WebUI's user panel, and the id segment is `[^/]+`
  rather than `.*` so Open WebUI keeps its own self-service `users/user/settings/update`
  family.
- **Explicitly not in P4:** the content-filter attribute, its UI, and anything that
  reads it. Those arrive with LiteLLM, in the sprint where the toggle can actually
  filter something.

### Tests

- **Add:** system-group immutability (API refuses, UI omits the control); last-admin and
  self-action guards under concurrent calls; multi-group membership (a user in
  `users` + `Family` + `Kids`), OR-resolution over an empty set and over an unknown
  group; namespace-sweep tests (our groups survive an upstream `sync_groups_by_group_names`,
  a foreign group inside the reserved namespace is claimed); guard-enforcement tests for
  the two flipped guards, including that the CP's own service-account path still works.
- **Delete:** the advisory tripwires — `assertIn("M6.2b", spec.reason…)` in
  `test_owui_policy.py`, the `advisory`-set assertion in `scripts/qemu-api-smoke-test.sh`,
  and their manifest strings. These are *expected* to fail the moment the manifest
  flips; converting them is the phase, not a regression.
- **Add QEMU:** group CRUD through the portal API, a user in three groups, and an
  assertion that the account guards now answer 403-JSON for OWUI-UI writes while the
  CP's own path still succeeds.

### Gate

**Built, and this gate is now closed.** `make test` + QEMU cover group CRUD, the system-group
immutability rules and the guards. The `M6.2b` tripwire this gate was really about is **gone**:
the retired label appears nowhere outside `proposals/` any more, because the manifest reason it
was pinned to is no longer advisory. `test_owui_policy.py` now asserts the split in both
directions — **all 7 guards `enforce`, none `advertise`** — and keeps a weaker, forward-looking
rule in its place: any guard that is ever made advisory again has to say `until` something in
its `reason`. QEMU asserts the same shape from the running appliance (7 of 7 in the ownership
view, 7 declared and 7 rendered) and probes each enforced path at the edge, `POST
/api/v1/auths/add` included, so OWUI's user panel is read-only end to end (D-17b). The CP is
unaffected by its own guard because it reaches the slot port directly, never nginx — and the new
M7 QEMU scenario would catch it if that ever stopped being true, since it asserts the Open WebUI
mirror came back `ok` after a create.

**Shares the BM-1 bare-metal check with P3, which has not been run.**

---

## P5 — Power controls, the SYSTEM panel, and update affordances

### Problem

- The local console (`nixos/modules/console.nix`) is a tty4 status splash plus a tty2
  `agetty --autologin root` shell. There is **no menu** — everything today is typing on
  a console you have to find, which is clumsy for reboot, shut down, and update.
- **Nothing in the tree triggers an update from the running system**: no HTTP route, no
  unit or timer, no CP import, no nginx location. `lemonseed-update` is a root CLI only
  (`nixos/modules/update.nix`, subcommands `status|apply|cancel`).
- The CP cannot even exec it: `nixos/modules/control-plane.nix`'s `runtimeInputs` has
  no `lemonseed-update` and no GRUB tools.
- Rebooting mid-work is unguarded: `chat_ready._prepare_lock` / `_pull_lock` /
  `_pull_process`, `modules._lock`, `_reconcile_request_lock`, `_owui_reset_lock`, and
  `owui_policy._reconcile_lock` all exist and none is consulted by a shutdown path.
- The "box is going away" machinery is reusable and should be reused, not rebuilt:
  `chat_ready.REBOOT_PENDING_FILE` + `chat_snapshot`'s `state="rebooting"` + the branded
  `/_lemonseed/cp-down` page + the shell's `landingCopy()` reboot branch.
- Settings is two URLs (`/settings/` shell, `/settings/content/` inner iframe) with
  panels: Lemonade AI Engine, Large Language Model, **Status**, Chat, API.

**Resolved before this plan, so P5 does not re-litigate it:** graceful shutdown already
works — you verified `shutdown -h`, `shutdown -r`, and ACPI power-button on the current
image. So "what must stop, in what order" is off the table, and P5 is about *reaching*
systemd safely, not inventing a shutdown protocol.

### Two answers you asked for

**(a) "Do we have a mechanism to reboot without updating when an update is staged?"**
Yes before the reboot, no after — and your **D4 answer makes the question nearly moot**.

- **Before rebooting:** `lemonseed-update cancel` already does exactly the right thing —
  it unsets `boot_once` and `boot_attempted` and leaves `boot_default` alone
  (`scripts/lemonseed_update.py:write_inactive_slot`/`cmd_cancel`). Unit-covered by
  `scripts/test_lemonseed_update.py` (8 tests, green today).
- **After rebooting:** too late. Only GRUB consumes `boot_once`
  (`boot/grub.cfg`: it copies `boot_once` → `boot_attempted` and clears `boot_once`),
  so the slot switch is already done and `boot_default` still names the old slot. Going
  back means `lemonseed-ab set-once <old>` or `lock-in <old>` — both exist as root CLI,
  **neither is exposed anywhere, and neither has ever been tested.**

**D4 adopted: the update flow reboots by itself.** "Install update" means *download, write,
reboot* as one action, so "staged but not rebooted" stops being a state we design UI around.
That deletes from this plan the two-option reboot dialog, the staged-state warning, and the
**Cancel update** button (the CLI subcommand stays — it is the escape hatch for the rare
manual case). It also largely neutralises the `DESIGN.md` §12 hazard below, because the
reboot now happens as part of the update rather than by accident later.

The complexity does not disappear, though; **it moves into a background job**, and three
things become mandatory:

1. **`apply` cannot run inside an HTTP handler.** It is a multi-minute, ~3 GB download plus
   a partition write and a double hash. So the route takes a non-blocking lock, starts a
   daemon thread (the established `chat_ready` / `modules` / OWUI-reset pattern), answers
   `202 {"accepted": true}` immediately, and the UI polls status with `portalFetch` — the
   appliance reboots itself under the page when it succeeds. `apply --reboot` already exists
   in `scripts/lemonseed_update.py`, so this is not new updater behaviour.
2. **The confirm must set expectations**, because the reboot is unattended: the download is
   roughly the size of the system, it takes minutes, **the appliance restarts when it
   finishes**, and the model then has to be reloaded onto the GPU. You saw that last cost on
   bare metal.
3. **Retry is cancel-then-apply, never bare `apply`.** `refuse_dirty_state()` aborts when
   `boot_once` **or** `boot_attempted` is set, so a failed attempt leaves state that must be
   cleared first. A "Try again" that calls `apply` alone would be a bug.

`Reboot` and `Shut down` become what you asked for: `systemctl reboot` and
`systemctl poweroff` (the unit target is `poweroff`; there is no `systemctl shutdown`), with
no typing interstitial and no CP-side timer.

**(b) "How do we detect and highlight a failed update?"** Today we cannot, and the
reason is specific: **the intended version is never persisted.**
`lemonseed_update.py` logs `artifacts ready (version …)` and `fetch_manifest` never
writes `manifest.json` to disk, so after a reboot the appliance knows only the version
it booted (`/run/appliance/version` → `appliance_version()`). There is no pre-reboot
marker, no boot counter, and the one intended-vs-booted comparison in the tree
(`scripts/lemonseed-avahi-alias.py`, against `setup.confirmed_version`) is written once
at first-boot setup and never refreshed by an update.

The good news is the state needed to detect it already exists, unread: **`boot_attempted`
set while `boot_default` equals the booted slot is exactly the signature of a failed or
stuck attempt.** So P5's design is small and concrete:

1. Record `{intended_version, target_slot, attempted_at}` when we arm `boot_once`.
2. On CP start, compare it against the booted slot and `appliance_version()`.
3. If they disagree → an **admin-facing update-failed banner** naming the version we
   tried for and the version we are running, with **Try again** as the action.

With D4 adopted this detector becomes **more** important, not less: the reboot now happens
unattended, so the banner is the only way anyone learns the update did not land. Retry is
the cancel-then-re-apply sequence above.

One hazard we are **not** changing: because only GRUB writes `boot_attempted`, an ordinary
reboot while an update is armed still silently completes the upgrade even if that slot is
unhealthy. D4 makes that window small (staged state only exists for the seconds between
`apply` finishing and the reboot it triggered), but it does not close it.

### Proposal

**One SYSTEM panel, formed by merging the existing STATUS panel into it.** Both are
"about the box", and merging is what lets the running version sit next to the latest
available, which is the whole point of an update affordance. Everything you already
reviewed and liked (Lemonade AI Engine, Large Language Model, Chat, API) stays put.

In order:

1. **Version** — running version, and when known, the latest available.
2. **Update** — *Check now*; *Download and install* armed only when a newer version exists
   (**it reboots the appliance when it finishes**, per D4); the update-failed banner when
   intended ≠ booted, with *Try again*. No *Cancel update* control; the CLI covers it.
3. **Reboot** and **Shut down**.
4. Existing status content: setup state, timezone, GPU, health.

**Confirms are sized to the damage.** Reboot, shutdown and update are data-safe: an
explainer plus a plain confirm, **no typed interstitial** — that stays reserved for
data-destroying actions (settings reset, user delete). Your wording is the spec, and the
update confirm additionally has to say the box will restart by itself when the download
finishes:

> **LEMONSEED SHUTDOWN** — This will power off the lemonseed appliance, disconnecting
> any active users. Would you like to proceed?

Accepting hands off to the existing going-away path (`REBOOT_PENDING_FILE` →
`landingCopy()` → the branded 503 page), which is also what the update's own reboot rides on
so the chat frame shows the same interstitial either way.

**Install is a job, not a request.** `POST …/update` takes the non-blocking lock, starts the
daemon thread that runs `lemonseed-update apply --reboot`, and returns `202 accepted`
immediately; a status field on the existing snapshot surface reports
`downloading` / `writing` / `rebooting` / `failed` and the UI polls it with `portalFetch`.
No streaming, no new protocol — the same shape the OWUI reset already uses.

**systemd owns the timing.** The CP asks for `systemctl reboot` / `systemctl poweroff`
and gets out of the way. Your question — "is this something systemd can handle rather
than wrapping systemd with another script?" — is answered yes and taken: no CP-side
countdown, no second watchdog. Timeouts and the kill hierarchy are systemd's job, and a
CP countdown would be a second, worse implementation of the same thing.

**Interlock before acting.** Acquire-or-refuse the locks above and check `_pull_process`;
on refusal return `409` naming the activity ("a model download is in progress"), so the
UI can say *why*. This gate runs before the job starts, not inside it — including for the
update path, where a reboot would otherwise be scheduled mid-pull.

**Update checks: one workflow, two triggers.**

- **Weekly automatic** with a large fuzz so appliances do not synchronously hammer the
  release endpoint: `sleep $(( $RANDOM * 86400 / 32768 ))` then the check, under a
  weekly timer.
- **"Check now"** in the SYSTEM panel.
- Both run **the same** workflow, so there is one definition of "an update exists".
- On finding one: a **red dot on the settings icon for admins**, and the *Download and
  install* button arms.
- **Never auto-download, never auto-install.** A silent download competes with a model
  pull for the disk guard and the WAN link; and now that install implies a reboot, a silent
  install would silently restart a box people are using.

**CP plumbing P5 needs regardless of the above:** `lemonseed-update` and a grub env tool
in `control-plane.nix`'s `runtimeInputs`. Note also that the updater's grub.cfg refresh
is silently skipped unless `LEMONSEED_GRUB_CFG` is in the environment, and no module in
`nixos/` creates either candidate path (`/etc/lemonseed/grub.cfg`,
`/usr/share/lemonseed/grub.cfg`). That is worth a 60-second live check on your box
before we build a UI on top of it.

### Release channel — **split out to M8**; D2 already applied to the tree

Per your note, the updater refactor, release signing, and the move to GitHub releases leave
P5 entirely (see "Split out to M8"). **D2 — which channel — is decided
(`http://dev-server/releases/stable/`, because the public name is behind Cloudflare
and Cloudflare prohibits large proxied objects), and it is already applied**, so P5's
*Check now* starts pointing at a channel that actually has releases:

| Site | Before | Now |
|------|--------|-----|
| `scripts/lemonseed_artifacts.py` `IMAGE_BASE_DEFAULT` | `https://lemonseed.ai/releases/stable/` | `http://dev-server/releases/stable/` |
| `installer/install.py` `IMAGE_BASE_DEFAULT` | same | same change, with a comment that the two must agree |
| `scripts/push-image-to-website.sh` `URI` | `https://lemonseed.ai/releases/stable/` | `http://dev-server/releases/stable/` |
| `nixos/installer/modules/web.nix` `LEMONSEED_IMAGE_BASE_URL` | already the test URL | unchanged |
| `DESIGN.md` §8 + decision log | "lemonseed.ai" | internal origin record, with the Cloudflare reason and the signing caveat |

Two honest caveats:

- **`SCP_PATH` stays `/websites/lemonseed.ai/releases/stable/` — resolved, and the
  question itself was the wrong one to ask.** `scripts/push-image-to-website.sh` is the
  existing, in-use deploy path for the host that serves `dev-server`; it is not
  new code with an unverified constant, it is the mechanism that already publishes this
  channel. **Keep using it. No new deploy mechanism until GitHub releases land in M8.**
  Whether `SCP_PATH` and the vhost root line up is therefore settled by the next real
  publish (push, then fetch `${URI}/manifest.json`), not by reading the hosting panel —
  which is a release-time check, not an open design question. `SCP_PATH` is unchanged.
- `proposals/M4/USB-INSTALLER.md` still names `https://lemonseed.ai/releases/stable/`. That is
  a historical milestone record, so I left it; `DESIGN.md` is the contract.

This also raises the stakes on signing, which is M8's first job: an internal HTTP origin with
**no signature verification anywhere in the tree** means anyone who can poison
`dev-server` can serve a malicious `root.squashfs`. The manifest's SHA-256s come from
the same origin, so they prove transfer integrity, not authenticity.

### GRUB config refresh — **two fixes, folded into P5** (from resolving Q3)

A live field defect, not a hypothetical, and the root cause is worth stating
because the symptom looks like a broken updater:

> An installed box never refreshes the ESP `grub.cfg` on update. That file changes
> only if the machine is reinstalled from USB.

The chain, verified in the tree rather than inferred:

1. `scripts/lemonseed_update.py` resolves the template as
   `GRUB_CFG = Path(os.environ.get("LEMONSEED_GRUB_CFG", "/etc/lemonseed/grub.cfg"))`,
   with `GRUB_CFG_FALLBACK = /usr/share/lemonseed/grub.cfg`.
2. No `nixos/` module creates either path on the **installed system**. The template
   is staged into the *control-plane* store path — `control-plane.nix` exports
   `LEMONSEED_GRUB_CFG=${cpRoot}/lemonseed/grub.cfg` for the CP service. The
   updater is a different service and does not get it.
3. The `lemonseed-update` wrapper in `nixos/modules/update.nix` exports only
   `PYTHONPATH`, `LEMONSEED_AB`, and `LEMONSEED_DOWNLOAD_DIR`. Without
   `LEMONSEED_GRUB_CFG` the updater falls back to the two nonexistent paths.
4. `write_inactive_slot` then guards the refresh with `if grub_src.is_file():` and
   **no `else`**, so the step is skipped silently and the update reports success.

**Fix 1 — export the template path in the updater wrapper.** Add
`LEMONSEED_GRUB_CFG` to the `lemonseed-update` wrapper's exports, pointing at the
same staged `boot/grub.cfg` the control plane already receives. This makes the
existing refresh code actually run.

**Fix 2 — make the skip loud.** `write_inactive_slot` must not proceed quietly when
no template resolves. Either log a conspicuous warning, or treat a missing template
as a failed slot write. Silent skipping is what let this survive every shipped
update: a success path that skips a step is worse than a failure, because nothing
anyone observes contradicts it.

The two fixes are inert without each other, so they land together. Fix 1 alone
still hides a missing template; Fix 2 alone would turn every installed box's next
update into a hard failure.

**Test:** an updater unit test with `LEMONSEED_GRUB_CFG` unset asserting the run
fails or warns loudly rather than reporting success — the present behaviour — plus
one asserting a valid template is copied to the ESP path the code actually writes,
pinned from `write_inactive_slot` rather than from this document.

### Tests

- **Add:** interlock unit tests (each lock held → `409` with the reason surfaced); the
  update-check decision table (up-to-date / newer available / no network / malformed
  manifest); **"never auto"** — no code path may reach `apply` without a request; **"install
  is a job"** — the route returns `202` without running `apply` synchronously, the status
  transitions are observable, and a second request while one is running gets `409`; the
  failure detector (intended ≠ booted → banner; marker cleared on a matching boot); the
  **cancel-then-reapply** retry sequence, proving `apply` alone is correctly refused while
  `boot_attempted` is set; that the reboot path issues `systemctl reboot` / `poweroff` and
  nothing else.
- **Add (QEMU):** power/update **dry-run** — the CP computes the command without executing
  it; and one real **reboot round-trip** asserting the appliance comes back, Postgres is
  healthy on `/api/data`, and the interstitial rendered while it was down.
- **Delete/replace:** any assertion that `/api/status` alone is the readiness signal for a
  reboot — M6.6's status split changed that shape, and the going-away signal is
  `chat_snapshot`. Logout's frame-teardown assertions move to P3's suite rather than being
  duplicated here. The staged-state dialog tests from the earlier draft are **never added**,
  and if any survive from a prior draft they go: D4 removed the state they described.

**Built.** The two `grub.cfg` fixes shipped first — `nixos/packages/lemonseed-update.nix`
exports `LEMONSEED_GRUB_CFG` (with a store-path fallback), and a slot write whose
template cannot be resolved now **fails loudly** instead of silently skipping — then the
affordance: check and install run as **jobs** behind the admin-gated
`GET/POST /_lemonseed/api/update…` routes (`202` plus a pollable status; the power
buttons refuse while an install runs), retry is cancel-then-apply, the failed-update
banner reads the arm-time `update-attempt.json` the updater now records, and a weekly
`lemonseed-update-check` service keeps *latest* fresh. `make test` covers the job
backend, the phase mapping, and the HTTP surface (members `403`; audit
`update.install` / `update.retry`). The bare-metal items below are **BM-3**, still yours.

**BM-3 review, applied:** the version rows are their own **Software update** box below
Power (row title *Current Version*); *Check now* says what it is doing and the poll
revives the buttons; the install dialog now **holds** across the whole job with a
progress bar fed by the updater's stdout percentages, and the status API answering
after the reboot sends the browser to `/login`; the failed dialog offers
**Retry** (cancel-then-apply, re-fetching any corrupt download) and **Cancel**,
and its copy wraps long checksums (bare-metal review, BM-4: a truncated push
left the checksum failing with only a Hide button and a hash past the box).
`lemonseed-update apply` preflights before touching a disk — an unknowable slot
pointer, a pending `boot_once`/`boot_attempted`, a leftover arm-time record, or a
channel version equal to the running one are each refused with a clear message and
`--force` as the CLI escape. The update's first real use also surfaced an unrelated
defect: on the Strix Halo machine the updated image never loaded `amdgpu` at boot
(simple-framebuffer held the display, Vulkan fell back to llvmpipe, lemond probed
NVIDIA every second) — both GPU stacks now load only when the hardware is present,
via udev modalias rules (`services.udev.extraRules`: PCI class 03 + vendor 1002
for `amdgpu`, 10de for `nvidia`; udev's built-in coldplug missed the device on the
updated image, while a manual `modprobe` bound instantly, so the rules fire the
load explicitly). Same review: the control plane's per-request access trail moved to **its own journald channel** (`-t appliance-control-plane-access`), each line carrying the real client IP from `X-Forwarded-For` and the forwarded username instead of the loopback address; and nginx's proxy-headers hash is sized for the identity gate, so the parse warnings that flooded the compiled default `/var/log/nginx/error.log` are gone (nginx errors were already journald).

**BM-2 (yours):** reboot, shut down, and sign out with the chat frame open; *Check now*
against `dev-server`; run a real **Download and install** and confirm it reboots
itself, shows the interstitial, and comes back on the new version; then — if you are willing,
since it is the one path no automation can reach — install a **bad** build and confirm the
failure banner appears after the fallback boot.

---

## Split out to M8 — Release & distribution

Too large to sit inside P5, as you said. Pulled out whole so nothing is lost:

- **Updater refactor** and the **GitHub releases** migration. The existing flat asset
  layout (`vmlinuz`, `root.squashfs`, `initrd.img`, `manifest.json`) already matches
  what a GitHub release can carry, and `lemonseed_update.py apply --url` already takes
  an explicit base URL, so the shape is compatible; the work is the URL scheme, asset
  handling, and rate-limit/auth behavior against GitHub.
- **Signing.** Today: per-artifact SHA-256 from the manifest, stream-verified during
  download, plus a post-write read-back hash of the partition. **There is no signature
  verifier.** `scripts/sign.sh` writes `.sig` files and `scripts/package.sh` ships
  `"signing": {"scheme": "detached-ed25519", "enabled": false}`, but nothing in
  `scripts/`, `installer/`, `control-plane/`, or `nixos/` ever reads a `.sig`, and no
  public key is in the image. Because `manifest.json` comes from the *same origin* as
  the blobs over plain urllib, the hash chain is **transfer integrity, not
  authenticity** — which `DESIGN.md` §11 already records as the intended future root of
  trust. Also relevant to M8: GitHub's asset `digest` field is likewise integrity only.
- **Sharding.** Payload blobs must be <2 GB; `root.squashfs` is ~3 GB today. Your shape
  (tar the payloads, cut the tar into 1 GB chunks, `manifest.json` outside the tarball
  describing both the chunks as inputs and the files as outputs) is the right one and is
  recorded so M8 starts from it.
- **`make release`** — sign and publish a GitHub release. Deferred here deliberately: do
  it once, with the real URL scheme in hand.
- **Roll back to version X.** Your idea, and it is a good one, but it cannot be built
  today for a concrete reason: **nothing records which version is in which slot.** No
  code tags `vmlinuz-a`/`vmlinuz-b` or a partition with a version, so "version X" is not
  a thing we can name in a dialog. Primitives exist (`lemonseed-ab set-once`/`lock-in`)
  but are untested for this purpose. Two prerequisites for M8: per-slot version tagging
  at write time, and a tested rollback case. The reason it needs testing rather than
  confidence: rollback is *probably* safe because migrations are additive-only and the
  runner deliberately tolerates a database newer than its own code (`db.migrate_cp_schema`
  returns early rather than refusing, explicitly so "an A/B rollback does not become a
  bricked appliance", asserted by `test_db.py`), **but user-data Postgres is shared
  across slots** — so anything already written by the newer CP (and an operator-triggered
  OWUI `config` TRUNCATE) is *not* undone by reverting the slot.

  P5 helps this along without claiming it: recording `{intended_version, target_slot}` when
  we arm `boot_once` is the first place the appliance ever writes down "which version is in
  which slot", but it only knows the slot we just wrote, not the one we are booted in.

## Beyond P1–P5

- **LiteLLM friendly model names** — stays later in M7, as you confirmed. Exposes
  `Lemonseed AI` and `Lemonseed AI with Content Filters` in front of lemond so model
  name/path changes stop churning OWUI's model config, gives OWUI a stable model name,
  and gives us the OpenAI-compatible endpoint for external apps. The manifest side is
  already prepared and deliberately empty: `owui-policy.json` reserves the
  `lemonseed-ai` / `lemonseed-ai-filtered` ids and the `^Lemonseed.*$` name space with
  `"definitions": {}` and the reason *"Definitions arrive with Phase 5 (LiteLLM)"*. (That
  "Phase 5" is the retired **M6.3b** phase number, not M7's P5 — worth disambiguating when
  the string gets rewritten in the LiteLLM sprint.)
  **Coupling to note: this is the sprint where P4's group attributes finally get a
  consumer and where the content-filter toggle becomes honest.** Per P4's outcome above,
  `effective_attributes()` was **not** built — attributes are published per group on
  `GET /api/users` and are not resolved onto a user — so this sprint owns both the
  OR-resolution and the consumer, not just the consumer.
- **Modules: define them, then ship one PoC.** Your split is the right one, and the
  names want to be distinct because the classes have different contracts:
  - **Applications** — full containers (or compose networks) that give the user a new
    app, surfaced as a tab (`HomeAssistant`). They need a URL, an iframe or a link, and
    identity headers.
  - **Plugins** — MCP tools or other enhancements the user reaches *through* chat, never
    directly over HTTP (`SearXNG`, `Wikipedia`). They need a **registration step** so the
    CP can wire them into OWUI's tool config, which is a lifecycle Applications don't
    have.
  - **Hardware enablement** (third class, explicitly not yet: a legacy NVIDIA GPU needing
    closed drivers). Named here so it is not rediscovered later as an oversight.
  Both of the first two consume the identity contract from P1 §7 — a module definition
  declares which of the forwarded headers it needs.
- **Security milestone: block-device crypto + TOTP.** LUKS for user-data with a migration
  plan, TPM where available, GRUB password, and closing the empty-root-password tty2
  bring-up hole (`DESIGN.md` §9) so the encryption story is real. **This milestone also
  owns TLS on the gateway** (D1 option D) and the TOTP decision, since those two are the
  same conversation.

## Also true, and worth not losing

**M6.4 AMD support is untested on AMD hardware.** The Vulkan backend is unit-covered, but
`nixos/modules/qemu-test.nix` sets `lemonseed.lemond.llamacppBackend = "cpu"`, so no
automated test has ever loaded a model through Vulkan, and no AMD GPU has been available
for the tier-D check in `proposals/M6/AMD-VULKAN-M64.md`. Treat the AMD path as
*implemented, unvalidated*. If an AMD box appears, that tier-D checklist is the whole
test plan and needs no new code. *(Update at M7 close: the Strix Halo box is now the
primary bare-metal test machine — Vulkan, shared-memory carve-out and boot-time `amdgpu`
load all verified there. What remains unvalidated is the AMD **dGPU** path only.)*

## Decisions

**Settled by your review — recording them so they do not get re-opened mid-build:**

| | Decision |
|---|---|
| **D1** | No TLS is achievable (no FQDN; browsers reject self-signed certs; a private CA is beyond a home user). Status quo today is a **plaintext password in the request body** — verified in code, not assumed. Fix: **SRP-6a**, built as **P2**, plus moving rate-limit state to Valkey. TLS stays backlog, labelled *blocked on FQDN/CA UX*. |
| **D2** | Channel is `http://dev-server/releases/stable/`; the public name is behind Cloudflare, which prohibits large proxied objects. **Already applied to the tree** — see the table under "Release channel" in P5. |
| **D3** | Reduced groups phase: ship the engine + system-group immutability in P4, defer the attribute editor and the content-filter attribute to the LiteLLM sprint. **Partly overtaken by P4 as built:** the key `content_filter` and its editor routes (`groups/attribute`, `groups/attribute-remove`) did ship, so what remains deferred is the **consumer** and the portal editor. Nothing reads the attribute yet, which is the honest reading of "deferred to the LiteLLM sprint" — but the value is writable today, so a later sprint should not assume the key arrives blank. |
| **D4** | The update flow reboots by itself. Reboot and shutdown are plain `systemctl reboot` / `systemctl poweroff`; no typed interstitial on data-safe actions. This replaces the "choose: reboot into new version or not" dialog from your earlier note — you asked how the auto-reboot would land, and the honest answer is that it *deletes* that dialog. *(BM-4 supersession: a failed update now offers **Retry/Cancel** in the dialog — Retry is cancel-then-apply; there is still no way to cancel an update that is running.)* |

**Resolved since review — each was answerable, and the answer is recorded with
where it came from, so none of them gets re-litigated from memory:**

| | Question | Answer, and how it was established |
|---|---|---|
| **Q1** | In Chrome on the portal, does `window.crypto.subtle` evaluate to `undefined`? | **Yes — `undefined`.** Verified on the portal over plain HTTP: a non-secure origin gets no SubtleCrypto, which is exactly why the appliance can never have it. The ~60-line pure-JS SHA-256 stays, and there is now only one code path to get wrong rather than a subtle/fallback pair. Pinned by `test_srp_client_js.py`, which executes that SHA-256 against the FIPS 180-4 vectors *and* against `hashlib` on identical inputs, including the 55/56/63/64/65-byte block boundaries where hand-rolled padding usually breaks. |
| **Q2** | On `the dev web host`, is `dev-server` served from `/websites/lemonseed.ai/releases/stable/`? | **Resolved by decision, not by inspection: `scripts/push-image-to-website.sh` remains the deploy mechanism, unchanged.** It is the script already used to publish this channel, so it is the authority on where the bits go; `SCP_PATH` is left exactly as D2 set it. No new deploy tooling until M8 moves the channel to GitHub releases. The directory question collapses into a release-time check — publish, then fetch `${URI}/manifest.json` — rather than an open design question. |
| **Q3** | Do `/etc/lemonseed/grub.cfg` or `/usr/share/lemonseed/grub.cfg` exist? | **Neither is created by any `nixos/` module** — confirmed by inspection, and it is the root cause of a real field defect: `write_inactive_slot` refreshes the ESP `grub.cfg` only when `LEMONSEED_GRUB_CFG` is set, and the `lemonseed-update` wrapper does not export it. So an installed box never refreshes its GRUB config except by USB reinstall. Fix folded into P5 (see below). Note the control plane *does* receive `LEMONSEED_GRUB_CFG`; the gap is specific to the updater wrapper. |
| **Q4** | P1 §6: OWUI group ownership. | **Decided: the CP owns groups and membership; `X-Forwarded-Groups` is the authority; OWUI mirrors on sign-in.** Chosen over "OWUI owns its groups" because the upstream sync **deletes** memberships absent from the header, so the two-owner arrangement is not merely untidy — it silently evicts users. Consequence for P4: the six unused `groups.*` entries in `owui-policy.json` are deleted rather than wired up, OWUI's own group UI is treated as **ADVISORY** (a mirror that the next sign-in will overwrite), and the hard constraint stands that **OWUI remains a vanilla upstream container with no source modification**. |

The Q3 finding is worth stating plainly because it changes P5's framing: the GRUB
refresh gap is not a hypothetical that the update UI might sit on, it is a defect
that already affects every installed box.
