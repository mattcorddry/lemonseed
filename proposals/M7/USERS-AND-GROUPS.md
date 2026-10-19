# M7 P1 — Users & Groups: design record

**Status: approved (rev 2).** This document is P1's deliverable
(`proposals/M7/PLAN.md` P1 §Deliverable). It is a **decision record**, not a
research note: every numbered decision is made here. §11 has no open items, so
**P3 is unblocked and building.** Rev 2 folds in the review decisions: no
`password_hash` in the new schema, no backfill or pre-M7 compatibility code
anywhere, browser-generated admin OTP reset, `owui_id` + synthetic OWUI
identity, `group_attributes` as JSONB, a retained `audit_log`, and a stricter
username rule.

Scope discipline: the *mechanism* of authentication was decided in D1 and is
already built (P2, SRP-6a). This document does not re-open it. It decides what
can be done with accounts now that the server never sees a password — which
turns out to be a smaller set than the old plan assumed.

Conventions: `file.py:NN` references are the state of the tree at the time of
writing. "Verified" means read from the tree, not inferred.

**Pre-M7 is dead.** There are no deployed Lemonseed systems before M7; the only
boxes in the world are two test machines, and the answer to any state we cannot
migrate forward is *reinstall*, not a compatibility path. So this design
deletes rather than deprecates, and it writes no backfill (§2). The cost is
explicit: **A/B rollback across the M7 boundary is gone** (D-21), and we accept
that until there is an install base worth protecting.

---

## 1. Where things actually stand

PLAN.md's P1 "Problem" list was written before P2 landed. Corrections, because
the stale parts change what P3 can assume:

| PLAN.md said | Now |
|---|---|
| "Login rate limiting is in-process memory … a restart clears a lockout" | **Fixed in P2.** Failure/lockout counts are Valkey TTL keys (`auth.py` `lemonseed:rl:f:<ip>` / `:l:<ip>`), mirrored in process dicts as fallback, failure direction **allow**. |
| "`db.SCHEMA_VERSION = 4`" | **Now 5.** P2 added nullable `users.srp_salt` / `users.srp_verifier`. The next step here is **v6** (§2). |
| "`password_hash` … reset password writes a hash" | **No longer possible.** bcrypt is deleted outright (`verify_credentials`, `hash_password` gone); `users.password_hash` is vestigial and is **dropped from the schema outright** in §2's baseline rewrite. |
| "Open blocker: OWUI group ownership" | **Decided (PLAN.md Q4).** CP owns groups and membership; `X-Forwarded-Groups` is the authority; OWUI mirrors on sign-in; the six unused `groups.*` api-map entries get deleted in P4; OWUI stays a vanilla upstream container. |
| "no AMD hardware" | Unrelated to this feature, but corrected in `DESIGN.md` §2/§11/§13 the same day: Strix Halo APU validated, AMD dGPU not yet booted. |

Verified current state that this design builds on:

- `users(username, display_name, email, password_hash, disabled, created_at,
  token_generation, last_login, srp_salt, srp_verifier)`, `groups(name,
  created_at)`, `user_groups(username, name)` `ON DELETE CASCADE`. `groups` has
  **no attribute columns**. Baseline DDL is `db.py:181-191`; migrations v4/v5
  are `db.py:278-281`.
- `auth.ADMIN_GROUP = "admin"`, `auth.BASE_GROUP = "users"`; `create_admin`
  joins the first admin to **both**. `auth.role()` is `admin` iff `admin` ∈
  groups. So the two-system-group model already ships; what is missing is that
  neither is *marked*, so nothing stops a UI renaming or deleting them.
- `srp_load_user()` (`auth.py:368`) returns `None` for a **disabled** row, and
  `verify` re-reads rather than trusting the challenge. **A disabled account
  already cannot complete a handshake in flight.** P1 adopts that as the
  contract; no new work.
- `owui_client.user_update(user_id, form)` → `POST /api/v1/users/{user_id}/update`
  **already exists** (api map `users.update`), as does `user_delete`
  (`owui_client.py:377-383`), which has **zero call sites today**.
- The CP reaches OWUI at `http://127.0.0.1:{port}` (`modules.py:598-608`) —
  **directly, not through the nginx gateway**. nginx guards therefore cannot
  block CP writes. Verified, and load-bearing for §7.
- `owui_policy.py:908-915`: `UsersHandler.desired()` is **keyed by email** and
  `apply()` ends with `"the loop never deletes"` (`:954`). `USER_SCOPES` is
  `{"membership", "is_admin"}` (`:56`).

### 1a. Two defects found while writing this, both now in P3's scope

These were not in PLAN.md and are not theoretical; both were verified in the
tree, and both are prerequisites for the flows below actually working.

**F-1. The session's OWUI identity reads `users.email`, so editing email
duplicates accounts — and would do it for *every* account the moment email
becomes a real address.** `owui_identities()` publishes
`"email": row["email"] or owui_email(username)` (`auth.py:617`, and the
docstring at `:593` says so outright), `create_admin` seeds `users.email` with
that same derived value (`auth.py:717`), and `create_session` copies
`user["email"]` into the session record (`auth.py:308`), which the gate turns
into `X-Forwarded-Email` (`stub.py:2341` → `gateway.nix:133`). Today
`users.email` *is* the synthetic `{username}@localhost`, so nothing breaks.
P3 makes `email` a real, editable household field (§10), which without a change
would reproduce the duplicate-account bug from §7 for every user who touches
their profile. **D-16′ is what closes it.**

**F-2. "Reset admin credentials" is a no-op today.** The installer writes the
marker to the ESP (`install.py:498-503`) and comments name
`lemonseed-auth-reset-seed` as the unit that relocates it to user-data;
`auth.RESET_MARKER_FILE` is `/var/lib/lemonseed/reset-admin` (`auth.py:36-37`).
**That unit does not exist** — the only seed unit in the tree is
`lemonseed-auth-gate-seed` (`gateway.nix:227`, `:686-694`), which writes empty
gate snippets and nothing else. Grepping `lemonseed-reset-admin` across the
repo returns only the installer write and two comments. So the checkbox has
never reset anything, and D-20 (forgotten admin password) does not work until
it is fixed. **D-27 in §8.**

---

## 2. Schema: one baseline rewrite, then v5 → v6

The additive-only rule (`test_db.py:190-205` bans `DROP`, `RENAME`,
`ALTER COLUMN`, `TRUNCATE`, `DELETE`, `UPDATE ` **in migration SQL**) has a
loophole that the earlier draft of this document missed and that I previously
described wrongly: **the baseline DDL in `_create_cp_tables` is not migration
SQL.** `test_only_additive_migration_sql` iterates `db.MIGRATIONS` only. So a
column can be removed from the shape we create for *new* boxes, as long as no
migration drops it from a box that already has it. With no pre-M7 install base
that is exactly the case we are in, so:

**D-29. Baseline rewrite.** Fold the two shipped migrations (v4
`token_generation`/`last_login`, v5 `srp_salt`/`srp_verifier`) into
`_create_cp_tables`, delete both `_migrate_v4` and `_migrate_v5`, set
`BASELINE_SCHEMA_VERSION = 5`, and add **one** v6 step for everything P3/P4
need. `test_migrations_cover_the_declared_version` (`test_db.py:183-190`)
asserts `MIGRATIONS == range(BASELINE+1, SCHEMA+1)`, so the rewrite is
self-enforcing, and `test_upgrade_from_baseline_adds_only_the_new_columns`
(`:135-155`) enumerates exact DDL, which is the intended friction: DDL that
reaches a box must be visible in a diff.

Dropping `password_hash` from the baseline answers the review question directly
— my earlier "cannot be dropped" was wrong. What is true is that **no migration
may drop it**; the baseline is ours to write.

The rewritten baseline shape for `users` (what a fresh box gets):

```sql
CREATE TABLE IF NOT EXISTS users (
  username TEXT PRIMARY KEY,
  display_name TEXT NOT NULL DEFAULT '',
  email TEXT NOT NULL DEFAULT '',
  disabled BOOLEAN NOT NULL DEFAULT FALSE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  token_generation INTEGER NOT NULL DEFAULT 0,
  last_login TIMESTAMPTZ,
  srp_salt TEXT,
  srp_verifier TEXT
);
```

A database stamped below the baseline cannot be upgraded (v4/v5 are gone), so
`migrate_cp_schema` must **raise** rather than silently skip steps:
`"CP schema version N predates the supported baseline 5; reinstall the
appliance"`. `provision_data_plane` already converts any raise into
`{"state": "error", "error": …}` (`db.py:401-402`), so the appliance reports
instead of bricking. `version == 0` still means "just created, assume baseline".

### v6 — the one migration P3/P4 add

| Table | Change | Notes |
|---|---|---|
| `users` | `ADD COLUMN IF NOT EXISTS password_reset_required BOOLEAN NOT NULL DEFAULT FALSE` | §5/D-11. Default `FALSE` — no existing account is forced to rotate. |
| `users` | `ADD COLUMN IF NOT EXISTS owui_id TEXT` + `CREATE UNIQUE INDEX IF NOT EXISTS users_owui_id_key ON users (owui_id) WHERE owui_id IS NOT NULL` | §7. Nullable because a user who has never been created in OWUI legitimately has none; unique-with-partial-index so two rows can never claim one OWUI account. |
| `groups` | `ADD COLUMN IF NOT EXISTS system BOOLEAN NOT NULL DEFAULT FALSE` | §3. `FALSE` default means custom groups are non-system without a backfill. |
| `group_attributes` | `CREATE TABLE IF NOT EXISTS (group_name TEXT NOT NULL REFERENCES groups (name) ON DELETE CASCADE, key TEXT NOT NULL, value JSONB NOT NULL, PRIMARY KEY (group_name, key))` | **D-30: JSONB**, per review. See why below. P4 creates its readers; P3 does not need them. Listed here so one migration covers both phases. |
| `audit_log` | `CREATE TABLE IF NOT EXISTS (id BIGSERIAL PRIMARY KEY, at TIMESTAMPTZ NOT NULL DEFAULT NOW(), actor TEXT NOT NULL, target TEXT NOT NULL DEFAULT '', action TEXT NOT NULL, detail JSONB)` | §11. |

Two things this table deliberately does **not** do, both forced by
`test_only_additive_migration_sql`:

- **No seeding.** Every non-`ALTER` statement must contain `IF NOT EXISTS`, so
  `INSERT INTO groups … ON CONFLICT DO UPDATE SET system = TRUE` is illegal
  here (and contains a banned word besides). Seeding the two system groups
  stays in the write layer, which needs to be idempotent anyway.
- **No backfill of any kind.** Not `owui_id`, not `users`-group membership, not
  `system`. See D-31.

**D-30, why JSONB for `group_attributes`.** The review asked for bool + string
+ list. A sparse-typed row (`value_bool`, `value_string`, `value_list`) needs a
DDL change for the fourth type, which under additive-only means a new schema
version per type. A flat `TEXT` value plus `value_type` stores booleans as
`"TRUE"`/`"true"`/`"True"` and invites exactly the drift that makes a boolean
check wrong in one code path and right in another. JSONB gives real booleans,
lists and strings, psycopg2 hands back native Python types, and
`jsonb_typeof(value)` is derivable — optionally a
`value_type TEXT GENERATED ALWAYS AS (jsonb_typeof(value)) STORED` column if a
query ever wants it indexed. The cost is that the database no longer knows the
*meaning* of a key, so the key→type registry lives in code (§3, D-3).

**D-31. No backfill, ever.** Per review: backfill logic is permanent tech debt
maintained for machines that do not exist. Concretely, P3 writes no
`owui_id`-adoption pass, no "adopt missing `users`-group join" pass, and no
repair for the duplicate `admin@localhost` row on the test box (it gets
reinstalled). `owui_id` is instead **captured at creation**:
`POST /api/v1/auths/add` returns the created user's `id`
(`models/auths.py:1128-1136`, read from the pinned image), so the CP learns the
id the moment it creates the account and never needs to go find it. §7 states
the ordering rule this depends on.

`users.last_login` already exists and needs only a **write** on successful
sign-in, in the same code path that issues the cookie — one statement, not a
background job.

---

## 3. System groups

`admin` and `users` are seeded, flagged `system = TRUE`, shown in the panel as
membership rosters with add/remove members, and **immutable otherwise**.

**D-1. authz level derives from system-group membership only.** `auth.role()`
reads `ADMIN_GROUP` and nothing else — custom groups can carry attributes and
can **never** change authz level. Enforced where the role is computed, so the
rule cannot drift.

**D-2. Three guards, enforced in the write layer, not the UI**, so a direct API
call cannot bypass them:

1. A `system` group cannot be renamed or deleted → `403 {"detail": …}`; the UI
   does not offer the control; QEMU asserts both.
2. The **last admin** cannot be demoted, disabled, or removed from `admin`.
   `auth.admin_exists()` already answers the question, and already returns
   `None` for "Postgres not ready" — **`None` must be treated as *refuse*.**
3. An admin **cannot delete or disable themself** — this is what stops the
   two-tab race where admin A demotes admin B mid-demote.

**D-3. `users` group membership is a stored row**, not implied. Group joins are
the read path and effective-attribute resolution wants the fact explicit.
`usermgmt.create()` joins every new account to `users` (D-32); there is no
adoption pass for rows that lack it (D-31) because there are no such rows.
The same discipline applies to `group_attributes`: keys are declared in a
code-side registry (`key → type, default, validator`), so an unknown key is a
rejected write rather than a silently-typed blob.

---

## 4. Password lifecycle — what SRP actually allows

Read this section before P3's rotation work, because it says the verb
"set someone's password" is impossible as originally written.

The server holds `v = g^x mod N` where `x = H(s | H(I | ":" | P))`
(`srp.py:151-158`). It cannot recover `P`, cannot verify a candidate password
presented to it, and **cannot manufacture a verifier for a password it does not
have**. Only a party holding `P` can produce `(s, v)`.

Therefore, and stated so it does not read as a contradiction:

- ✅ A user who **knows** their password can change it (§5) — they can prove
  the old and produce a new verifier. This includes admins, for their own
  account.
- ✅ An **admin** can reset a **present-or-absent user's** password (§6) without
  knowing it — because the admin's browser *creates* the new secret and posts
  only its verifier. The admin therefore holds the OTP in cleartext for the
  length of that session; D-13 states the consequence.
- ❌ The one case with no in-portal answer is **the admin themself, who has
  forgotten their own password and has no co-admin**: nobody who holds the
  required secret is present. No artifact the appliance could issue fixes that,
  so it is §8's reinstall.

Earlier draft phrased the third bullet as "nobody can reset a password for a
user who is not present", which contradicted §6. The distinction was *who holds
the secret*, not whether a browser is open.

### D-4. Username is immutable

`x` binds the identity string `I` **inside** the hash:
`inner = H(identity || ":" || password)` (`srp.py:158`; the comment there —
"Binding the identity inside the inner hash is what stops one verifier being
reused across usernames"). So renaming a username **invalidates the stored
verifier**: the account would become unsignable, and only re-enrollment could
fix it.

Decision: **username is immutable, enforced in the write layer.** "Rename" in
P3's Users panel means **display name**, and the label must say so. Renaming a
household member is out of scope for M7; if it is ever wanted it is "create new
account, migrate, delete old", not an `UPDATE`.

Because `I` is hashed, the *normalisation* in D-33 (lower-casing) must happen
on **both** ends of every handshake and enrollment, identically, or `John` and
`john` derive different verifiers from the same password. See §10.

---

## 5. Changing your own password (applies to admins too)

Two-phase, **with no window in which the account has no password**.

### D-5. The flow

1. `POST /_lemonseed/api/srp/challenge {username}` → `{cid, salt, B}`.
2. Browser computes `A`, `M1` from the **current** password.
3. Browser derives `salt'`, `verifier'` locally from the **new** password
   (identical math to enrollment).
4. `POST /_lemonseed/api/srp/rotate {cid, A, M1, new_salt, new_verifier}`.
5. Server, in **one transaction**:
   - `finish_handshake(...)` — validates `M1` against the **old** verifier;
   - `UPDATE users SET srp_salt=%s, srp_verifier=%s,
     password_reset_required=FALSE, token_generation=token_generation+1
     WHERE username=%s AND srp_verifier=%s`.
     The `AND srp_verifier=%s` guard is what makes a concurrent second tab
     fail cleanly instead of clobbering.
   - commit, then issue the new session cookie.
6. Respond `{M2, target}`. `M2` derives from the **old** shared secret, so a
   client that validates it has proof the server knew the old password *and*
   (because it is only sent after commit) that the write landed.

### D-6. Rollback is structural, not a compensating action

Nothing is written before the old proof validates; the write is a single
statement in a single transaction. Any failure — wrong current password,
rejected `B % N == 0`, `u == 0`, a concurrent change, a dropped connection, a
Postgres error — leaves the **old `(salt, verifier)` intact**. There is no
delete-then-create step anywhere, so there is no state in which a user is
locked out with a null password. The user just retries.

This is a deliberate design requirement, not an incidental property. Do not
"improve" the flow by clearing the verifier first.

### D-7. Rotation bumps `token_generation`

Every *other* session for that account dies on its next request; the session
being used to rotate is re-issued. (M6.6's session store already honors the
counter: `auth.user_generation()` `:239-262` is re-checked by `verify_session`,
and `auth.revoke_sessions()` `:265-288` bumps it.)

**The revocation already redirects.** `gateway.nix:393-408` `@auth_unauthorized`
answers a `X-Requested-With: fetch` request with `401 {"lemonseed":
"session-expired"}` and every other request with `return 302
/login?rd=$request_uri`. So "does a bumped generation actually 302 the user to
the sign-in page?" is a property of the shipped gate, not new P3 work — P3 only
has to not bypass it.

### D-8. Password policy stays client-side

The server cannot police a value it never sees. The minimum-8 rule lives in
`stub.SRP_ENROLL_JS` and its rotation twin. Recorded in `DESIGN.md` §13 so the
missing server-side check is not read as an oversight.

### D-9. `crypto.getRandomValues` is available; `crypto.subtle` is not

On an insecure origin browsers withhold SubtleCrypto but **not**
`getRandomValues`. P2's client already relies on this and **fails closed**:
`stub.py:674-681` throws `no source of randomness available` rather than
falling back, and `stub.py:510-514` documents `crypto.subtle` being undefined on
the portal. It works on the HTTP portal, which is what makes browser-side OTP
generation (§6) possible on a plain-HTTP appliance.

**Do not add a server-side entropy fallback.** `getRandomValues` is the one
Web Crypto API that is not gated on secure context (Chrome 11 / Firefox 21
era), so the browsers that could need it do not exist here — and the fallback
would be worse than the problem: a server-generated OTP has to transit the
network to reach the admin, which breaks the one property D-10 exists to keep.

---

## 6. Admin resets another user's password

### D-10. Mechanism (**approved; this is what P3 builds**)

1. Admin's browser generates a one-time password locally with
   `crypto.getRandomValues`: **12 characters from an unambiguous alphabet**
   (`abcdefghjkmnpqrstuvwxyz23456789` — no `i`, `l`, `o`, `0`, `1`), displayed
   as `xxxxxx-xxxxxx`, shown once with a copy button. ~3.3e16 possibilities;
   satisfies the client min-8 policy on the raw value.
2. The browser **enrolls** that OTP: derives `(salt, verifier)` exactly as
   setup does.
3. `POST /_lemonseed/api/users/<name>/reset-password {salt, verifier}` →
   server writes both, sets `password_reset_required = TRUE`, bumps
   `token_generation` (the user's existing sessions die immediately).
4. The admin hands the OTP to the person out-of-band. **The OTP is never sent
   to the appliance** — only its verifier.

No expiry on the reset state (review: not required for M7). `password_reset_required`
already blocks sessions entirely (D-11), so an unrotated OTP is not a usable
account; the residual risk is an unrotated account sitting around, recorded as
accepted.

### D-11. Forced rotation happens at sign-in, and no session is issued first

`srp/verify` checks `password_reset_required`. If set, it validates `M1`,
returns `{M2, rotate_required: true}` and **no `Set-Cookie`**. The login page,
after checking `M2`, starts a fresh challenge and calls `srp/rotate` (§5) with
the old OTP and the new password. Only then is a cookie issued.

Why not issue a session and gate it? Because that creates a
half-authenticated state that every surface — the portal, the CP APIs, **and
Open WebUI** — would have to honor, and any surface that forgets becomes a way
to sit in an OTP account forever. Refusing the cookie is a smaller, stronger
rule: **an unrotated credential can never produce a session at all.** Cost is
one extra challenge round-trip on a page the user visits once.

The user's experience is one screen: sign in with the OTP → asked for a new
password twice → in.

### D-12. Lockout applies normally

Typing an OTP is a sign-in. The existing 5-failures / 5-minute window and
15-minute lockout (`auth.py:46-49`) apply; nothing about reset bypasses them.

### D-13. Honest consequence: an admin can impersonate any user (**accepted**)

The admin generated the OTP and therefore knows it. Until the user rotates, an
admin can sign in **as that user** and read their OWUI conversations, which are
per-user. Forced rotation bounds the duration but does not remove the
capability. This is inherent to "an admin may reset a password on a box with no
other recovery path" and holds for every mainstream system too — but it should
be said in the UI when the admin clicks Reset, and stated in `DESIGN.md`.

The alternative (drop admin reset entirely, delete-and-recreate on a forgotten
password) is **rejected**: it destroys the chat history that a household
actually loses. Earlier drafts numbered it D-14; that section is deleted and the
name is retired.

---

## 7. Identity keys, email, and the OWUI mirror

### D-15. The bug is real, reproduced in the field, and structurally caused

Editing the admin's email in OWUI's admin panel (a) logged the portal session's
OWUI frame out with an "account disabled" page and (b) on the next automatic
sign-in **created a second `admin@localhost` account**.

Mechanism, read from the pinned image rather than inferred: OWUI identity is
keyed on **email**; the CP sends `X-Forwarded-Email` at every trusted-header
sign-in; and in `signin` (`auths.py:743-757`) an email that matches no OWUI row
is **silently created** via `signup_handler(request, email, str(uuid.uuid4()),
name, …)`. Change the address on either side and the next sign-in mints a fresh
account. Our own reconcile loop is keyed by email too (`owui_policy.py:895-897`,
`:908-915`) and never deletes (`:954`), so the duplicate then lives forever.

### D-16′. Two separate identifiers, and never the same string

The review question — "email address != owui_id, right?" — is the crux, and the
answer shapes the whole section:

| Name | Value | Who reads it | Mutable? |
|---|---|---|---|
| `users.email` | the household's real address, RFC-validated (§10) | the portal only; future SMTP | yes |
| OWUI **identity email** | `owui_email(username)` = `{username}@localhost`, **always derived, never stored** | OWUI (`X-Forwarded-Email`, and its own `users.email` column) | no (username is immutable, D-4) |
| `users.owui_id` | OWUI's internal user id (a UUID), captured at creation | the CP, for `users.update` / `users.delete` | set once |

**D-16′. The OWUI identity email is `{username}@localhost` regardless of
`users.email`.** Concretely, `owui_identities()` stops reading `row["email"]`
(`auth.py:617`) and always emits `owui_email(username)`; `create_session` stops
copying `user["email"]` into the session record and stores the derived identity
instead (`auth.py:308`). This kills F-1 dead and makes the duplicate-account
class of bug structurally impossible: the identity string is a pure function of
an immutable username, so nothing a user or an OWUI admin edits can move it.

What we give up, stated plainly: **OWUI always shows `alice@localhost`** in its
user list and profile, not the household address. That is acceptable today
because nothing on the appliance sends mail. It is also forward-compatible —
when SMTP arrives, `users.email` is already the real address and OWUI keeps its
synthetic one. This is why the domain stays `@localhost` (D-19) even though
`users.email` is now a real address that must satisfy real rules.

**`users.owui_id`** is still worth having even though the identity email can no
longer drift: it is the handle `user_update`/`user_delete` need, it lets
`UsersHandler` key on something the CP controls instead of string-matching
emails, and the partial unique index (§2) makes "two CP rows, one OWUI account"
a constraint violation instead of a silent bug. **No adoption pass** (D-31):
`users.create` calls `POST /api/v1/auths/add`, which returns the created `id`
(`models/auths.py:1128-1136`), and stores it in the same transaction. If OWUI is
unreachable at create time, `owui_id` stays `NULL` and the reconcile loop
*creates* the account (it cannot update a row that does not exist) and fills the
id then.

**D-16a. Identity reuse is impossible.** Review: *"guard against new accounts
being created under old IDs, as they could get access to a prior user's OWUI
data."* Two rules:

1. **Deleting a CP account deletes the OWUI account** (§10 D-23). Not deferred
   to the reconcile loop, which is forbidden from deleting.
2. **Never adopt an orphan.** If OWUI already has a row at
   `{username}@localhost` when the CP creates an account with that username — a
   leftover, or something an OWUI admin made by hand — the CP **deletes it and
   creates its own**. Adopting it would hand the new person the old person's
   conversations, which is exactly the failure being guarded against.

One hazard the CP cannot engineer around: OWUI refuses to delete **its first
user** (`users.py:990-1000`). Whether that is the CP service account or the
first portal admin depends on creation order and must be **determined
empirically in P3**, not assumed. Where the delete is refused, the portal must
surface the refusal and keep the CP row, not report success.

### D-17. Which OWUI routes can change identity (enumerated, not guessed)

This was the one open question that could not be answered from our own tree. It
is now answered by reading the pinned image: the route tables and form models
were extracted out of `open-webui:v0.11.0` (the exact digest in
`container-images.json`, `72c0ba64…`) from its layers in the Nix store. Four
findings, three of which change the design.

1. **There is no self-service email edit.** The account-settings route is
   `POST /api/v1/auths/update/profile`, and its `UpdateProfileForm` is
   `{profile_image_url, name, bio, gender, date_of_birth}` — **no email**
   (`models/users.py:174`). The other self-service write,
   `POST /api/v1/users/user/info/update`, PATCH-merges into the `info` JSON
   blob, not the email column. **The only route that can change an email is the
   admin-only `POST /api/v1/users/{user_id}/update`**, which requires
   `get_admin_user`, checks `EMAIL_TAKEN`, and calls
   `Auths.update_email_by_id`. So the door you walked through really was the
   admin panel, and a single guard covers it.
2. **OWUI's own password change is already inert on this appliance.**
   `auths.update_password` raises `400 ACTION_PROHIBITED` whenever
   `WEBUI_AUTH_TRUSTED_EMAIL_HEADER` is set (`auths.py:391`), which is how we
   run. No guard is needed for it, and there is no OWUI-side password for us to
   keep in sync. Note `users.update` *does* still accept a `password` field and
   would bcrypt-hash it — our mirror must never send one.
3. **Group and role headers are genuinely the sync channel.** Trusted-header
   `signin` calls `Groups.sync_groups_by_group_names()` from the groups header
   (`auths.py:764`) and `Users.update_user_role_by_id()` from the role header
   (`auths.py:770`) on **every** sign-in. P4 assumed this mechanism exists; it
   does, and P4 needs no new API calls for group membership.
4. **The duplicate-account mechanism is now proven, not inferred.** In
   `signin` (`auths.py:743-757`): if no OWUI user matches the email header,
   OWUI calls `signup_handler(request, email, str(uuid.uuid4()), name, …)` — it
   silently **creates** the account with a random password — then authenticates
   by email. This is the whole bug in four lines.

**D-17b. OWUI's user panel becomes read-only in M7** (review decision). The
guard set is therefore:

- `POST /api/v1/users/[^/]+/update` — **enforce**, replacing today's narrower
  `%CP_OWUI_ID%`-only rule. This closes OWUI-side email edits, role edits, and
  password sets in one rule.

  The id segment is matched with `[^/]+`, **not** `.*`, and the reason is a real
  regression caught in review: these guards compile to an nginx regex against
  `$request`, and `.*` is greedy across `/`, so `users/.*/update` also swallows
  `POST /api/v1/users/user/settings/update`, `.../user/status/update` and
  `.../user/info/update` — OWUI's self-service writes, which D-17a explicitly
  says stay with OWUI. A greedy rule would silently take away members' ability to
  save their own avatar, bio, timezone and status, for no coverage gained:
  every real user id is a slash-free UUID, so `[^/]+` blocks exactly the same set
  of admin writes.
- `DELETE /api/v1/users/.*` — already declared, currently `advertise`; **flip
  to `enforce`** so OWUI's delete button stops being a lie. Its own delete of
  an account would orphan the CP row and could later be "adopted" by a new
  account, which is D-16a's forbidden case.

Both stay in `owui-policy.json`, whose guards apply to browser traffic only:
the CP calls `127.0.0.1:{port}` directly and never traverses nginx (§1), and
its API key's `allowed_endpoints` are derived from the api map, which already
includes `users.update` (`derive_allowed_endpoints`, `owui_policy.py:621`).
That is why flipping these to `enforce` does not break CP reconciliation.

Deliberately **not** guarded: `POST /api/v1/auths/update/profile`. It cannot
touch email, role, or password, and blocking it would deny users their own
avatar and bio. It *can* change OWUI's copy of the display **name**, which is
why the next section states the rule that name is cosmetic.

### D-17a. The guarded set is identity and authz, not everything

The rule, stated once so the manifest stays coherent: **guard attributes that
select an identity or a privilege; let cosmetic ones drift.**

| Attribute | Guarded in OWUI | How it converges |
|---|---|---|
| OWUI identity email | yes (`users/[^/]+/update`) | Derived from an immutable username; the CP never mirrors `users.email` into it |
| role | yes (same rule) | Header re-asserts it at every sign-in |
| group membership | n/a (no such write used) | Header re-syncs at every sign-in |
| display name | no | Mirror pushes on a CP-side edit; otherwise drifts |
| avatar, bio, timezone, settings | no | OWUI owns them |

Display name drifts by design, and that is safe: the trusted name header is
read **only when OWUI creates the account** (`auths.py:734-741`), so name was
already one-way before M7. Nothing selects a user by name, so a stale copy in
OWUI is a cosmetic wrinkle, not a security hole. P3 pushes `name` on a CP-side
display-name change; we do not add a reconcile handler for it.

### D-18. The mirror is reconciliation, not a push

Review: *"with new (never logged in) users, OWUI will not have a record in the
users table for them, so any UPDATE attempt will probably fail … we are
enforcing our intent on an ongoing basis rather than a one-shot."* Right, and
`UsersHandler` is already built that way — `observe()` / `desired()` /
`changes()` / `apply()` on a periodic tick. Keep it, with three changes:

- **Key by `owui_id`** where present, by identity email otherwise
  (`owui_policy.py:895-897`, `:908-915`). `observe()` must retain OWUI's `id`
  (it reads `item["id"]` only to filter protected accounts today).
- `USER_SCOPES` (`:56`) gains `name`; `changes()` (`:918-927`) gains the diff.
  Email is **not** a managed attribute — the OWUI row's email is created once
  and never written again (D-16′).
- **`desired[].email` is `owui_email(username)`**, so the hard ordering hazard
  disappears: making email a *managed* attribute while email was still the join
  key would have made the loop see a missing desired row and mint a duplicate.
  It is not managed, and the key is no longer email.

Ordering for portal actions, because it still matters for *errors*: **write the
CP row first, then push to OWUI.** A failed OWUI push is then cosmetic drift —
visible in OWUI, corrected at the next reconcile — and cannot duplicate an
account. Do not retry in a loop; record the failure, write it to `audit_log`
(§11), and let the reconcile tick surface it, matching how every other managed
entity reports drift.

### D-19. Keep `@localhost`; a real address is not wanted here

The proposal to switch the OWUI identity to `{user}@lemonseed` **does not
work**, and the tree already says why (`auth.py:53-58`): OWUI's format check
"requires a dot in the domain but explicitly exempts `@localhost`. That rules
out `@lemonseed` (rejected by `POST /api/v1/auths/add`, so OWUI's own admin UI
could never create or edit such a row)". A bare `@lemonseed` identity cannot
even be written by OWUI. `@localhost` is exempted, it is what every existing row
already holds, and the hostname-derived alternative was already rejected there
because the box renames itself at the mDNS handoff — and OWUI keys identity on
that string, so a change would duplicate every account.

Note this decision is now only about the **synthetic** identity (D-16′). The
user-visible `users.email` is a different field with different rules (§10).

### D-32. `usermgmt` owns the primitives; hooks are how other modules join

Review: *"some of these user primitives (create, delete, etc) may also be
required by future modules; if there's an easy way to extend our mechanism …
not a hard requirement right now."* There is a cheap version, so we take it.

`control-plane/usermgmt.py` owns every account primitive: `create`,
`set_display_name`, `set_email`, `rotate_password`, `admin_reset_password`,
`set_disabled`, `set_groups`, `delete`. Each one: validate → guard (D-2) →
CP write in one transaction → OWUI write → `audit_log` row → notify. No route
handler writes to `users` directly.

After the transaction commits, `usermgmt` calls `notify(event, payload)` on a
**list of registered callables** (`register_observer(fn)`), events being
`user.created`, `user.renamed`, `user.email_changed`,
`user.password_rotated`, `user.password_reset`, `user.disabled`,
`user.enabled`, `user.deleted`, `group.*`. Handlers are called outside the
transaction; a handler that raises is logged and skipped, never rolled back
into the user's operation. That is the whole extension point — no plugin
framework, no discovery, no config. OWUI sync is the first real consumer and
stays where it is (the reconcile loop is pull-based; the observers are the
"do it now" path), and the same registry is the natural home for the D-2 write
guards' cross-module notices later.

---

## 8. Forgotten admin password

### D-20. Reinstall with the "Reset admin credentials" marker

A forgotten admin password is recovered by USB reinstall with
**preserve user data** + **Reset admin credentials** checked. This is exactly
what that checkbox was built for (`installer/server.py:174-177` is the UI row;
`:88` forces it off unless user data is being preserved). The marker clears the
`admin` group so `/setup` reopens, and `/setup` performs client-side SRP
enrollment for the new admin.

Verified machinery that already works, once the marker actually arrives:
`auth.RESET_MARKER_FILE` (`auth.py:37`), `reset_marker_present()` (`:513-515`),
`admin_bootstrap_required()` returning `True` while it exists (`:505-511`), and
`create_admin` demoting every former admin — `DELETE FROM user_groups WHERE
name = %s` (`auth.py:746-748`), other groups preserved — then unlinking the
marker (`:791-792`). Tested at `test_auth.py:880-888`. **None of it fires today
because of F-2.**

No in-portal recovery path exists or should be built: any mechanism that can
set a verifier without the old password is, on a machine with no TLS and no
out-of-band channel, reachable by anything on the LAN. **Physical access
remains the root of trust** — and `DESIGN.md` §9 already notes root has an
empty password on bring-up builds, so this is not a stronger lock than the rest
of the box. Review confirmed D-20 as the mechanism and explicitly rejected the
alternative that was floated for it (that option is retired; the number is not
reused).

### D-27. Fix the marker handoff (F-2): put the marker in user-data

The ESP is the wrong place for it. It is a FAT partition that is not mounted on
a running appliance (both `scripts/lemonseed-ab.sh:81` and
`scripts/amdgpu-kernel-params.sh:33` mount it at `/run/lemonseed-esp`
themselves, on demand), so consuming it needs a new mount-and-relocate unit —
new root surface, new boot ordering, for a file that has one reader.

Instead: **the installer writes the marker straight into user-data**, where the
consumer already looks. With preserve mode the installer has the target's
user-data partition in hand and already mounts it at
`/run/lemonseed-install-user-data` (`install.py:606-628`), and `/var` is the
btrfs subvolume `@var` of that same filesystem (`storage.nix:126-141`,
`scripts/appliance-storage-provision.sh:23`), so the destination is
`/run/lemonseed-install-user-data/@var/lib/lemonseed/reset-admin`.

Rules:

- Write it when the flag is set; **delete a stale one when it is not**, so a
  preserve-mode reinstall always states the desired state explicitly.
- **Not best-effort.** The log copy beside it is wrapped in a best-effort
  `try`; the marker must not be, because silently failing it means the user
  believes they reset the admin password and did not. Raise on failure and show
  it in the installer UI.
- Delete the ESP write (`install.py:498-503`) and the two comments naming the
  phantom `lemonseed-auth-reset-seed` unit (`install.py:499-501`,
  `auth.py:36`). Fixing F-2 by inventing that unit is the more complicated
  route to the same outcome.

### D-28. Tell the admin how many accounts the box will reset

Review: with more than one admin, the checkbox must say so and may list the
usernames. The live installer cannot query Postgres, so the CP publishes a
tiny roster it can read:

- **CP side:** whenever admin-group membership changes (create, delete,
  promote, demote, reset-marker consumption), write
  `/var/lib/lemonseed/admins.json` — `{"admins": ["alice", "bob"]}`. Same code
  path as the audit write, so it cannot go stale in practice; if the write
  fails, log it and move on (it is an affordance, not a control).
- **Installer side:** when a disk is selected with preserved user data, mount
  it read-only, read `@var/lib/lemonseed/admins.json`, and return the list with
  the disk details (the same call that already reports `preservable_user_data`,
  `install.py:378`).
- **UI:** with 2+ admins, the checkbox row reads *"Reset admin credentials —
  removes admin access from N accounts: alice, bob. Their chats and files are
  kept."*; with 1, the current single-admin wording; with no roster file (a
  pre-P3 box), the generic sentence with no names.

### D-20a. No straggler admin row may keep a working verifier

`create_admin` upserts by `username`, so a reset that enrolls a *different*
username leaves the old admin row alive as an ordinary user **with a working
SRP verifier** — a former admin, still able to sign in, whose OWUI account and
chat history are untouched. Review: the row must be **overwritten if possible,
and must not survive as a straggler**. So when the reset marker is consumed:

1. Same username as an existing row → the upsert already overwrites it. Good.
2. Different username → every row that was in `admin` when the marker was
   consumed is **deleted** (CP row + its OWUI account, D-16a), not demoted.

The delete happens in the same transaction as the demote, and each OWUI delete
goes through the D-16a refusal path (if OWUI refuses, the CP row is deleted
anyway — a portal user who can no longer sign in as admin must not be able to
come back — and the refusal is audited and surfaced as drift). This is the one
place where a destructive delete is *caused* by the CP without an operator
clicking Delete, so: the installer UI warning (D-28) is the consent step, and
every deletion is an `audit_log` row.

### D-21. Rollback across the M7 boundary does not work (accepted)

A pre-M7 slot has no bcrypt hash (the verifier check was deleted) and cannot be
given a verifier, so **A/B rollback across the M7 boundary cannot sign anyone
in**, and after §2's baseline rewrite an older slot would also be missing
columns that no longer exist in its own DDL but do in the database. Review
decision: **assume pre-M7 is dead**; until launch, "reinstall required" is the
approved answer to a breaking change. Tracked in `DESIGN.md` §11. Additive-only
still applies *forward* (v5 → v6), which is what keeps a normal M7→M7 update
readable by the previous slot.

---

## 9. Reserved identities

**D-22.** Two identities are not deletable or editable from the portal:

| Identity | Rules |
|---|---|
| the **first admin** account | Username, OWUI identity, SRP columns immutable. Display name editable. Cannot be disabled or deleted while it is the last admin (D-2.2) — and in practice treat it as un-deletable outright. |
| `lemonseed-cp` (service account) | Not a portal user at all. Already `never_touch: ["%SERVICE_ACCOUNT_ID%"]` in the manifest, filtered out of `observe()`, and protected by the two `%CP_OWUI_ID%` guards. Portal must list it as a system identity or not list it — **not** as an editable row. |

**The first admin's username is arbitrary** (review). Verified: nothing
downstream keys on the literal string. `auth.ADMIN_GROUP = "admin"` is a
*group* name; `_require_admin` (`stub.py:2292-2296`) tests group membership;
the only place `admin` appears as a username is the setup form's `value="admin"`
default (`stub.py:985-993`). Two things must change so the assumption stays
false: reserved-identity logic must identify the first admin **by row** (the row
`create_admin` wrote), never by name, and the QEMU smoke assertion
`email != "admin@localhost"` (`scripts/qemu-api-smoke-test.sh:715-717`) must
derive the expected identity from the username the test actually enrolls.

The read-only rule lives in the write layer (D-2's discipline), and the UI omits
the control rather than rendering it disabled.

---

## 10. Field rules, validation, and the delete flow

**D-33. Username.** Replaces `auth._USERNAME_RE` (`auth.py:51`,
`[a-z0-9][a-z0-9_.-]{0,63}`, today applied only by `create_admin`):

```
[a-z][a-z0-9_.-]{1,15}
```

- **First character a letter**, total length **2-16**. Short names are what
  shell, `ps`, log lines and PAM handle well, and the ceiling keeps the derived
  OWUI identity comfortably inside any length check downstream.
- Then **reject `.` and `..` outright**, and reject a trailing `.` or `-` —
  both pass the regex above and both are hostile to POSIX-conforming names.
- **Every username input is lower-cased before it is matched** (setup form,
  sign-in form, rotation, admin reset, portal user creation, the API layer).
  `John` and `john` are the same account and either may type either spelling;
  the stored and hashed value is always `john`. Because `I` is inside the SRP
  hash (D-4), normalisation must be applied identically by the client and the
  server on *every* path — a path that forgets produces an account that cannot
  sign in, so this belongs in one shared helper on each side, not inline.
- **Reserved patterns**, blocked at creation with a message that says the name
  is reserved rather than invalid. Rationale: the appliance may later run tasks
  as UIDs or expose PAM, and a portal account squatting a system name is a
  migration we do not want to have to do. Patterns: `root`, `daemon`, `bin`,
  `sys`, `sync`, `games`, `man`, `lp`, `mail`, `news`, `uucp`, `proxy`,
  `www-data`, `nobody`, `nscd`, `nginx`, `messagebus`, `syslog`, `dbus`,
  `polkitd`, `postgres`, `valkey`, `redis`, `dhcp*`, `systemd*`, `_systemd*`,
  and `lemonseed*` — the last pattern also covers the two identities this
  appliance really creates (`lemonseed-cp`, the OWUI service account, and
  `lemonseed-lemond`, a real host user — `lemond.nix:249`).
  `admin` itself is **not** reserved: it is the documented default username, and
  D-22 protects that *row* by identity rather than by name. Managed list in
  code (`RESERVED_USERNAMES`), extended by review; QEMU asserts `root`,
  `systemd-network`, `lemonseed-cp`, `.` and `..` are all refused.

**D-25. Field set.** `username` immutable (D-4), `display_name` editable,
`email` editable. `create_admin` currently writes the username into
`display_name` (`auth.py:770`); P3 stops and writes the real name from the setup
form.

**D-34. Email validation** = **RFC 5322 addr-spec shape ∩ OWUI's own validator**.
The appliance must not accept an address it could never hand to a mail
transport, and must not accept one OWUI would reject if the two ever share a
record. So:

- Local part: RFC 5322 **dot-atom** text only — runs of ASCII `A-Z a-z 0-9`
  plus the specials `!#$%&'*+/=?^_` and backtick `` ` `` and braces/tilde,
  separated by single dots, with no leading, trailing or doubled dot. **No
  quoted strings, no comments, no non-ASCII** (an address we cannot type into a
  transport is not worth its quoting rules).
- Domain: labels of `[A-Za-z0-9-]`, hyphen not first or last, dot-separated,
  and — because that is OWUI's rule (`auth.py:53-58`) — **must contain a dot**,
  with the single exemption `localhost`.
- Total length ≤ 254, local part ≤ 64. Case-insensitive: stored lower-cased.
- `display_name`: required non-empty after stripping, ≤ 80 characters, no
  control characters. It is forwarded to OWUI's name header, so keep it out of
  anything header-hostile (no CR/LF, no NUL).

Note that D-16′ means these rules never apply to the OWUI identity string,
which is generated from a username that is already RFC-5321 dot-atom-clean by
construction (`[a-z][a-z0-9_.-]{1,15}` ⊂ dot-atom-text), so
`{username}@localhost` is valid without any validation step.

**D-23. Delete flow.** Immediate and data-destroying, with a **typed
confirmation** (the review's call): the portal requires typing the username, as
it already does for the wipe-everything action. `owui_client.user_delete`
(`owui_client.py:380-383`) gains its first call site here — its docstring
already says the reconcile loop must never call it, and the reconcile loop
remains forbidden. Delete order: OWUI delete → CP row (`user_groups` joins go
via `ON DELETE CASCADE`); if OWUI refuses (D-16a's first-user rule) the portal
shows the refusal and the CP row stays. The tombstone / 7-day-undelete idea
stays in the backlog with its jurisdictional note. Enable/disable and
display-name/email edits need no confirmation.

**D-24. Identity contract for modules.** Modules receive the **minimum** set,
which is exactly what the gate already forwards: `X-Forwarded-User`, `-Email`,
`-Name`, `-Groups`, `-Role` (`gateway.nix:133-136`). Stated as a named contract
so a future module definition can *declare* which headers it needs instead of
inventing new ones. It does not grow to expose our full user model. **`-Email`
here is the synthetic OWUI identity** (D-16′), not `users.email` — a module must
not receive a household address it has no need for.

**D-26. M7 non-goals**, all of which go to the `DESIGN.md` §11 backlog unless
already there: per-user quotas, per-user model allowlists beyond the one
content-filter boolean, directory auth (LDAP/OAuth/PAM), TOTP enrollment,
profile pictures, birth date / gender fields, attributes P4 does not ship,
self-service *sign-up* (admins create accounts, full stop), real SMTP, MFA,
per-user disk quotas, and audit export.

---

## 11. Audit log

**D-35. Where it lives: PostgreSQL, with a journal copy.** Review asked me to
weigh journald, whose attraction is free time-based deletion.

| | journald | PostgreSQL |
|---|---|---|
| retention | free, machine-global (`SystemMaxUse`, `MaxFileAge`) | our own sweep, our own window |
| structure | text + `KEY=value` fields; no joins | rows, joins on username/group, `JSONB` detail |
| reads | `journalctl`; needs root or a journal group on NixOS | the CP's existing psycopg2 path; portal-readable |
| survives | the journal is per-install and gets trimmed by volume, not by meaning | user-data, same as the accounts it describes |
| already used for | operational logs, mixed with everything else | CP state |

The deciding factor is the *consumer*: the portal must show "who did what to
this account, when", filterable per user and joinable against accounts. That is
a query, not a log tail. And the retention argument for journald is weaker than
it looks — `Storage=auto` with a persistent `/var` (`storage.nix:126-141`) means
journald trims by *disk usage*, so an audit trail's lifetime would depend on how
noisy nginx is. **Decision: `audit_log` in Postgres** (§2), **plus one
`log.info("lemonseed-audit …")` line per event** — ~2 lines, and it buys an
independent copy that survives a wiped database and is readable from a root
shell with no CP. Not the record of truth; that stays the table.

**D-36. Retention: 365 days, swept in application SQL.** There is no precedent
for SQL retention in the tree (only the in-memory `srp.PendingStore._prune`), so
this is new: `DELETE FROM audit_log WHERE at < NOW() - INTERVAL '365 days'` on
the **existing** slow policy tick (`modules.py:660-671`,
`POLICY_AUDIT_INTERVAL_SEC = 300`, `:549-554`). A runtime `DELETE` is
application SQL, not migration DDL, so `test_only_additive_migration_sql` stays
green — the ban is on migration statements. Sweeping every 5 minutes is
absurdly generous for a daily concern; it is chosen because the tick exists and
already owns background reconciliation, not because the interval matters.

**D-37. What gets logged.** `actor` is the acting session's username (`system`
for the reset marker and other unauthenticated-but-privileged paths, `cp` for
CP-initiated work like reconcile-created accounts), `target` the affected
username or group, `action` a short verb (`user.create`, `user.delete`,
`user.rename`, `user.email_changed`, `user.password_reset`,
`user.password_rotated`, `user.disabled`, `user.enabled`, `group.create`,
`group.delete`, `group.member_add`, `group.member_remove`,
`group.attribute_set`, `model.changed` for the admin-class model edits PLAN.md
mentions), and `detail` a `JSONB` blob of *names of things changed*, never
values. **Reuse the existing secret masking** (`owui_policy.py:967-1000`:
`MASK`, `_SECRET_LEAVES`, `is_secret_key`, `_mask_nested`) rather than inventing
a second policy — the failure mode of an audit log is faithfully recording the
secret someone pasted into a field. Never write a password, verifier, salt, OTP,
token, or API key, and note that `detail` for `user.email_changed` records only
that the email changed, not the previous address.

Cost, honestly: **about half a day** — table + writes in each `usermgmt`
primitive + the sweep + tests. It is cheap because the write layer is already
consolidated (D-32); the reason it was ever expensive is that audit logging
scattered across route handlers is how you get an audit log with holes in it.

---

## 12. Open questions

**None.** Every item this document carried as open is decided:

- Was D-14 (drop admin reset) needed? **No — D-10 approved, D-13's impersonation
  window accepted, D-14 retired** (§6).
- What happens to the old admin row on a reset-marker install? **Deleted, with
  its OWUI account, if the username differs; overwritten if it matches** —
  D-20a, and D-28 warns before it happens.
- Does `password_reset_required` expire? **No**, not for M7 (§6); D-11 already
  blocks sessions so an unrotated OTP is not a usable account.
- OWUI self-service email edit? **Does not exist** in the pinned image (§7,
  D-17), and OWUI's user panel goes read-only anyway (D-17b).
- Duplicate `admin@localhost` on the test box? **Reinstall; write no code for
  it** (D-31).

Two things left to *discover* rather than decide, both small and both P3 tasks,
not design questions: which OWUI account is "first" for the delete refusal
(D-16a), and whether OWUI's read-only user panel turns out to break anything
after D-17b is enforced — which is the reason the review wanted the flip done
now rather than deferred.

---

## 13. What this changes in P3 and P4

**Schema groundwork (P3 first act).** Baseline rewrite + `BASELINE = 5`,
`SCHEMA_VERSION = 6`, one `_migrate_v6`, `_migrate_v4`/`_migrate_v5` deleted,
`migrate_cp_schema` raises on a sub-baseline stamp (§2); `test_db.py`'s
`test_upgrade_from_baseline_adds_only_the_new_columns` and
`test_fresh_database_reaches_current_version` updated to the new exact DDL.
Delete the leftover `password_hash` references: `auth._load_users`' `SELECT`
(`auth.py:521-524`) and `create_admin`'s writes of `''` (`auth.py:750-760`).

**P3 gains:** `srp/rotate` (§5) and the `rotate_required` branch in `verify`
(D-11); the admin reset route (D-10); `usermgmt.py` + observers (D-32);
`audit_log` + retention sweep (§11); the username/email validators and
lower-casing (D-33/D-34); `owui_id` at create + `usermgmt` delete (D-16′/D-16a);
identity email decoupled from `users.email` (F-1); the `users.last_login`
write; D-27/D-28/D-20a for the recovery path; D-22's by-row first-admin
identification and the parameterized smoke-test identity.

**P3's `set_password` is deleted and replaced** by `rotate` (self) and
`reset-password` (admin), because a single "set password" verb presumes a server
that can hold a password.

**P3 also ships the UI this record assumed would follow the backend**: the
**People** panel in Settings (accounts, groups, and the audit log on demand) and
the self-service **My Profile** page at `/profile/`, reached from an account
popover that every signed-in page carries and that hides the gear for
non-admins. The two password-change screens share one `stub.ROTATE_JS`, so the
panel and the page cannot grow two rotation sequences. One correction found by
writing the QEMU scenario: `create` must set `password_reset_required`, because
the admin's browser derived that verifier and therefore knows the credential
(D-13). Setup's `create_admin` is the opposite case and stays `FALSE`.

**P3 does *not* gain** an email mirror to OWUI, an `owui_id` adoption pass, a
gate rule for forced rotation (D-11 removes the need), or any pre-M7 repair
code.

**P3 also flips three OWUI guards to `enforce`** (D-17b): `users/[^/]+/update`
widened from `%CP_OWUI_ID%`-only to all users, `users/.*` delete, and — once the
People panel existed — `POST /api/v1/auths/add` too. Nothing in OWUI's user panel
can write, which was the point of D-17b.

**P4 gains** the `groups.system` flag and `group_attributes` from the same v6
migration, deletes the six `groups.*` api-map entries (decided, PLAN.md Q4),
and gets the D-2 guards and D-35 audit actions.

**Tests to add** (P3): rotation with a wrong current password leaves the old
verifier byte-identical; rotation of an account whose `srp_verifier` changed
mid-flight fails via the `WHERE … AND srp_verifier=%s` guard; `verify` on a
flagged account returns `rotate_required` and **no `Set-Cookie`**; an unrotated
OTP account cannot reach any gated route; admin reset writes a verifier, sets
the flag and bumps `token_generation`; **the OTP does not appear in the request
body, the log, or the response**; username immutability; username
normalisation (`John` signs in as `john`) **and** that a mixed-case enrollment
still verifies; reserved and over-long usernames refused; email addresses
accepted/rejected against the D-34 list; `owui_id` captured at create; a
`user_delete` refusal from OWUI does not report success; deleting a user calls
`user_delete` exactly once and the reconcile loop never does; the reset-marker
path removes a differently-named former admin and its OWUI row; `admins.json`
roster written on membership change; an audit row written per primitive with no
secret values in it; the retention sweep deletes old rows only; sub-baseline
schema stamp raises.

**QEMU (built, in `scripts/qemu-api-smoke-test.sh` under the M7 P3/P4 banner):**
the admin creates a member from a browser-derived verifier → signs in with the
one-time code → asserts **no cookie was issued** → rotates → asserts the cookie
→ signs in again with the chosen password → asserts OWUI signed that person in
as `{username}@localhost` while `users.email` holds something else entirely.
Group create, JSONB attribute validation, membership, the member/admin gating
split, the reserved-name and case-folding guards, delete (including that the
deleted account's held cookie stops working), the audit actors, and both delete
guards — self-action, and one admin refusing to remove the first-admin row — are
all asserted over the real gateway in the same pass.

**BM-1 (next bare-metal cycle, batched with the deferred login-text check):**
the same flow by hand in a browser, including the password-change-cancel path
(change your own password, then make it fail — confirm you can still sign in
with the old one), and OWUI's now-read-only user panel.

---

## 14. Decision log

| ID | Decision | Why |
|---|---|---|
| D-1 | authz from system groups only | Custom groups must never change authz level; enforced where the role is computed so it cannot drift. |
| D-2 | 3 write-layer guards; `admin_exists() → None` = refuse | UI-only guards are bypassable; the self-action guard kills the two-tab race. |
| D-3 | `users` membership is a stored row; attribute keys declared in code | Joins are the read path; a declared key→type registry is what makes JSONB safe to widen. |
| D-4 | **Username immutable** | `x` binds the identity (`srp.py:158`), so a rename invalidates the verifier. |
| D-5–D-8 | Self-service rotation; single transaction; verify-then-write | No null-password window; rollback is structural, not compensating. |
| D-9 | `getRandomValues` available, `subtle` is not; no server-entropy fallback | Non-obvious; the reset design leans on it. A server-generated OTP would have to cross the wire. |
| D-10 | Admin reset = browser-generated OTP + verifier posted | OTP never transits; only its verifier. |
| D-11 | **No session until rotation completes** | A gate-flag approach needs every surface (incl. OWUI) to honor it; refusing the cookie is one rule that cannot be forgotten. |
| D-12 | Lockout applies to OTP sign-ins | Otherwise reset is a lockout bypass. |
| D-13 | Accept the admin-impersonation window | Inherent to reset-without-recovery; bounded by forced rotation; disclosed in UI + DESIGN. |
| D-15/D-16′ | Three identifiers: `users.email`, derived `{username}@localhost`, `users.owui_id` | Email-keyed identity is the verified root cause of the duplicate admin; deriving the OWUI identity from an immutable username makes the class impossible. |
| D-16a | Delete the OWUI account with the CP account; never adopt an orphan | Otherwise a new account inherits a previous user's OWUI data. |
| D-17 | Guard `users/[^/]+/update`; enumerate routes from the pinned image | No self-service email edit exists, and OWUI's own password change is already refused under trusted-header auth. |
| D-17a | Guard identity and authz, let cosmetics drift | Keeps the manifest small and honest; name was already one-way because the name header is read only at account creation. |
| D-17b | OWUI user panel read-only in M7 | Review: flip it now to find breakage early rather than at launch. |
| D-18 | Mirror is reconciliation, keyed by `owui_id`; CP row first, OWUI push second | Never-logged-in users have no OWUI row, so one-shot pushes fail; with `owui_id` keying a failed push is drift, not duplication. |
| D-19 | Keep `@localhost` for the OWUI identity | OWUI requires a dot in the domain and only exempts `@localhost`; `@lemonseed` is unwriteable. |
| D-20 | Forgotten admin password = reinstall + marker | Any no-password recovery path is LAN-reachable; physical access is already the root of trust. |
| D-20a | Reset deletes differently-named former admins | An upsert keyed by username otherwise leaves a former admin with a working verifier. |
| D-21 | Rollback across M7 is dead; reinstall is the answer | No install base; the alternative is carrying pre-M7 compatibility code forever. |
| D-22 | Reserved identities identified by row, not name | The first admin's username is arbitrary (`admin` is only the default); verified nothing keys on the string. |
| D-23 | Delete is immediate, typed-confirm, and OWUI-deletes too | Data-destroying by design; also the only way to prevent identity reuse. |
| D-24 | Minimum identity headers to modules; `-Email` is the synthetic identity | Modules get what the gate already forwards, and no household address they don't need. |
| D-25 | Field set: `username` immutable, `display_name`/`email` editable | Setup form stops faking a display name out of the username. |
| D-26 | M7 non-goals → backlog | Sign-up, SMTP, MFA, quotas, directory auth, audit export are all out. |
| D-27 | Reset marker goes in user-data, written by the installer; ESP route deleted | F-2: the named relocation unit never existed. User-data is where the consumer already looks, and the installer already has it mounted. |
| D-28 | CP publishes `admins.json`; installer names the accounts the box will reset | A checkbox that silently resets N admins is not informed consent. |
| D-29 | Baseline rewrite; `password_hash` leaves the schema; BASELINE = 5 → SCHEMA = 6 | The additive-only rule binds migrations, not the DDL we create; with no pre-M7 base, writing the baseline honestly is cheaper than a vestigial column. |
| D-30 | `group_attributes.value` is JSONB | Real booleans and lists; new types need no DDL under additive-only. |
| D-31 | No backfill, no pre-M7 repair, ever | Review: backfill logic is long-term debt for two test boxes that will be reinstalled. `owui_id` is captured at create instead. |
| D-32 | `usermgmt.py` owns the primitives + `notify(event, payload)` observers | One write path for guards and audit; extension point for future modules without a plugin framework. |
| D-33 | Username `[a-z][a-z0-9_.-]{1,15}`, lower-cased everywhere, reserved-name patterns | POSIX-friendly, ≤16 chars, case-insensitive login, and no squatting on names we may need for PAM/UIDs later. |
| D-34 | Email = RFC 5322 dot-atom ∩ OWUI's dot-in-domain rule | Accept nothing we could not hand a transport or that OWUI would refuse. |
| D-35 | Audit in Postgres, with a one-line journal copy | The consumer is a query, not a log tail; journald trims by volume, not by meaning. |
| D-36 | 365-day retention, swept on the existing 300 s policy tick | Table + `DELETE` in application SQL keeps the additive-migration test green. |
| D-37 | Actor/target/action + masked JSONB detail | Reuses the OWUI secret masking; an audit log that records secrets is worse than none. |
| D-38 | The settings rail lives **inside** `/settings/content/`, not in the portal shell | OWUI already owns an auto-hiding left rail; a second beside it is unusable. The shell keeps its top bar for portal nav. |
| D-39 | **One dialog component** (`stub.DIALOG_JS`) for every question; native `prompt`/`confirm`/`alert` are gone | Native boxes block the iframe's event loop, carry one line of copy, and cannot disable their own OK. Escape / outside click resolve the `dismiss` value, never the default. |
| D-40 | A refusal the panel can predict is a **disabled** button with the reason in its `title` | Review: buttons the write layer refuses read as a broken UI. The server still refuses; the panel only says so first. |
| D-41 | Power = reboot + shutdown, admin-only, **409 while uninterruptible work runs**, answer-then-exec | A reboot mid-pull leaves truncated weights and mid-reconcile leaves half-applied settings; the response must reach the browser before the box disappears. Reuses `reboot-pending.json` so the shell reports `rebooting`. |
| D-42 | The selected model is readable on `/profile/`, changeable only by an admin | A member may know what they are talking to; changing it changes it for every account, so the button is an admin's. |
| D-43 | Icons are hand-drawn inline SVG (rail server-rendered; row actions via `createElementNS`) | No icon font, no request per glyph, no emoji (a different picture in every OS font), and no path from a string to parsed markup. |
| D-44 | **Every OWUI model entry is public** — reconcile asserts one wildcard read grant (`user:*`) through `POST /api/v1/models/model/access/update` | Bare-metal review: OWUI's model editor defaults a new entry to owner-only access, which hid the chat model from every non-admin. Model choice is a global, admin-managed setting, so there is no per-user model visibility to preserve; the access-only route never touches params/meta and backfills the minimal entry a non-preset model may not have. Sweep targets are skipped (they are deleted) and audit-only ticks report without writing. |
| D-45 | Bare-metal review fixes: the Users/Groups/Recent-changes sections are three bordered panels with equal headings; the Copy button falls back to `execCommand('copy')` when `navigator.clipboard` is absent (portal is plain HTTP on a LAN address); the audit CSV's first column is `timestamp`; a forced rotation stamps `last_login` (every OTP-created account's first sign-in lands in `srp/rotate`); the profile status pill starts hidden and the username is set as an input value | All reported in the same BM cycle; the clipboard fallback is the only behavior change — `navigator.clipboard` exists only in a secure context, so on the box it never worked. |
| D-46 | Power interlock refuses only the **ruinable** phases (pull, load); the readiness verify — same lock, but a read — refuses nothing. The gate snippets are **persisted** to `/var/lib/lemonseed/` and restored by the boot seeder, so the edge gate is correct the moment nginx starts | BM-2 review: a reboot right after boot answered "model being downloaded or loaded" while the model was already serving, because the pass holds the prepare lock through its verify; and the first navigation after a reboot got a bare needs-admin JSON, because the empty-seeded `/run` snippet left the edge open until the gate worker finished waiting for Postgres. Reboot and shutdown now behave identically at every point after boot. |
| D-47 | **Update affordance:** *Check now* and *Download and install* are **jobs** (`202` plus a pollable status read off the updater's own log lines), never a synchronous `apply`; retry is **cancel-then-apply**; the power buttons refuse (`409`) while an install runs; the failed-update banner reads the arm-time `update-attempt.json` the updater records, cleared when the target lands; a weekly `lemonseed-update-check` timer keeps *latest* fresh | A multi-gigabyte download cannot share a request lifecycle with the panel's poll, and an install's own reboot is the appliance doing the power button's work, so it rides the same `reboot-pending.json` marker. The attempt record is the only moment the target slot is ever known, so it is the only honest basis for a "your update failed" banner. BM-3 review: the dialog persists across download, write, and reboot with a progress bar driven by the updater's stdout percentages; the Try-again button is gone (the CLI is the remedy for a failed attempt); and `apply` preflights the slot pointer, the pending boot state, any leftover attempt record, and a same-version channel before touching a disk. |
| D-48 | The `lemonseed-update` wrapper exports `LEMONSEED_GRUB_CFG`, and a slot write with **no resolvable grub template fails loudly**; the ESP `grub.cfg` copy is unconditional | PLAN.md P5 Q3's field defect: installed boxes never refreshed the ESP `grub.cfg` except by USB reinstall, because the wrapper exported only `PYTHONPATH`/`LEMONSEED_AB`/`LEMONSEED_DOWNLOAD_DIR` and both fallback paths were created by no module. A success path that silently skips a step is unobservable; a failure is not. |
