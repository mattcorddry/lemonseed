# M6.3b Phase 4B — Enforcement: guards, manual reconcile, config reset

> **Milestone design artifact.** Phase plan for M6.3b Phase 4B; superseded by
> `DESIGN.md` for how the appliance behaves today.

Status: **all gate decisions taken (T4-A…T4-F); building.** T4-F resolved as **F1** —
no extra port forward in the QEMU harness; the guest stays reachable only through
nginx, so the smoke run resembles production.
Supersedes §3–§6 of `OWUI-PHASE4-PORTAL-GUARDS.md` for 4B. §9.1 of that doc records
what 4A actually shipped; this plan is written against that reality, not the draft.

Gate rules inherited from the phase plan: `make test` green **and** `make test-qemu`
green (×2, because 4B changes state semantics) before 4B is called done; nothing
committed; flag the bare-metal pass explicitly (§8).

---

## 0. Corrections to the earlier 4B draft

Four claims across the two earlier drafts are wrong; fixed here before any of them
becomes code.

| Draft claim | Reality (verified) |
|---|---|
| "guards are included after `authGate`, so an unauthenticated caller gets the 302 → `/login` first" | nginx `if (…) { return …; }` runs in the **rewrite** phase; `auth_request` runs in the **access** phase. Rewrite wins regardless of include order, so an anonymous caller gets `403`, not the login redirect. This is now an explicit decision (**T4-A**), not an accident. |
| "CP talks to slot ports `127.0.0.1:3001/3002`" | Slot ports are **`18080`/`18081`** (`modules.SLOT_PORTS`). Still localhost-bound, so "CP allowed, UI blocked" still falls out for free. |
| "seed the guards file beside the auth-gate snippets — there is a unit test over that loop" | There is **no** unit test over the seed loop. `seedAuthGate` is a shell snippet in `gateway.nix` that loops over `auth-gate.conf auth-admin.conf`; coverage is QEMU-only (`/run/lemonseed/…` presence). We add the file to that loop and prove it in QEMU. |
| (this plan, first draft) "assert the same call direct to the slot port → 200, through nginx → 403" | **The smoke harness cannot reach a slot port.** `scripts/qemu-api-smoke-test.sh` forwards exactly one port (`hostfwd=tcp:127.0.0.1:19080-:80`, L191) and has no ssh / guest-agent path into the guest; the guest's `127.0.0.1:18080/18081` are unreachable from the host. That assertion as written was impossible. See **T4-F** for the replacement. |

Also confirmed for the render: no `location` matches `/api/v1/*`, `/openai/*` or
`/ollama/*`, so all OWUI REST traffic reaches the app through `locations."/"`
(`gateway.nix:528`). That one location is the only place guards need to live.

Two further nginx facts, learned by running the pinned nginx against the real render
rather than by reading drafts, and reflected in the code:

- `if ($a$b ~ …)` is **not a valid condition** — nginx's `if` takes exactly one
  variable. The render uses `$request` (the request line, `"METHOD URI PROTOCOL"`),
  anchored as `^METHOD ` with an ` HTTP/` tail, which gives the same method+URI match
  from a single variable.
- `default_type` is **rejected inside `if`**. The denial body is still JSON text; it
  just ships without a `Content-Type` claim, which OWUI's admin UI does not need to
  surface the error.

---

## 1. Scope

| # | Deliverable | Where |
|---|---|---|
| 4B.1 | `owui_policy.render_guards()` — pure manifest+placeholders → nginx text; `guards_hash()` | `owui_policy.py` |
| 4B.2 | Promote the day-one guard set to `mode:"enforce"` | `owui-policy.json` |
| 4B.3 | Render + atomic write of `/run/lemonseed/owui-guards.conf`, reload nginx only on content change; expose rendered state | `modules.py` |
| 4B.4 | nginx: static `include` of the guards file inside `locations."/"`; empty-file seed | `gateway.nix` |
| 4B.5 | Portal honesty: `enforced` reflects the **rendered** file, plus declared-vs-rendered hash card | `owui_portal.py`, `stub.py` |
| 4B.6 | `POST /_lemonseed/api/owui/reconcile` (admin-only, manual write run) | `stub.py`, `modules.py` |
| 4B.7 | `POST /_lemonseed/api/owui/reset-config` (admin-only, typed confirm) + the maintenance window + `db.truncate_owui_config()` | `stub.py`, `modules.py`, `db.py` |
| 4B.8 | Tests (§7): new unit coverage, obsolete assertions retired, QEMU sections ×2 | tests, smoke script |

Out of scope (unchanged from the phase list): user/group management (M6.2b),
LiteLLM model proxying (Phase 5), automatic reset, member-visible config.

---

## 2. The day-one enforce set

Locked earlier at the phase gate and re-checked against the pinned v0.11.0 routes.
Rule: **we enforce only where the appliance already provides the capability, or the
guard is pure self-protection.** Enforcing a guard that strands an admin action the
appliance cannot otherwise serve is a regression, and M6.2b (CP user CRUD) is not
built yet.

| Guard | 4B mode | Why |
|---|---|---|
| `POST /openai/config/update` | **enforce** | The real connection writer (upserts `openai.enable`, `api_base_urls`, `api_keys`, `api_configs`). Rewriting it clobbers the managed Lemonade connection. |
| `POST /ollama/config/update` | **enforce** | There is no Ollama on the appliance. |
| `POST /api/v1/configs/connections` | **enforce** | Writes `direct.enable` (direct endpoints), which we keep off. |
| `POST /api/v1/users/%CP_OWUI_ID%/update` | **enforce** | Protects the *Lemonseed Control Plane* service account. |
| `DELETE /api/v1/users/%CP_OWUI_ID%` | **enforce** | Same. |
| `POST /api/v1/auths/add` | advertise | Until M6.2b ships CP-side account creation, blocking this removes an admin's only account path. |
| `DELETE /api/v1/users/.*` | advertise | Same reason; flips to enforce with M6.2b's delete propagation. |

Advisory guards keep their portal copy ("Lemonseed will manage this — not yet
blocked"), so the intent is visible even where it is not enforced. Changing an entry
is a one-word manifest edit + re-render.

Manifest `guards` mode changes alter `policy_hash`, so the next boot re-asserts
through the existing `on_upgrade` path. No extra plumbing.

### 2.1 The enforcement boundary is a fence, not a wall (deliberate)

`POST /api/v1/configs/import` is **not** guarded, so an admin who knows it exists can
still write a managed key (`openai.api_base_urls`) directly, bypassing the connection
guard. That is on purpose:

- it is OWUI's raw settings path, and it is how an admin writes **their own** keys —
  the "yours" half of the ownership model. Guarding it would take away the power this
  whole milestone exists to hand over;
- the QEMU drift proof writes a managed key out-of-band *through this endpoint*, so
  guarding it would delete our ability to demonstrate drift detection at all;
- the backstop is the reconcile contract, not the fence: `full_management` entries are
  re-asserted at boot (and on demand via the manual button), so a clobber survives
  until the next write run and is reported as drift in the portal meanwhile. That is
  precisely the `managed` badge's promise.

Same reasoning for the model API (`selective_management`: we own `lemonseed-ai*` and
clobber a name collision, we do not block the endpoint). If we ever want the fence to
be a wall, the honest way is a per-key `enforce` mode on the *setting*, not more nginx
`if`s — noted for the backlog rather than built now.

---

## 3. 4B.1 — Guard rendering

### 3.1 Pure renderer (new, in `owui_policy.py`)

```python
@dataclass(frozen=True)
class GuardRender:
    text: str                     # nginx include body (may be empty)
    enforced: tuple[str, ...]     # "METHOD /path" actually rendered
    pending: tuple[str, ...]      # enforce guards skipped: placeholder unresolved
    errors: tuple[str, ...]       # why each pending key was skipped
    sha256: str                   # sha256 of text; "" text still hashes

def render_guards(manifest: Manifest,
                  placeholders: Mapping[str, str]) -> GuardRender: ...
```

Rules:

- One directive per enforce guard, no `set` chains, no `map`:

  ```nginx
  # managed by the Lemonseed control plane - do not edit
  if ($request ~ "^POST /openai/config/update([?][^ ]*)? HTTP/") {
    return 403 '{"lemonseed":"managed-by-lemonseed","hint":"Manage OpenAI connections from Lemonseed settings."}';
  }
  ```

- **`$request`, not `"$request_method$request_uri"`** (as drafted here before
  building). nginx's `if` requires a *single* variable on the left; concatenating
  two is a config error — `nginx -t: "invalid condition"` — and nginx does not
  accept `default_type` inside `if` either. Both were found by running the pinned
  nginx against the real render, which is now the standing way to check this
  renderer. `$request` is the request line (`POST /path?q HTTP/1.1`), so method
  and path share one variable and one pattern. An absolute-form request target
  would not match and fails open; LAN clients do not send one.
- `$request`'s URI runs to the next space → the pattern is anchored as
  `^<METHOD> <path>([?][^ ]*)? HTTP/`. No `$` and no backslash appear in a
  rendered pattern (nginx strips backslashes in quoted config strings, and the
  renderer owns the end anchor), so guard paths may use neither. Anchoring is
  load-bearing: the QEMU sanity case asserts a path that merely *starts with* a
  guarded path is **not** blocked.
- Placeholders are substituted with the **resolved value** (a UUID, validated
  against an id-shaped charset), not the `[^/]+` the validator uses for regex
  compilation. `%CP_OWUI_ID%` and `%SERVICE_ACCOUNT_ID%` are the same value
  (existing convention).
- A guard whose placeholder is unresolved (service identity not provisioned yet)
  is **skipped and reported in `pending`** — never rendered as `[^/]+`, which
  would block every user id.
- Body strings are single-quoted nginx strings built from the guard's **`hint`**,
  a short field with a strict charset (`[A-Za-z0-9 .,;:()/%+-]`, ≤160 chars,
  checked at load *and* at render). The plan's "assert the `reason` contains no
  `'` or `$`" was not satisfiable: shipped reasons contain apostrophes
  ("Open WebUI's connection editor"), so that rule would have raised on every
  tick and silently disabled enforcement. `reason` stays portal prose and is
  never interpolated; `hint` carries the copy, and its charset is what makes "a
  manifest string smuggles an nginx directive" structurally impossible.
  The denial body is JSON text served without a JSON `Content-Type` (see the
  `default_type` note above) — the marker, not the media type, is the contract.
- Output is byte-stable for the same input (sorted by method+path). The CP
  compares `sha256` to skip needless reloads.
- Empty enforce set → empty text → empty file → a no-op include.

Landed shape differs from the draft in one name: `GuardRender.errors` carries the
human-readable reason for each `pending` key (the draft had one list for both),
and `pending` is deliberately blank in `owui_guard_state()` unless
declared == on-disk == nginx. `guards_hash()` from the draft was dropped —
`GuardRender.sha256` is that function.

### 3.2 Who writes the file

| File | Owner | When |
|---|---|---|
| `/run/lemonseed/owui-guards.conf` | CP: `modules.sync_owui_guards()` writes atomically (the `auth._write_conf` pattern: tmp + `chmod 0644` + `replace`) and calls the existing `modules.reload_nginx()` **only** when the sha changes | first call after service identity is provisioned; re-checked whenever `policy_hash` or `_service_account_id` changes |
| seed (empty file) | existing `lemonseed-auth-gate-seed` unit, one more entry in its `for f in …` loop | before `nginx.service` |

`gateway.nix` contributes exactly two static lines (the include inside
`locations."/"`, and the extra seed filename). Everything dynamic stays CP-owned —
same pattern as `owui-upstream.conf` and `auth-gate.conf`.

Call site: `supervise_modules_once()` (cheap and idempotent, see §3.5) rather than
only inside `ensure_open_webui()`, so a manifest change is picked up on the next tick
instead of waiting for a cutover or a reboot. First render still happens right after
service identity is provisioned, so guards are in place before any admin can act.

### 3.2.1 No stale rules (the T4-B answer)

A rule is never added to or removed from the file incrementally. The file is the
render of the current manifest — a pure function of
`(manifest.guards[mode == "enforce"], resolved placeholders)` — rewritten wholesale.
A rule therefore cannot survive a policy change that removes it, provided a sync runs
after that change. Three things make that a guarantee rather than a hope:

1. **Sync on every supervisor tick.** Rendering a 7-entry manifest costs microseconds;
   the expensive parts (`replace` + reload) sit behind the sha compare. Staleness is
   bounded by one tick, not by the next reboot.
2. **`rendered_sha` and `nginx_sha` are tracked separately.** `nginx_sha` advances only
   after a reload returns 0. With a single hash, one failed reload would leave nginx
   serving the old rule set forever while the CP believed it was done; with two, the
   next tick retries.
3. **Fail-open for the UI, fail-closed for intent.** A render error keeps the previous
   file and is reported. The seed is an empty file, so a boot where the CP never runs
   means **zero** blocking rather than a frozen admin UI. The reconcile loop is
   unaffected either way, because it talks to the slot port directly.

Residual limit, stated plainly: the declared-vs-served hash pair in the portal
compares the live manifest to the **file**. nginx's in-memory config can lag the file
only while reloads keep failing — logged on every attempt, retried every tick.

The unit tests in §7.1 pin exactly the case asked about: a guard flipped
`enforce → advertise`, or dropped from the manifest, must vanish from the rendered
file, and the rendered text must not mention that URI at all afterwards.

### 3.3 Failure posture

- Render raises (unresolved placeholder beyond the known-pending case, assertion on
  reason text): **keep the previous file**, log `owui guards: …`, retry next tick.
  Never write a partial file.
- No `nginx -t` in the CP today; `reload_nginx()` is best-effort, so a bad render
  leaves nginx on its last good config. Mitigated by a pure renderer that emits only
  from a closed pattern set, covered by unit tests.
- Guards protect the UI path only. A client that can reach `127.0.0.1:18080/18081`
  bypasses them — accepted threat model (LAN-facing entry is nginx `:80`; slot ports
  are localhost-bound and are exactly what the CP uses, which is why the CP is never
  blocked by its own guards).

### 3.4 T4-A (decided: accept) — anonymous callers get 403

nginx evaluates `if (…) { return …; }` in the **rewrite** phase and `auth_request` in
the **access** phase, so guards fire before the login redirect regardless of include
order. Accepted as **A1**: guarded paths are mutating `POST`/`DELETE` endpoints that
need an admin session to be meaningful anyway, and the denial body carries no user
data and no config values. Normal navigation is unaffected (the guard set is
mutating-only, so an expired session still hits a `GET` and gets the login redirect).

Watch-list, to be revisited if any of these actually bites:

- An admin whose session expired mid-form in the OWUI admin UI sees "managed by
  Lemonseed" instead of a sign-in prompt. Fallback **A2**: render the pattern against
  `"$request_method$request_uri$cookie_lemonseed_session"` requiring a non-empty
  cookie, so anonymous traffic falls through to `auth_request` and gets the redirect.
  Costs a subtler regex per guard and a new way to shoot ourselves (a cookie-name
  change would silently disable enforcement), so it stays in §11 backlog, not in 4B.
- OWUI's toast/retry behaviour on a 403 arriving mid-save is a browser-only
  observation → §8 bare-metal pass.

Confirmed expectation from your note: OWUI surfaces a failed `fetch` as a small red
toast, and since every guarded path is an admin-only save, a toast plus a visible
"Lemonseed manages this" badge is an acceptable outcome. The body is JSON text
(served without a JSON `Content-Type` — nginx will not accept `default_type`
inside `if`), so whether OWUI shows our `hint` or a generic failure is exactly the
question §8 asks on hardware.

---

### 3.6 Where guards do *not* go

`locations."/ws/"` (`gateway.nix:522`) proxies OWUI's Socket.IO transport, which the
pinned build mounts at `/ws/socket.io` (`socket/main.py:251`). It gets **no** guards:

- nothing config-mutating rides Socket.IO — config and connection writes are REST;
- Socket.IO polling uses `POST`, so a guard include there would sit directly in the
  chat data path for zero benefit;
- keeping the include in exactly one location keeps "which requests are guarded?"
  answerable by reading one line of nginx.

QEMU asserts this (§7.2 item 4) rather than trusting it.

## 4. 4B.5 — "Enforced" in the portal means *actually enforced*

Today `owui_portal._guards()` derives `enforced` from `guard.mode == "enforce"`, i.e.
from the manifest. Once the CP renders a file, that is a claim about intent, not
reality — and reality is the thing worth showing.

- `modules.owui_guard_state()` returns
  `{declared_hash, rendered_hash, rendered: [...], pending: [...], enforced_count,
  pending_reason}` read from the *file* (re-rendered from the current manifest for
  the declared side) — cheap, local file only.
- Exposed in `modules_snapshot()["open_webui"]["owui_guards"]` (auth-gated, masked —
  it contains no values, but it stays inside the same gating as the rest).
- `build_view(manifest, status, export, guard_state=None)` gains an optional
  argument: `enforced` becomes `key in guard_state.rendered`; when the manifest says
  enforce but the render does not, the entry gets
  `state: "pending_service_identity"`. `guard_state=None` (unit tests, CP mid-restart)
  falls back to the old manifest-derived meaning, so 4A's tests keep passing.
- The portal guard card shows `enforced 5 of 7 · manifest <hash8> · served <hash8>`,
  with a mismatch line in accent colour. That is the difference between "we declared
  it" and "nginx is doing it", and it is what makes smoke assertions meaningful.

**T4-B:** is the view's `enforced` sourced from the rendered file (recommended — it
cannot lie) or from the manifest (one fewer data path)?

---

## 5. 4B.6 — Manual reconcile

`POST /_lemonseed/api/owui/reconcile` (admin-only; nginx `authGate + adminGate`,
CP-side `_require_admin` again — same double gate as `GET /owui/config`).

- New public seam in `modules.py`:
  `request_policy_reconcile(triggers: frozenset[str]) -> dict[str, Any]`. It exists
  for M6.2b too (§7 of the earlier doc), so it is the seam, not a one-off:
  - refuses with `{ok: false, reason: "not ready"}` unless identity is ready and a
    slot is serving;
  - non-blocking lock (the `chat_ready`/`data_services` pattern) → returns
    `{ok: false, reason: "in flight"}` with HTTP 409 if a reconcile is running;
  - runs `owui_policy.reconcile(port, {"at_boot", "manual"}, _policy_deps(port))`,
    clears `_policy_slot_done` so the run cannot be skipped as already-done, and
    returns the **masked** drift report (reusing `masked_report_entry`, which is
    already what `_reconcile_run` persists).
- Response: `{ok, drift, policy_hash, last_error}` — masked, admin-only.
- Portal: an "Apply Lemonseed settings now" button next to the policy status strip,
  results rendered inline; after it, a Refresh reloads the ownership tree.

This is the escape hatch for "an admin deliberately broke something and wants it
back now, not at next boot". Per P4-D, if we never use it, we delete it.

---

## 6. 4B.7 — The config reset window

### 6.1 Why it cannot be live (unchanged)

OWUI's config store is the `config` table in its own database. Truncating it under a
running slot degrades every later read to upstream defaults mid-session — including
`auth.enable_api_keys` (default `false`), which would invalidate the CP's own `sk-`
key mid-reconcile. Blue/green does **not** help: both slots share the database. Both
must be down.

### 6.2 Sequence

`modules.reset_owui_config()`, guarded by a non-blocking lock and
`state == "ready"`; runs on a daemon thread with the result visible in
`/api/modules` (the endpoint returns `202 accepted` immediately — a reset takes
minutes under TCG and must not hold an HTTP connection).

```
1. state = "resetting" (new lifecycle value; portal shows a progress row);
   OWUI_READY_FLAG.unlink() so the shell serves Getting Ready for document
   requests instead of riding the 502 path.
2. stop both slots (_stop_container on each SLOT_PORTS name)
   -> nginx gets connection-refused on lemonseed_owui -> error_page 502 ->
      @owui_loading. The Getting-Ready page serves the whole window; no new
      nginx surface needed.
3. db.truncate_owui_config()  # new: psycopg through db._connect("open_webui")
                             # (admin creds, module db); TRUNCATE TABLE config only
4. start the candidate slot (_run_container(other_port, env_path))
5. wait http_ready_stable (SPA assets) and OWUI's /health/db
6. force a full write reconcile: clear _policy_slot_done,
   triggers {"at_boot", "on_upgrade", "manual"}. initial_setup entries re-seed
   on their own — after the truncate their keys are absent, which is exactly
   the initial_setup firing condition.
7. verify the service identity + sk- key (user table, not config — but
   auth.enable_api_keys is re-asserted at step 6); re-mint if the call fails
8. write_upstream(candidate) + reload_nginx(); re-sync guards (unchanged id, so
   normally a no-op); state = "ready"
```

`TRUNCATE` is issued by a new `db.truncate_owui_config()` rather than inline SQL, so
the unit tests can fake one function and the statement (`TRUNCATE TABLE config`) is
asserted in exactly one place. It connects through the existing private
`db._connect("open_webui")` (admin credentials from `postgres.env`, the module's own
database — see `db.RESERVED_MODULES`), which is the same path `provision_data_plane`
already uses.

### 6.3 Failure ladder (D1 mitigations, wired)

| Stage fails | Action |
|---|---|
| 2–4 (slot won't come up) | Stay on the Getting-Ready page; `state="error"` + `reason` in `/api/modules`; portal offers Retry. Never cut over to an unreconciled slot. |
| 6 (up, but reconcile not ok) | `owui_policy.restore_lkg()` (version-gated, already implemented — this is the moment it becomes load-bearing), then re-run reconcile. Still failing → maintenance page + the drift report, no cutover. |
| 7–8 | Non-fatal: the slot is healthy and reconciled; report the cutover error and let the supervisor retry. |

Option (c) from your D1 list (roll back the OWUI image) needs no work: the image tag
is pinned in-tree, so a reset can never land on an untested build.

### 6.4 T4-C: reset does **not** auto-restore last-known-good

Deliberate: auto-restoring the LKG after a reset would undo the wipe the admin just
asked for. The LKG is only used to *recover a broken reset* (stage 6), never to
second-guess a successful one. Restoring the LKG as an end in itself would be a
third, separate admin action (`POST …/restore-config`) — proposed for the backlog,
not for 4B, since a reconcile rebuilds everything Lemonseed owns anyway.

**T4-C** asks you to confirm this asymmetry (reset ≠ undo) is the intended meaning
rather than adding a "reset then restore my own settings" flow now.

### 6.5 Trigger surface (P4-B, already agreed)

Admin-only; typed confirmation in the portal (the confirm string must be typed, the
copy says "wipes the chat interface's settings — not chats, models or accounts");
one reset in flight; status row in Settings while it runs; never automatic.

---

## 7. Tests

### 7.1 Unit (`make test`)

New in `test_owui_policy.py` — renderer (pure, no I/O):

- enforce guard → exactly one `if`, correct anchored regex, **no** `default_type`
  (corrected during implementation — nginx rejects `default_type` inside `if`; see §0);
- advertise guard → **no** nginx output;
- `%CP_OWUI_ID%` resolved → id appears literally; unresolved → guard lands in
  `pending` and `[^/]+` appears **nowhere** in the text;
- **`hint`** text containing `'` or `$` → `PolicyError`, no partial render (corrected:
  the charset rule belongs on `hint`, the nginx-interpolated copy, not on `reason`,
  which is portal prose and legitimately contains apostrophes);
- byte-stability: same input twice → same sha; different enforce set → different sha;
- empty enforce set → empty text.

New in `test_modules.py`:

- `sync_owui_guards()`: writes the file, reloads nginx when the sha changes,
  **does not** reload when it does not, keeps the previous file when the renderer
  raises;
- `request_policy_reconcile()`: refuses when identity not ready; returns in-flight
  on the second concurrent call; clears `_policy_slot_done`; returns a masked drift
  report (assert `sk-` never appears in the payload);
- `reset_owui_config()` happy path ordering (stop → truncate → start → ready →
  reconcile → cutover), refusal when not ready, refusal while one is in flight,
  reconcile-failure path calls `restore_lkg` and does **not** call
  `write_upstream`, truncate issued exactly once via `db.truncate_owui_config`.

New in `test_db.py`: `truncate_owui_config()` issues `TRUNCATE TABLE config` and
returns `{state, error}` on failure instead of raising.

Modified:

- `test_owui_policy.py::test_in_tree_manifest_loads` — the shipped-manifest
  assertion `{g.mode} == {"advertise"}` (L149) is **obsolete**. It becomes an
  explicit day-one table: these five paths enforce, these two are advertise **with a
  reason string naming M6.2b**. A guard added later as advertise-by-accident (or an
  account guard flipped early) then fails the test.
- `test_owui_portal.py` — guard-card cases for rendered-state sourcing
  (`enforced` from `guard_state`, `pending_service_identity` on mismatch, old
  behaviour when `guard_state=None`).
- `test_control_plane.py` — both POST endpoints: admin reaches the handler, member
  gets 403, reset without the exact typed confirm gets 400.

Removed: nothing else. Phase 3's engine tests are consumed, not replaced, by 4B.

### 7.2 QEMU (`make build-qemu && QEMU_API_TIMEOUT=1200 make test-qemu`)

**Harness constraint (new §0 row):** the smoke script reaches the guest through
exactly one port (`hostfwd …:19080-:80`, L191) — it has no path to
`127.0.0.1:18080/18081` inside the guest. So the draft's "same call direct to the slot
port → 200" assertion is not runnable as written. It is replaced by the **marker
rule** and the **CP-still-writes proof** below, which test the same claim
(guards bind the edge, never the CP) without a second port forward. If you want the
literal A/B comparison anyway, that is **T4-F**.

**Marker rule (applies to every assertion below).** Never assert on status alone.
OWUI itself answers `403` for permission failures, so a bare status check is
ambiguous in both directions:

- *guarded* → assert status `403` **and** body contains `"managed-by-lemonseed"`;
- *not guarded* → assert the body does **not** contain `"managed-by-lemonseed"`
  (whatever status OWUI returns).

In order, extending the existing OWUI section (it runs after the reconcile gate, while
the admin session + OWUI token are live, and **before** the drift-flip section so a
bad render fails on the guard row rather than confusingly inside the drift test):

1. **Seed + render truth:** `/api/modules` reports
   `owui_guards.rendered_sha == declared_sha`, `nginx_sha == rendered_sha` (the reload
   actually landed), `enforced_count == 5`, `pending` empty, and the two advertise
   paths present but unenforced.
2. **Every enforce guard, one assertion each** (all via nginx, admin session). Your
   question, answered literally: **yes — one positive assertion per guard we ship,
   no exceptions.** 5 enforce + 2 advertise (§7.2 items 2 and 3) covers all 7, so a
   guard added to the manifest without a QEMU case is a visible gap, not a silent one:

   | Call | Expect |
   |---|---|
   | `POST /openai/config/update` | 403 + marker |
   | `POST /ollama/config/update` | 403 + marker |
   | `POST /api/v1/configs/connections` | 403 + marker |
   | `POST /api/v1/users/<service-id>/update` | 403 + marker |
   | `DELETE /api/v1/users/<service-id>` | 403 + marker |

   `<service-id>` comes from `/api/modules` → `open_webui.service_account_id`, so the
   `%CP_OWUI_ID%` placeholder is validated end-to-end rather than assumed. Bodies are
   minimal-but-valid, so the only reason a call can fail is the guard, not schema
   validation.
3. **Every advertise guard stays open** — with deliberately *non-mutating* calls, so
   the test creates nothing:
   `POST /api/v1/auths/add` with an empty body, and
   `DELETE /api/v1/users/does-not-exist`. Both must be OWUI's problem (400/404/422),
   never our marker. This is the test that fails if someone flips an account guard to
   enforce before M6.2b exists.
4. **Unguarded battery** (your "handful of key unguarded URIs" — chosen because the
   smoke suite itself depends on them, so a sloppy render breaks here loudly instead
   of mysteriously later). None may carry the marker:
   `GET /api/v1/configs/export`, `POST /api/v1/configs/import`,
   `POST /api/v1/auths/signin`, `GET /api/v1/models/list`,
   `GET /_app/version.json`, `GET /api/status`, and
   `GET /ws/socket.io/?EIO=4&transport=polling`.
   The last one is deliberate and specific: OWUI mounts Socket.IO at
   `/ws/socket.io` (`socket/main.py:251`), which nginx matches with the **separate**
   `locations."/ws/"` (`gateway.nix:522`) that gets **no** guards — see §3.6. Probing
   it proves the include landed only where we intended, and chat polling is exactly
   what would hurt if it had not.
5. **Anchor sanity:** `POST /api/v1/configs/connections-x` must not carry the marker.
   This is the only thing that proves the regex is anchored rather than prefix
   matching (which would silently block `…/connections/update`-shaped paths).
6. **Guards did not break the CP:** immediately after the guard assertions,
   `POST /_lemonseed/api/owui/reconcile` must return ok and the following
   `GET /api/v1/configs/export` must still show `direct.enable` false,
   `ollama.enable` false and exactly one `openai.api_base_urls` entry. The UI was just
   refused on those same keys and the CP wrote them a moment later — that is the
   "CP allowed, UI blocked" property, observed through state instead of a second
   port. It also catches the nasty failure mode where a bad render bricks the
   appliance's own ability to manage OWUI.
7. **Reset window:** trigger the reset; while it runs, poll through nginx and assert
   only the Getting-Ready interstitial serves (no half-configured 200s); afterwards
   `owui_policy.ok` is true again, `nginx_sha == rendered_sha` still holds, and the
   managed keys match the manifest.
8. **Marker re-plant (cross-phase):** the Phase-2 persistence marker is planted via
   `configs/import` and a reset truncates it away. The reset section asserts it is
   **gone** (proof the truncate hit), then **re-plants it** and asserts it reads
   back — so the next run's persistence proof stays honest.
9. **4A section updated:** the existing assertion "all guards `enforced is False`"
   (smoke L739) is **obsolete** → assert 5 enforced / 2 advisory and the hash pair
   matches.
10. **Existing invariants stay:** two-run drift proof, audit-pending semantics, single
    cutover in the serial log, no env drift. Serial greps gain
    `owui guards` beside the existing `owui policy|owui env|owui: cutover`.

Because 4B changes state semantics, **run `test-qemu` twice** on the final tree
(same-userdata second boot), as in Phase 3.

---

## 8. What QEMU cannot see → bare-metal ask

QEMU proves the render, the status codes, the reset window and the reconcile
endpoint. It does not exercise a browser against them. At the end of 4B please run,
on the NVIDIA box:

1. Open WebUI admin UI as a real admin → try to save the OpenAI connection → record
   **exactly** how OWUI presents a `403` whose body is our JSON. Expectation (yours,
   from using OWUI): a small red floating error box. That is acceptable for an
   admin-only action, but we should know whether the text is our `hint` or a generic
   "Request failed 403", because that copy is the whole user-facing story of this
   phase. Confirm the Lemonseed settings page still shows `openai.*` as ours
   afterwards (nothing half-saved).
2. Delete the **Lemonseed Control Plane** account → blocked; delete nothing else by
   accident.
3. Portal: Apply-now button, ownership tree after it, reset with typed confirm,
   the interstitial during the window, and the tree fully re-asserted after.
4. Chat still works end-to-end after the reset (lemond, and today's pre-LiteLLM
   connection).
5. Session-expiry spot check for the T4-A watch-list: sign in, leave the tab until the
   session dies, then try a managed save. If "managed by Lemonseed" instead of a
   sign-in prompt feels wrong in practice, that is the trigger for backlog item A2.

---

## 9. Decisions

### 9.1 Taken at this gate

| ID | Decision | Outcome |
|---|---|---|
| **T4-A** | Anonymous callers get `403` on guarded paths (nginx rewrite phase precedes `auth_request`) | **Accept (A1).** Watch for bad UX; §3.4 watch-list, A2 filed to backlog. |
| **T4-B** | Portal `enforced` sourced from the **rendered file** | **Yes, with the no-stale-rules rules** (§3.2.1): whole-file render, sync every supervisor tick, separate `rendered_sha`/`nginx_sha`, plus unit tests that a dropped guard disappears from the text. |
| **T4-C** | Reset never auto-restores LKG; LKG only recovers a *broken* reset | **Yes.** A "copy the config table before truncate → rollback a failed reset" path is agreed as the right escalation *if* reset proves buggy — filed to backlog, not built now. |
| **T4-D** | Reset on a background thread: `202` + status in `/api/modules` | **Yes.** |
| **T4-E** | Manual reconcile = `{"at_boot","manual"}` write run, masked report returned | **Yes**; drop the button if we never use it (P4-D). |

### 9.2 One new decision from the harness discovery

**T4-F — how to test "CP allowed, UI blocked" without a route to the slot port.**
The smoke script can only reach the guest through nginx (§0, §7.2), so it cannot call
the same endpoint both ways.

- **F1 (recommended): state-based proof, no harness change.** §7.2 item 6 — after the
  UI is refused on `openai.*`/`ollama.*`/`direct.*`, the CP-side manual reconcile
  returns ok and the export shows the managed values. Same property, arguably the more
  meaningful observation (it proves the CP can still do its job), and it costs nothing.
- ~~**F2: add a second `hostfwd` for one slot port**~~ to the smoke script's QEMU
  invocation, then assert the literal pair: direct `POST /openai/config/update` → not
  our `403`, via nginx → our `403`. Strongest form of the claim, but it deliberately
  exposes a port the appliance binds to localhost-only for the whole test run, and it
  depends on which slot is active at that moment (blue/green means either `18080` or
  `18081`), which is exactly the kind of coupling that makes smoke tests flaky.

**Decided: F1.** Rejected F2 on the grounds that the harness must not carry a bypass
port that production does not have — QEMU should resemble production as closely as
possible. The browser-side check in §8.1 covers the rest.

---

## 10. Exit criteria

1. `make test` green (new renderer/guard/reset/endpoint tests in; the two obsolete
   assertions replaced by explicit day-one tables).
2. `make build-qemu` + `QEMU_API_TIMEOUT=1200 make test-qemu` green **twice**.
3. DESIGN.md §M6.3b: acceptance items ticked for enforcement + reset, §11 backlog
   updated (LKG-restore-as-an-action, A2 anonymous exemption, config-table snapshot for
   failed-reset rollback — all three filed at this gate).
4. This doc updated with a "4B landed" section (what shipped, hashes, counts).
5. Nothing committed. Bare-metal items in §8 listed for you, not assumed done.

### 10.1 4B landed (2026-10-07)

- **Renderer:** `owui_policy.render_guards()` + `GuardRender` / `GuardPending` in
  `owui_policy.py`. Pure text in, text out: one `if ("$request" ~ "^METHOD … HTTP/")
  { return 403; }` per enforce guard, `hint` interpolated as the denial body, nothing
  at all for `advertise`. Placeholders resolve to literals or the guard goes to
  `pending` — a `[^/]+` pattern can never reach nginx.
- **Manifest:** `owui-policy.json` v1 now ships **5 enforce / 2 advertise**. Enforced:
  `POST /api/v1/configs/connections`, `POST /openai/config/update`,
  `POST /ollama/config/update`, `POST /api/v1/users/%CP_OWUI_ID%/update`,
  `DELETE /api/v1/users/%CP_OWUI_ID%`. Still advisory: `POST /api/v1/auths/add` and
  `DELETE /api/v1/users/.*`, each with a `reason` that names **M6.2b** as the owner of
  the flip. Every guard carries a `hint`.
- **Sync:** `modules.sync_owui_guards()` runs every supervisor tick — whole-file render,
  atomic write to `/run/lemonseed/owui-guards.conf`, nginx reload only when the
  rendered sha changes, previous file kept if the render raises. Guard failure cannot
  kill the tick.
- **Actions:** `POST /_lemonseed/api/owui/reconcile` (masked `{"at_boot","manual"}`
  report, 409 in flight, 503 before OWUI is ready) and
  `POST /_lemonseed/api/owui/reset-config` (typed confirm, `202` + `owui_reset` status
  in `/api/modules`, holds the module lock for the whole window so the supervisor
  cannot race it: both slots down → truncate → start → ready → identity → reconcile →
  cutover, with LKG restore if the rebuild does not come back clean).
  The persisted `sk-` is dropped before the post-reset reconcile — the truncate resets
  `auth.enable_api_keys` to false, so a remembered key would wedge the rebuild.
- **Gateway:** `owuiGuards` include in `locations."/"`, added to the `seedAuthGate`
  loop, exact-match admin locations for both actions. Nothing added to
  `locations."/ws/"`.
- **Tests:** `make test` **425** green. Renderer, guard-sync, manual-reconcile and
  config-reset suites added; `test_in_tree_manifest_loads` and the portal day-one
  table now assert the explicit 5/2 split (an accidental advertise, or an account guard
  flipped early, fails the test); the QEMU assertion "guards must exist and stay
  advisory" is retired.
- **QEMU:** green **twice** (`QEMU_API_TIMEOUT=1200 make test-qemu`, full run). Same
  numbers both runs: 395 rows / 35 namespaces / 7 guards / 5 enforced, rendered sha
  `1b955782f436` = declared, all 5 enforced paths refused with the
  `managed-by-lemonseed` marker, both advisory paths answered by Open WebUI (422 /
  200), the unguarded battery clean, `POST /api/v1/configs/connections-x` **not**
  matched (anchoring holds), CP still writes what the UI was refused, the out-of-band
  `web.search.enable` flip reported pending rather than silently fixed, and the reset
  window stayed on Getting Ready, rebuilt every managed key and re-established the
  guards. Serial log shows the render and the reset.

---

## 11. Bare-metal review (2026-10-08) → dispositions

The §8 checklist run on the NVIDIA box. Findings, what each one actually was, and
what landed. Validation for the whole batch: `make test` **430** green, and
`QEMU_API_TIMEOUT=1200 make test-qemu` green **twice** on
`out/appliance-20261008.01-qemu` (rendered sha now `c7f8eacc0aa5` — the denial body
changed; all five enforced paths refused, advisory paths still answered by Open
WebUI, reset window clean with the new `reset` confirm word).

| # | Observed | What it was | Disposition |
|---|---|---|---|
| 1 | LLM re-downloaded after the upgrade | Cache volume is fine; the `models_dir` policy only became effective once `config.json` was actually persisted, so pre-upgrade weights lived outside `/var/lib/lemonseed/models`. Independent second gap: lemond's shipped `auto_check_model_updates=true`. | **Partly fixed.** `auto_check_model_updates=false` pinned in `lemond.nix` (defaults *and* the every-start jq rewrite). Orphan-dir reclaim + CP "already on disk" fast path → DESIGN §11. Needs a bare-metal reboot check. |
| 2 | `lemonseed-cp@localhost` vs `admin@lemonseed.local` | Cosmetic *and* load-bearing: OWUI keys trusted-header identity on email, so a domain change duplicates accounts. Hostname-derived addresses are ruled out (the box renames itself after the factory mDNS handoff). `@lemonseed` (no dot) looked appealing but OWUI's `validate_email_format` requires a dot in the domain and exempts only `@localhost`. | **Aligned on `@localhost`.** `auth.OWUI_EMAIL_DOMAIN` + `auth.owui_email()` are now the single source for household identities (`create_admin`, `owui_identities`) and `modules.OWUI_SERVICE_EMAIL` derives from it, so the two can never drift again (pinned by a unit test). Nothing sends mail, so DNS resolution is irrelevant. **Existing boxes keep their stored `@lemonseed.local` rows** — the email is read from the user row, never re-derived, and rewriting it would duplicate the account in OWUI. |
| 3 | Guarded writes show a generic "Server connection failed" | Our denial body had **no `detail` key** and arrived as `text/plain` (nginx's `default_type`), so OWUI could not read a reason. | **Fixed.** Body is now `{"detail": <hint>, "lemonseed": …, "hint": <hint>}`; `default_type application/json;` added to the OWUI catch-all location (illegal inside `if`, and the only body that location generates is ours — the auth gate 401→redirect and upstream failures go to named locations). Verified against the pinned nginx **1.30.4** locally: guarded `POST` → `Content-Type: application/json` + `detail`; an unrelated location still serves `text/plain`, so the directive is scoped as intended. Smoke script now fails if a denial is not JSON with a non-empty `detail`. |
| 4 | Editing the CP account (email, admin→user) fails silently | Same cause as #3: the refusal landed in OWUI's generic catch. Note this exercised **our** guard, not upstream immunity — on that box the *human* is the earliest user, so OWUI's own primary-admin protection does not cover the CP account. | **Fixed** with #3 (the admin now sees why). |
| 5 | No delete option at all for `lemonseed-cp` | Answered: OWUI v0.11.0's admin user row offers **only** a pencil (Edit → Role/Name/Email/New Password/Save) for *every* account — there is no delete affordance in the table at all, so nothing was hiding it. | **Closed, no code.** Our `DELETE /api/v1/users/…` guards are unreachable from the UI and stay as defense-in-depth for direct API calls; the only supported delete path is the portal's, which is M6.2b. Advisory guard for `DELETE /api/v1/users/.*` unchanged. |
| 6 | Panel carries far too much per-method detail | Correct for us, wrong for a home admin. | **Fixed.** Default view: status pill + one actionable line + *Apply now* / *Reset chat settings* + a progress banner. The key tree, entity cards, guard cards and machine pills (version, policy hash, snapshot, reconcile time) moved into a collapsed **Advanced details**. |
| 7 | "Open Open WebUI settings" lands on the chat page | Real bug in the bridge, not a bad route: the handler set `chat.src = path` then called `setView('chat')`, and `showChatFrame()` immediately undid the override — so *every* deep link, including "Open model workspace", snapped back to `/?embed=1`. | **Fixed** via a one-switch `keepDeepLink` allowance: the frame still returns to the chat entry point when the admin clicks Chat themselves. `/admin/settings` dropped from `OWUI_DEEP_LINKS` (nothing links to it now, and a smaller allowlist is a smaller surface); the button is deleted. |
| 8 | Reset prompt had newlines, needed a long case-sensitive phrase, status was a tiny line at the window bottom | Copy and markup. | **Fixed.** `OWUI_RESET_CONFIRM = "reset"` matched with `.strip().lower()` (unit tests pin the case/space variants *and* that `reset chat settings` is still refused), single-line prompt copy, and progress in a bordered banner (`hidden` until there is something to say) that also adopts an in-flight reset when the panel is opened mid-window. Smoke script sends the new word. |
| 9 | Clicking Chat during a reset showed the cached SPA | `chatBootstrapped` stayed true while Open WebUI was stopped underneath it, so the old bundle kept rendering until a manual reload. | **Fixed.** `owuiResetting()` + `retireChatFrame()` in the shell: the frame is dropped (interstitial covers the window, fresh bundle on the rebuilt slot), and the interstitial now says "Rebuilding chat settings" instead of "Preparing the chat interface…". |
| 10 | Reset preserved user prefs and model settings; did clear a bad LDAP setup; chat returned after reload | Working as designed (config table only). | Recorded as evidence for §6. |
| 11 | Session expiry not exercised | — | Still open; it is the trigger for backlog item **A2**. |
| 12 | Admin panel is missing Reboot / Shut down / Update | Those operations only exist behind a root shell on tty2 — not a home-user path, and the only way to act on an available update. | **Out of scope here.** DESIGN §11 item extended and **scheduled for the next sprint**. |

### 11.1 Still wanted from the box

1. **Finding 1 log re-capture:** the lemond log handed over
   (`~/tmp/lemonseed-lemond.oct8.txt`) is **17 bytes** — the capture failed, so the
   re-download has no evidence behind it yet. Re-capture with
   `journalctl -u lemonseed-lemond -b -1 > ~/tmp/lemond-prev-boot.txt` after the next
   upgrade/reboot, and include
   `find /var/lib/lemonseed -maxdepth 5 -type d -name 'models--*'`.
   Note the reboot in question ran the **pre-fix** build, so it says nothing about
   the `auto_check_model_updates=false` pin.
2. Reboot once on the new build and confirm the model is **not** re-fetched (this is
   the only check for the `auto_check_model_updates` pin). **Done 2026-10-09** — the box
   goes straight to *Loading … onto the GPU* with no download message.
3. The session-expiry spot check (§8.5). **Still open.**
4. The new admin view: is *Apply now* / *Reset chat settings* + Advanced details the
   right amount of surface, or should the buttons change? **Answered — see §11.3.**
   Advanced details did not survive the answer.

(§11 #5 is resolved — OWUI's user table has no delete affordance at all.)

### 11.2 Follow-up batch (same day)

Answers and fixes that came out of the §11 review, plus two test-determinism bugs
that the review exposed.

| Item | What it was | Landed |
|---|---|---|
| §11 #2 email domains | `@lemonseed` (the obvious shortening) is **not** usable: OWUI's `validate_email_format` requires a dot in the domain and exempts exactly one no-dot domain, `localhost`. Nothing sends mail, so resolution was never the constraint — the format check is. | `auth.OWUI_EMAIL_DOMAIN` / `auth.owui_email()` are now the only source of a household identity email; `modules.OWUI_SERVICE_EMAIL` derives from it. Unit tests pin the shape and the shared domain, and the QEMU smoke check asserts the portal admin signs in as `admin@localhost`. |
| §11 #5 no delete affordance | OWUI v0.11.0's admin user row is Edit-only for *every* account (pencil → Role/Name/Email/New Password/Save). | No code. Guards on `DELETE /api/v1/users/…` stay as defense-in-depth for direct API calls; portal-driven delete is M6.2b. |
| Silent flake **A** — `OSError: [Errno 39] Directory not empty` in `test_auth.AuthGateWorkerTests` | `write_gate` writes GATE_CONF **then** ADMIN_CONF, but `_wait_gate` polled only GATE_CONF, so tearDown's `TemporaryDirectory.cleanup()` raced the worker's second write. | `_wait_gate` now also requires `auth._gate_synced`, which is set only after every write the worker performs. |
| Silent flake **B** — `AssertionError: 'idle' != 'ready'` in two `ChatReadyTests` | `test_ensure_chat_worker_returns_immediately` leaked its daemon thread on purpose (`join(timeout=1)` against a `time.sleep(30)` mock). The worker sleeps 2s *before* calling `prepare_local_chat`, i.e. after the decorator's patch is gone — so the real function ran mid-suite, on real paths, holding `_prepare_lock`; the next `prepare_local_chat()` lost the non-blocking acquire and returned the idle snapshot. | The test installs its patch with an explicit `start()` and registers cleanups so the worker is drained **while** the patch is still installed (LIFO order is the whole trick — inverting it reproduced the original bug exactly). |
| Backlog | Real email addresses + basic SMTP; directory-auth layer decision; portal Reboot / Shut down / Update **scheduled for the next sprint**. | DESIGN §11 (the existing update/shutdown/restart item was extended rather than duplicated). |

Determinism evidence for flakes A and B: **10 consecutive** full-suite runs, 432 tests,
all green (the pre-fix suite failed roughly 1 run in 6).

Validation for this batch: `make test` **432 green**; `make build-qemu` then
`QEMU_API_TIMEOUT=1800 make test-qemu` **PASSED twice** (24 checks each, guard sha
unchanged at `c7f8eacc0aa5` — nothing in the guard renderer moved). The second run is
the interesting one: the portal admin re-signed in as `admin@localhost` against the
existing user-data disk and the planted config marker survived container recreation.

### 11.3 Second and third reviews (2026-10-08/09) — too much sausage making

Both reviews said the same thing in different words: the panel was explaining the
implementation instead of giving a home admin something to do. Recorded here because
each round deleted surface rather than rearranging it.

| Observed on the box | Landed |
|---|---|
| A `CHAT` box with no settings in it, and a separate *Chat interface settings* panel below | One `Chat` panel with `Status` + `Controls` subsections. The empty top box is gone, not merged-by-rename. |
| `path /?embed=1` row | Deleted. The iframe URL is not a fact an admin can use. |
| *Open Open WebUI settings* lands on the chat page | Button deleted, with the whole deep-link bridge: `OWUI_DEEP_LINKS`, `owuiOpen`/`OWUI_LINKS`, the shell's `OWUI_ADMIN_ROUTES` allowlist and its `postMessage` listener. Navigating the OWUI frame from the portal was the *only* reason the shell accepted posted paths, so the listener left with it — and its open-redirect surface with that. |
| Per-method status and control everywhere; *Waaaay too much info* | Per-key ownership tree, entity/guard cards and machine pills deleted (~5.6 KB of JS). `GET /_lemonseed/api/owui/config` still carries them and QEMU smoke still asserts that payload, because a test that cannot see the classification cannot catch us regressing it. If a human ever needs to read it: DESIGN §11, *Ownership report as a build artifact*. |
| Download ETA never populated | Probe deleted outright, not fixed — see DESIGN §11 *Download-ETA speed probe*. `auto_check_model_updates=false` then confirmed on the box: reboot goes straight to *Loading … onto the GPU*. |
| Collapsed **Advanced details** holding version / image / reconcile time | Deleted one round later. Three facts that a user would never open a disclosure for became two **Status** rows: `Open WebUI v0.11.0 | Up 59 minutes (healthy)` and `Lemonseed configuration | updated 2026-10-09 05:05 UTC`. Pills now mean *problem* only, and the empty strip is `hidden` (an empty flex box still takes a line). |
| `Refresh` did not say what it refreshes | → **Refresh status** (it re-reads `/_lemonseed/api/owui/config` and re-renders the Status rows). |
| Untitled *Diagnostics* disclosure at the bottom | `API` panel, **System API Data**. It is also the intended home for the OpenAI-compatible endpoint toggle → DESIGN §11. |

Two invariants this left behind, both pinned by tests:

1. **The engine's verdict, not the container's.** `updated <stamp>` renders only when
   `owui_policy` reports `ok`; anything else is **needs attention** / **not applied
   yet** in red with `last_error` on hover. A green row that lied about reconciliation
   is worse than a red one.
2. **One timestamp format.** `YYYY-MM-DD HH:MM UTC`, produced by `stub._fmt_utc`
   server-side and by trimming the same ISO string client-side. `last_reconciled` is
   always written in UTC, so the two cannot disagree — and the QEMU smoke check asserts
   the *rendered* stamp, so a regression is a build failure rather than a bug report.

Validation for this batch: `make test` **453 green** (9 new tests this round, no
obsolete test left behind), loopback-only canary **0 non-loopback connects**,
and `make build-qemu && QEMU_API_TIMEOUT=1800 make test-qemu` **PASSED** (24 checks).
The QEMU run is the one that matters here: it asserts the *rendered* panel on a real
appliance, so `Open WebUI v0.11.0`, `Up …` and a real `updated … UTC` stamp came from
docker and a real reconcile rather than from a fixture.
