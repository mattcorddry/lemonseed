# M6.3b Phase 4 — Portal ownership view, enforce-mode guards, reset window

Status: **Approved at the gate of 2026-10-07; 4A building.** Decisions recorded
in §8 (P4-A through P4-E); §1.1 explains why two guards stay advertise.
Spec source of truth: `DESIGN.md` §M6.3b (*Portal visualization & write guards*,
*Implementation* items 6–7, *Testing*). Where this plan and DESIGN.md conflict,
DESIGN.md wins and this document gets corrected.

After this sprint (user direction 2026-10-07): return to **M6.2b** user/group
management and **Phase 5** LiteLLM model management/proxy — those two are the
next work items, and M6.2b is what lets the account create/delete guards flip to
enforce (§1.1).

Phase status: Phase 1 (service identity), Phase 2 (persistence flip) and Phase 3
(the `owui_policy` engine) are landed and validated (`make test` 316 green;
`make test-qemu` green ×2). Phase 5 (LiteLLM friendly models + SearXNG/ComfyUI
entries) waits behind this phase.

## 0. Recommended split: 4A then 4B, each with its own test gate

A QEMU cycle is ~30–45 min under TCG, so the phase is split at the point where
risk changes character:

| Sub-phase | Deliverable | Risk profile |
|-----------|-------------|--------------|
| **4A** | Read-only ownership view: `GET /_lemonseed/api/owui/config` + portal panel (badges, tooltips, namespace rollup, deep links) | Read-only. Cannot break chat or config. Delivers the visual artifact you asked for. |
| **4B** | Enforcement + mutation: nginx guards promoted to **enforce**, `POST /_lemonseed/api/owui/reconcile`, `POST /_lemonseed/api/owui/reset-config` (the stop→truncate→start→reconcile window) | Can deny a real admin action and can take OWUI down briefly. |

Gate rule stands: **no transition while `make test` or `make test-qemu` is red.**
4A is a full sprint (unit + QEMU green), then 4B is a full sprint.

## 1. Scope

**In (4A):**

| # | Deliverable | Touches |
|---|-------------|---------|
| 4A.1 | `owui_portal.py` — pure join of manifest + OWUI `configs/export` → ownership tree, badges, namespace rollups, masking. No I/O, no imports of `modules.py` | new file |
| 4A.2 | `GET /_lemonseed/api/owui/config` (admin-gated, JSON) | `stub.py` |
| 4A.3 | Settings panel section *Chat interface (Open WebUI)*: tree with badges + `reason` tooltips + per-namespace rollup + policy status strip (`ok`, last reconcile, pending drift) + Refresh | `stub.py` (`page_settings_content`) |
| 4A.4 | Deep link bridge: panel link → shell opens that OWUI admin page in the chat frame | `stub.py` (shell + panel JS) |
| 4A.5 | Nix: install `owui_portal.py` with the CP; nginx location for the endpoint (gated + admin) | `control-plane.nix`, `gateway.nix` |
| 4A.6 | Tests (§5, §6): new `test_owui_portal.py`, endpoint tests, QEMU section | tests, smoke script |

**In (4B):**

| # | Deliverable | Touches |
|---|-------------|---------|
| 4B.1 | Guard renderer: manifest `guards[mode=enforce]` + runtime placeholders → `/run/lemonseed/owui-guards.conf` text (pure function, unit-tested) | `owui_portal.py` |
| 4B.2 | Render + `nginx reload` from the CP after service identity is provisioned; re-render when the service-account id or manifest changes | `modules.py` |
| 4B.3 | nginx: `include /run/lemonseed/owui-guards.conf;` in the OWUI-bound location; seed the file empty beside the auth-gate snippets | `gateway.nix` |
| 4B.4 | Promote the day-one guard set to `enforce` in the manifest (see **P4-A**) | `owui-policy.json` |
| 4B.5 | Reset maintenance window: `reset_owui_config()` — stop both slots → `TRUNCATE TABLE config` → start candidate → wait `/health/db` + `/ready` → full reconcile → cutover | `modules.py`, `db.py` |
| 4B.6 | `POST /_lemonseed/api/owui/reconcile` and `POST /_lemonseed/api/owui/reset-config` (admin-gated) + confirm dialog and progress in the portal | `stub.py` |
| 4B.7 | Tests (§5, §6), including replacement of the now-obsolete "all guards advertise" assertion | tests, smoke script |

**Out (explicitly later):**

- **Identity-event pushes** (user create/delete/admin change → immediate OWUI
  push) and the **`groups` entity handler**. Both need M6.2b's user/group
  mutation endpoints as callers; today they would be dead code with no call
  site. The seam contract is pinned in §7 so M6.2b lands them in one change.
- **Body-aware guards** (`name` matching `^Lemonseed.*$` on create) — DESIGN §11
  backlog; path-only nginx guards cannot see request bodies.
- **LiteLLM friendly models** and SearXNG/ComfyUI manifest entries → Phase 5.
- **Per-connection management** (letting admins add their own connections) →
  D5 deferred; day one keeps `openai.api_base_urls` as one whole-list write.

### 1.1 Blocking discovery: M6.2b is not built, and two guards depend on it

Verified in the current tree: there is **no `/api/auth` user-management
endpoint and no portal Users & Groups section** (M6.2b is still open in DESIGN's
M6 acceptance checklist and §14). Today the only working path to create or
remove a household account is **OWUI's own admin UI**.

So DESIGN's day-one enforce set — `POST /api/v1/auths/add` and
`DELETE /api/v1/users/.*` — would leave an admin with **no way at all** to add
or remove a user until M6.2b ships. That is a real regression, not a policy
nuance, so it needs your call (**P4-A**, §8).

## 2. 4A — how the ownership view works

### 2.1 Data path

One call per portal view, no polling loop:

```
panel (Refresh) → GET /_lemonseed/api/owui/config      [admin-gated]
                    ├─ owui_policy.load_manifest()      (cached, already parsed)
                    ├─ owui_policy.state_summary()      (in-memory, no DB)
                    └─ client.call("config.export")     (one OWUI round-trip)
                    → owui_portal.build_view(manifest, state, export) → JSON
```

`configs/export` is the right source because it returns **every** DB-backed key,
managed or not — which is exactly what makes the “yours” badge possible. Cost is
one request; the settings page deliberately does **not** poll this endpoint the
way it polls `/api/chat` (render on open + Refresh).

### 2.2 Badge taxonomy

| Badge | Rule | Portal copy |
|-------|------|-------------|
| `managed` | key is a `managed` setting entry | **Lemonseed manages** - tooltip = manifest `reason` + cadence |
| `initial_setup` | key is an `initial_setup` entry | **Seeded once** - yours after that |
| `yours` | key present in the export, absent from the manifest | **Yours** |
| drift overlay | a row whose key appears in the last drift report | state label + note: drift seen and not corrected, could not be applied, ... |

Entity kinds (models/users) and guards have no config-table rows, so they render
as **cards** beside the tree: one per entity kind (mode, owned ids,
reserved-name regex, scope, drift counts + samples) and one per guard with an
explicit `enforced` boolean - so "advisory today" is never mistaken for
"blocked". That is where "models are selectively managed but users are fully
managed" becomes visible, which was the original ask.

Payload shape (asserted by `test_owui_portal.py` and the QEMU smoke): `ok`,
`live_available`, `policy` (+ `drift_counts`), `counts` (config / managed /
initial_setup / yours, always zero-filled), `sections` (section, title, counts,
total, rollup, entries), `entities`, `guards`. It degrades rather than fails: no
export means `live_available=false` and intent still renders; a rejected manifest
classifies every key `yours` and sets `policy.manifest_error`.

### 2.3 Namespace rollup

**As built:** group by Open WebUI's **top-level** config segment (`web`, `openai`,
`auth`, ...), not the first two segments. The two-segment rule shredded the small
namespaces into one-row groups (`ollama.enable`, `automations.enable`,
`direct.enable` each alone), which is the opposite of browsable, and OWUI's own
admin UI groups by this same segment. On the real appliance that is 35 sections
over ~395 rows, each a collapsible details block with an `ours` / `mixed` /
`yours` rollup badge and managed / seeded-once / yours counts.

Rollups are computed over the union of export and manifest keys, not the manifest
alone, so a namespace an admin has populated shows real denominators - e.g. `web`
shows `mixed` because `web.search.enable` is ours and `web.ui_enabled` is theirs.

### 2.4 Masking

**As built, masking is broader than the plan said, and that is deliberate.**
`configs/export` is Open WebUI's `Config.get_all()` and redacts **nothing**, and
the drift snapshot in `cp_meta` is served by `/api/modules`, which is auth-gated
but **not** admin-gated - so an unredacted payload would leak an admin's provider
credentials to any household member. Therefore:

- `ValueSpec(kind=secret)` keys (e.g. `openai.api_keys`) collapse to `"***"`.
- Structured values keep their **shape** with secret leaves masked, so a
  connection list still reads as one connection to a named base url.
- A leaf-name heuristic covers keys we do **not** manage (`*.token`,
  `*.auth_token`, `*.bearer_token`, `*.client_secret`, `*.password`, ...).
  Matching is on the exact last segment, so flags like `auth.enable_api_keys`
  and path lists like `auth.api_key.allowed_endpoints` still render.
- Drift entries are masked **at serialization** (`masked_report_entry`), never
  in `plan()` - the planner keeps seeing real values, and applied actions come
  from `Action` objects, so masking can never corrupt a reconcile.
- `users:` entity rows report state, not mirrored person attributes.

Known limit: a users drift row keeps its `users:<email>` **key** (operators need
to identify the row), so member e-mails remain visible in `/api/modules`.
Accepted for now; revisit once the member list is treated as sensitive.

### 2.5 Deep links without touching OWUI's DOM

Locked M6 architecture forbids DOM injection into OWUI, so links stay on **our**
panel page. Click posts a `{lemonseed: "owui-admin", path: "/workspace/models"}`
message to `parent`; the shell allowlists the path (exact match, or the route
plus a `/` prefix) against the verified route table before touching the frame it
owns, and marks the frame overridden so returning to Chat puts it back on
`/?embed=1`. Verified v0.11.0 admin routes only — currently `/workspace/models`;
`/admin/settings` was removed after the bare-metal review (nothing links to it any
more, and a smaller allowlist is a smaller surface). Order matters and was the
original bug: the shell sets the keep-deep-link flag, aims the frame, *then* switches
views, because `showChatFrame()` resets an overridden frame to `/?embed=1` and was
undoing the very switch the admin had asked for. A path that is not on the
allowlist is dropped: an arbitrary src from postMessage would be an open redirect
through our own origin. No `window.open` fallback (it would escape the shell and
skip the allowlist).

## 3. 4B — guards, rendered from the manifest

### 3.1 Why the CP renders the file (not Nix)

Two placeholders are unknowable at Nix eval time: `%CP_OWUI_ID%` (the OWUI user
id of the service account, minted at first boot) and the enforce/advertise split
if we ever make it runtime-configurable. Nix contributes one **static include**;
the CP owns the content — the same reloadable-file pattern as
`/run/lemonseed/auth-gate.conf` and `owui-upstream.conf`.

| File | Owner | When |
|------|-------|------|
| `/run/lemonseed/owui-guards.conf` | CP writes (atomic temp+rename via the `_write_conf` pattern), then the module state machine's existing `reload_nginx()` | after service identity is provisioned; re-written when the guard render hash changes |
| seed (empty file) | existing `lemonseed-auth-gate-seed` unit, one more entry in its loop | before nginx starts |

An empty file is a no-op include: every variable and `if` lives inside the
rendered content, so nginx always parses at boot, before the CP has run.

### 3.2 The nginx idiom (one directive per guard, no `set` chains)

`parse_route()` already splits `"POST /api/v1/auths/add"` into method + path
regex, and guard validation rejects uncompilable regexes, unknown placeholders
and bogus methods. Rendering becomes a single quoted-string `if` over
`$request_method$request_uri`:

```nginx
# managed by the Lemonseed control plane — do not edit
if ("$request_method$request_uri" ~ "^POST:/openai/config/update(\?.*)?$") {
  return 403 '{"lemonseed":"managed-by-lemonseed","hint":"Change this in Lemonseed settings."}';
}
```

**v0.11.0 route facts that shape the rendering** (verified against the pinned
image's source, and why the guard set changed from the first draft): Open WebUI
mounts its `openai` and `ollama` routers at `/openai` and `/ollama` with **no**
`/api/v1` prefix, so those guard regexes must match the bare path inside
`locations."/"` — a `/api/v1/openai/...` pattern would silently never fire.
`POST /openai/config/update` is the actual connection writer: it upserts
`openai.enable`, `openai.api_base_urls`, `openai.api_keys`, `openai.api_configs`
and clears the model caches. `POST /ollama/config/update` is its analogue. What
the first draft guarded, `POST /api/v1/configs/connections`, writes only
`direct.enable` + `models.base_models_cache` — still worth guarding (direct
endpoints are ours to keep off), but it is not the egress valve.

Notes: `$request_uri` includes the query string, hence `(\?.*)?$`; the body is
convenience only (QEMU asserts the status code, not the content type). Guards
are included **after** `authGate` in `locations."/"`, so an unauthenticated
caller still gets the `302 → /login` behavior first, and an authenticated
browser request gets `403`.

All OWUI-bound traffic (including `/api/v1/*`, which has no dedicated location)
reaches OWUI through that one location, and the CP talks to slot ports
`127.0.0.1:3001/3002` directly — so “CP allowed, UI blocked” still falls out for
free with no allow-list to maintain.

### 3.3 Failure posture

- Render failure (placeholder unresolved, e.g. service identity not ready yet):
  **leave the previous file**, log `owui guards: …`, retry on the next supervisor
  tick. Never write a partial file.
- `nginx -t` is not run by the CP today; the reload is best-effort and a bad
  render would leave nginx on its last good config. Mitigation: the renderer is
  a pure function covered by unit tests, and it emits only from a closed set of
  patterns (no raw manifest text is interpolated other than the regex and the
  escaped placeholder value).
- Guards are **only** as strong as nginx: a client that can reach the slot ports
  bypasses them. That is the accepted threat model (LAN-facing entry is nginx
  `:80`; slot ports are localhost-bound).

## 4. 4B — the reset window

### 4.1 Why it cannot be live

Confirmed against the v0.11.0 source: the config store is the `config` table
(`key` PK, `value` JSON, `updated_at`) in OWUI's own database. Truncating it
under a running slot degrades every subsequent key read to the upstream default
mid-session — including `auth.enable_api_keys` (default `false`), which would
invalidate the CP's own `sk-` key while it is reconciling. **Blue/green does not
help**, because both slots share the same database: the old slot would serve
half-configured state. So both slots must be down for the truncate.

### 4.2 Sequence (all primitives already exist in `modules.py`)

```
0. guard: refuse unless state=ready, no reset in flight (non-blocking lock),
   request is admin-authenticated. Set state="resetting" (visible in /api/modules).
1. stop the active slot; ensure the candidate slot is stopped too
   -> nginx gets connection-refused on lemonseed_owui -> error_page 502 ->
      @owui_loading: the Getting-Ready page serves. No new nginx work needed.
2. TRUNCATE TABLE config  (psycopg on OWUI's db via db.postgres_admin_dsn();
   the config table only — chats, users, files untouched)
3. start the candidate slot (_run_container on the other port)
4. wait /health/db then /ready (http_ready_stable)
5. full write reconcile: triggers {at_boot} (+ on_upgrade), and clear
   _policy_slot_done so it cannot be skipped as "already done".
   initial_setup entries re-seed automatically: after the truncate their keys
   are absent, which is exactly the initial_setup firing condition.
6. verify sk- key still valid (the key lives in the user table, not config;
   auth.enable_api_keys is re-asserted at step 5) -> mint a fresh one if not
7. write_upstream(candidate) + reload nginx; state back to ready
```

### 4.3 Failure ladder (your D1 mitigations, wired)

| Stage fails | Action |
|-------------|--------|
| 1–3 (slot won't come up) | Stay on the Getting-Ready page, `state="error"` + `reason`, retry button. Never cut over to an unreconciled slot (D1 again). |
| 4–5 (up, but reconcile not ok) | Try `owui_policy.restore_lkg()` (Phase 3's last-known-good snapshot, already implemented and now finally load-bearing), then re-run reconcile. Still failing → stay on the maintenance page with the drift report in the portal. |
| 6–7 | Non-fatal: slot is healthy and reconciled; report the cutover error and retry next tick. |

The CP-container-pin option (your c) needs no work: the image tag is pinned in
tree, so a reset can never land on an untested OWUI build.

### 4.4 One cross-phase interaction to handle in the smoke test

The QEMU smoke's Phase-2 persistence proof asserts a marker key planted via
`configs/import`. A reset window **truncates it away**, which would silently
downgrade the next run's persistence assertion to “absent → plant again”. So the
reset section re-plants the persistence marker after the window and asserts it
came back (a stronger claim: reset worked *and* persistence still works).

## 5. Tests — unit (`make test`)

New `control-plane/test_owui_portal.py`:

- **View building:** managed/first-boot/yours classification against a manifest +
  a realistic export (dict of dotted keys); rollup math (`3/41`, unmanaged-only
  namespace, empty export); drift overlay marks `pending`; secret keys masked and
  the plaintext never appears in the serialized JSON; policy cards render
  entities + guards.
- **Guard renderer:** one `if` per enforce guard; advertise guards produce no
  nginx output; `%CP_OWUI_ID%` substituted and regex-escaped; unresolved
  placeholder → renderer refuses (returns previous content, no partial file);
  output is byte-stable for the same input (so the CP can hash-compare and skip
  needless reloads); empty enforce set → empty file.
- **Reset window** (`test_modules.py`, faked subprocess/psycopg/client): happy
  path order (stop → truncate → start → ready → reconcile → cutover); refuses
  when not ready; refuses when a reset is already in flight; reconcile-fail path
  calls `restore_lkg` and does **not** cut over; truncate is issued against the
  `config` table only; `_policy_slot_done` is cleared.

Modified:

- `test_owui_policy.py` — the shipped-manifest test that asserts
  `{g.mode} == {"advertise"}` (line ~149) becomes **obsolete**: it changes to
  “every guard is enforce **or** explicitly listed as awaiting M6.2b”, so a
  future guard added as advertise-by-accident still fails the test.
- `test_control_plane.py` — endpoint auth gating (admin 200, member 403, anonymous
  handled by the nginx gate) and the settings-page section markers.

Removed: nothing beyond that one obsolete assertion — Phase 3's engine tests all
remain valid (Phase 4 is a consumer of the engine, not a replacement).

## 6. Tests — QEMU (`make build-qemu && QEMU_API_TIMEOUT=1200 make test-qemu`)

New/extended assertions, in order:

1. **Carry-over gap from Phase 3** (close it here, it costs nothing): after the
   ok-gate, assert the *shape* of the managed connection in `configs/export` —
   `openai.api_base_urls == [<lemond base>]`, `openai.api_configs` has exactly
   that one key with `valves`. Today QEMU asserts `openai.enable=True` but never
   checks the URL, so a wrong base URL would sail through and only break chat on
   hardware.
2. **4A — portal endpoint shape:** `GET /_lemonseed/api/owui/config` (session
   cookie) returns `entries[]` where `ollama.enable`, `openai.enable`,
   `web.search.enable` carry `badge: "managed"` with a non-empty `reason`;
   unmanaged keys carry `badge: "yours"`; `openai.api_keys` is masked; rollup for
   the `openai` namespace is non-zero; guard + entity policy cards present. Assert
   **badges, not values** for `web.search.enable` (this section runs before the
   drift-flip section, and value assertions there would race the boot reconcile).
3. **4B — guard live vs direct:** the same call, two paths. Direct to the slot
   port: `POST /api/v1/configs/connections` → `200/201`. Through nginx with the
   session cookie: `403`. Also service-account protection:
   `DELETE /api/v1/users/<service-id>` through nginx → `403`, and a *non*-service
   path is not blocked by the render (sanity that the regex is anchored, e.g.
   `POST /api/v1/configs/connections-x` must not 403).
4. **4B — reset window:** trigger `POST /_lemonseed/api/owui/reset-config`; while
   it runs, poll through nginx and assert only the Getting-Ready interstitial
   serves (no OWUI assets, no half-configured 200s); after it returns,
   `owui_policy.ok` is true again, the Phase-2 marker key is **gone** (proves the
   truncate hit), a freshly planted key survives a later read, and the managed
   keys match the manifest (`ollama.enable` false, `openai.enable` true, base URL
   correct).
5. **Existing invariants stay:** two-run drift proof, audit-pending semantics,
   single cutover in the serial log, no env drift (serial greps:
   `owui env|owui policy|owui guards|owui: cutover|owui supervise`).

Because 4B changes state semantics, **run `test-qemu` twice** on the final tree
(same-userdata second boot) as we did for Phase 3.

## 7. Seams we pin now so M6.2b doesn't redesign this later

Deferred work stays cheap if Phase 4 leaves these shapes in place:

0. **Group ownership must be decided before it is managed.** The trusted-header
   sync is destructive with respect to groups we do not provision (see §9.1), so
   M6.2b starts by choosing: stop sending `X-Forwarded-Groups`, or send a closed
   CP-provisioned namespace. The view already renders group-less today; nothing
   in 4A depends on the answer.
1. **Identity push seam.** `owui_policy` already accepts a trigger set; add
   `"on_identity"` to `_cadence_fires`, and in M6.2b's mutation handlers call
   `modules.request_policy_reconcile(frozenset({"on_identity"}))` after a
   committed user/group transaction. The CP's member-delete handler is the one
   place allowed to issue `users.delete` (the reconcile loop never deletes).
2. **Groups handler.** `EntityHandler` is already the extension contract
   (`validate/observe/desired/changes/apply`, plus `reserved` for sweeps); adding
   `GroupsHandler` is one class + one `entities.groups` manifest block keyed by
   group name, mirroring CP `groups`/`user_groups`. Written when there is a CP
   group mutation to mirror, using this recipe.
3. **Admin demotion.** `users.update` is already on the client; the demotion push
   is a call site, not new plumbing.

## 8. Decisions I need from you at the gate

**P4-A — guard enforcement vs the missing M6.2b (the one real decision).**

- **(i) Recommended: enforce what doesn't strand anyone now.** Day-one enforce =
  `POST /api/v1/configs/connections`, `POST /api/v1/users/%CP_OWUI_ID%/update`,
  `DELETE /api/v1/users/%CP_OWUI_ID%`. Account create/delete guards stay
  **advertise** (badge: “Lemonseed will manage this — not yet blocked”) and flip
  to enforce in the same change that ships M6.2b's user CRUD + delete
  propagation. Enforcement never removes a capability the appliance can't
  already provide, and flipping later is a one-line manifest edit + re-render.
- (ii) Pull a minimal M6.2b slice into Phase 4 (CP user add/remove + portal
  Users list + OWUI propagation) so all five guards enforce on day one. Bigger
  phase, mixes two milestones, and M6.2b's reset-password/disable paths would
  still land later.

**P4-B — reset window trigger surface.** Proposed: admin-only, explicit typed
confirmation in the portal (the button opens a confirm dialog with a “this wipes
chat-interface settings, not chats or accounts” line), one concurrent reset, and
a status row in Settings while it runs. No auto-reset on reconcile failure —
failures surface with a Retry, per your “automatic or prompt the admin” note.

**P4-C — how much of the unmanaged config to show.** Proposed: show every
exported key to **admins** (masked for secrets), members see none of this panel.
Alternative is manifest-adjacent keys only, which weakens the “see what is
yours” goal.

**P4-D — manual reconcile button.** Proposed: yes, admin-only
`POST /_lemonseed/api/owui/reconcile` (a write run with the boot trigger set).
It is the escape hatch after an admin deliberately breaks something and wants it
back now rather than at next boot.

**P4-E — 4A/4B split.** Proposed as §0. If you would rather have one phase and
one QEMU cycle, say so and I will run 4A+4B as a single sprint.

## 9. Exit criteria

Per sub-phase: `make test` green; `make build-qemu` + `make test-qemu` green
(×2 for 4B); DESIGN.md §M6.3b acceptance checklist items 5–7 ticked with
evidence; the proposals doc and DESIGN.md implementation/testing rows updated;
nothing committed.

### 9.1 4A landed (2026-10-07)

- **Code:** `control-plane/owui_portal.py` (pure `build_view`), masking helpers in
  `owui_policy.py` (`is_secret_key`, `mask_setting`, `masked_report_entry`, now
  applied where the drift snapshot is serialized), `modules.owui_config_export()`
  (`{ok,data,error}`, never raises), the admin-only `GET
  /_lemonseed/api/owui/config` route, the settings-panel tree/strip/cards, the
  shell deep-link bridge, and the exact-match admin location in `gateway.nix`.
- **Tests:** `make test` **365** green (31 view + 8 masking + 5 endpoint + 3
  markup new); `QEMU_API_TIMEOUT=1200 make test-qemu` **PASSED**, asserting
  badges rather than values: 395 rows / 35 namespaces / 7 guards against the real
  v0.11.0 image, the admin-planted marker classified `yours`, the `openai`
  namespace `ours`, exactly one managed base url mirrored by `api_configs`,
  `openai.api_keys` masked, guards all advisory, and no `sk-` anywhere in the
  payload.
- **Manifest corrections found by reading the pinned source, not the docs:** the
  connection writer is `POST /openai/config/update` (guard added; the
  `/api/v1/configs/connections` reason was wrong), `POST /ollama/config/update`
  added, and the users-entity reason now states what actually mirrors membership
  (trusted headers at sign-in **plus** the boot loop).
- **Discovered while verifying (matters for M6.2b):** trusted-header sign-in
  calls `Groups.sync_groups_by_group_names`, which **removes** any OWUI group
  membership missing from `X-Forwarded-Groups` — so groups an admin creates in
  OWUI lose our users at their next sign-in. Upstream skips the sync when the
  header is empty, which is the cheap mitigation. Filed in DESIGN.md §11 as a
  decision that gates M6.2b.

Flag for your **bare-metal pass** (things QEMU structurally cannot see), which I
recommend doing at the end of 4B rather than before it:

1. Portal ownership view in a real browser: badges, tooltips, rollups, deep link
   opening OWUI admin in the chat frame.
2. Guard UX as an admin experiences it: try to add a connection in OWUI's admin
   UI → the blocked-403 error surfaces inside OWUI, and the portal badge explains
   why.
3. Reset window end-to-end on hardware: maintenance page duration feels sane,
   chats/accounts survive, settings come back to intent.
4. Carry-over Phase-3 items still open: real chat through OWUI with a GPU model,
   and a true `lemonseed-update` upgrade from an installed image (proves the
   one-time pre-marker container recreate).

## 10. Risks

| Risk | Mitigation |
|------|-----------|
| Enforcing a guard strands a real admin action | P4-A(i): enforce only guards with a Lemonseed equivalent or a pure-protection purpose; advertise the rest with visible copy. |
| Bad nginx render takes the public entry down | Pure renderer + unit tests; empty-file seed; atomic write; reload is best-effort so nginx keeps its last good config; render only from a closed pattern set. |
| Reset window leaves OWUI unusable | Never cut over an unreconciled slot; LKG restore then re-reconcile; maintenance page is the failure state, not a half-configured UI. |
| `configs/export` grows large and the panel gets slow | One call per view, no polling; rollups let the default render be collapsed; if a real box shows pain, paginate per namespace then (not before). |
| Portal leaks config values | Admin-gated endpoint (nginx `adminGate` + CP-side check), secret-bearing keys masked, masked plaintext asserted in a unit test. |
| 4B's two-run QEMU drift interacts with reset's truncation | §4.4: re-plant and re-assert the persistence marker inside the reset section. |
