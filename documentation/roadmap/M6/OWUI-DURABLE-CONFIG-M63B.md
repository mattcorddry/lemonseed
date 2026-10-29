# M6.3b — Open WebUI durable config & control-plane reconciliation (design record)

> **Milestone design artifact.** Moved out of `DESIGN.md` during the
> M6 close-out docs cleanup. `DESIGN.md` describes the product **as built**;
> this file is the historical design record for the milestone and may
> describe intent that later work changed. Check `DESIGN.md` first.

---

### M6.3b — Open WebUI durable config & control-plane reconciliation (design)

**Decision (2026-10):** Invert the M6.3a authority model. Open WebUI's own config store — a per-key `config` table in the existing OWUI Postgres database — becomes the **source of truth** for OWUI settings; env vars drop to **bootstrap-only** (bootstrap set below). The CP stops asserting policy through `--env-file` + recreate-on-drift and becomes an **API reconciler**: it drives OWUI's admin HTTP API through a thin stdlib client, enforcing the Lemonseed intent exactly as declared in a **versioned policy manifest** (`control-plane/owui-policy.json`) shipped read-only in SquashFS. `ENABLE_PERSISTENT_CONFIG=False` (we were opting *out* of the upstream default) is dropped, and most of `_POLICY_ENV_KEYS` / `_policy_env_ok` goes away. Numbered **M6.3b** because it completes M6.3a's policy-seeding work; sequenced after M6.5.

**Why a manifest (legibility requirement):** the CP↔OWUI contract must be inferable from one declarative file, not scattered through `modules.py` logic. The manifest maps the OWUI API surface and declares how the CP treats each part of it — and every entry carries a human-readable `reason` that surfaces in the portal, so an admin browsing OWUI config can see what Lemonseed manages, in which mode, and why.

**Why the flip is safe (migration property):** the `config` table is **empty on every deployed appliance today** — persistence has always been off, so OWUI never wrote a row and GUI edits mutated an in-process dict lost at restart. On the first boot after the flip OWUI seeds the whole table from its env-derived defaults (which are the values running boxes already use), so existing appliances upgrade invisibly and behavior is unchanged. (Upstream v0.11.0 `models/config.py`: `key` TEXT PK + `value` JSON + `updated_at`; `seed_defaults()` inserts **only missing keys**; from that boot on, DB beats env for every key.)

**Why no library:** `whogben/owui_client` matches our pin (0.11.0) and its docs site is a handy API reference, but it is `async` — wrong shape for our threaded stdlib CP — and a runtime dep would need Nix packaging for no gain over a small `urllib` client. Build our own; precedent is `lemond_client.py`.

#### Bootstrap set vs durable config

Keys that must stay env are those OWUI reads at import time (before it can query config; `env.py`) or that secure the very channel config is managed over. Only these remain in `enrich_open_webui_secrets()` and in the drift check (`_policy_env_ok` → `_bootstrap_env_ok`):

| Env key | Why it stays env |
|---------|------------------|
| `DATABASE_URL` | Needed before a DB connection exists |
| `WEBUI_SECRET_KEY` | Hard requirement with auth on; signs session JWTs + API keys |
| `WEBUI_AUTH`, `WEBUI_AUTH_TRUSTED_{EMAIL,NAME,GROUPS,ROLE}_HEADER` | Identity plumbing wired at middleware import time |
| `OFFLINE_MODE` | Suppresses import-time network calls before config is available |
| `WEBUI_NAME`, `ENABLE_VERSION_UPDATE_CHECK` | Read at import; absent from the DB `DEFAULT_CONFIG`, so no manifest entry can back them |

Everything else in today's env payload (`ENABLE_*` toggles, `USER_PERMISSIONS_*`, `DEFAULT_MODEL_*`, `OPENAI_API_BASE_URL(S)` + keys, …) becomes manifest-managed DB config. (The one-shot reset is **not** an env hatch: the portal reset runs as the Phase-4 stop→truncate→start→reconcile window.)

**Detecting a stale container (`_bootstrap_env_ok`).** The floor check requires exact match on the bootstrap keys **plus a CP-owned generation marker** (`LEMONSEED_OWUI_ENV_GEN=2`) that only our env-file writes; bump the value whenever the floor changes. An earlier design — recreate when *retired* keys are present in `docker inspect` env — is **impossible**: the OWUI v0.11.0 image bakes `OLLAMA_BASE_URL=/ollama`, `OPENAI_API_KEY=`, `OPENAI_API_BASE_URL=` and `WEBUI_SECRET_KEY=` into its `Config.Env`, and `docker run --env-file` merges over image ENV, so those keys are never absent (proved itself with an infinite blue/green recreate storm on QEMU). **Blue/green stays** for image-pin bumps and bootstrap-env changes — but env drift on DB-managed keys must **never** force a recreate again; stomping DB policy via env is exactly the bug class this milestone removes.

#### Interaction modes (manifest semantics)

Two orthogonal axes: **mode** (how much we own) applies per kind of config entity; **enforcement** (whether we merely *advertise* ownership or also *block* writes) applies per section. `selective_management` is meaningful only for **collections** (sets of entities: models, users, groups) — a scalar (single dotted key) has no notion of extra entries.

| Kind | Mode | Semantics |
|------|------|-----------|
| Scalar | `initial_setup` | Delivered as env at first boot; never written after. User GUI edits stick |
| Scalar | `managed` | We own the value; re-asserted on cadence. GUI edits get reverted (labeled in portal) |
| Collection | `initial_setup` | Seeded once (first boot / if undefined); the collection is the user's thereafter |
| Collection | `selective_management` | We own *our* entities, matched by the kind's ownership key (models: CP-defined id); all other entities untouched; entities claiming our reserved *name* namespace are swept with a portal log entry |
| Collection | `full_management` | We own the intent **within the kind's declared scope**: create missing, fix variation, delete extras (users' scope deliberately excludes deletion — see Admin & user sync) |

**Ownership by CP-defined id, namespaced by reserved name** — verified in v0.11.0: `ModelForm.id` is a **client-supplied string stored verbatim** (create rejects duplicates with `MODEL_ID_TAKEN`; the validity check is just non-empty + ≤256 chars) — not a server-assigned UUID. Owned models therefore match by **CP-chosen id** (`lemonseed-ai`, `lemonseed-ai-filtered`): the ids cannot churn under us because *we* write them, and a wiped/reseeded DB makes our entities *missing* → the reconciler re-creates them under the same ids instead of orphaning ownership. Display names are a reserved namespace too (`^Lemonseed.*$`, case-insensitive): an OWUI-UI-created model claiming a reserved name is removed by the reconcile namespace sweep with a portal-log entry — path-only nginx guards cannot see request bodies, so request-time blocking waits for a body-aware mechanism (§11 backlog).

**Cadence** bounds re-assertion of `managed` scalars so the reconciler never stomps user intent on a timer:

| Cadence | Re-assert when | Day-one key class |
|---------|----------------|-------------------|
| `at_boot` | Every time an OWUI slot goes healthy | Security / egress / identity / integration wiring (auth toggles, external-connection posture, lemond URL + key) |
| `on_upgrade` | Only on manifest version bump | Preferences a user may reasonably change between releases |
| `manual` | Only on an explicit portal action | Anything where surprise is worse than staleness |

**v0.11.0 gap that shapes `initial_setup`:** OWUI's startup calls `seed_registered_defaults()`, which **writes every known key into the table at first boot**, not just keys an admin touched. So `initial_setup` cannot mean “POST once after boot” — by then every key is already “defined.” For that key class `initial_setup` means “deliver the value as **env at first boot**” (env seeds the row); the reconciler backfills with `POST /api/v1/configs/import` only if the row is missing when first observed.

#### Manifest sketch — `control-plane/owui-policy.json`

JSON (not YAML): the CP is stdlib-first and `json` is a zero-dependency parser. Shipped read-only with the release; `owui.min_version` pins the API shape it assumes.

```json
{
  "version": 1,
  "owui": { "min_version": "0.11.0" },
  "settings": { "entries": {
    "web.search.enable": {
      "value": false, "mode": "managed", "cadence": "at_boot",
      "reason": "No web-search sidecar yet — egress stays off until SearXNG lands"
    },
    "openai.api_base_urls": {
      "value": { "derived": "lemond_base" }, "mode": "managed", "cadence": "at_boot",
      "reason": "Chat traffic goes to lemond and nowhere else"
    },
    "ui.default_pinned_models": {
      "value": ["lemonseed-ai"], "mode": "initial_setup",
      "reason": "First-boot default only — pinning models is yours after that"
    }
  } },
  "entities": {
    "models": { "mode": "selective_management", "own_by": "id", "own_ids": ["lemonseed-ai", "lemonseed-ai-filtered"], "reserved_name_regex": "^Lemonseed.*$" },
    "users":  { "mode": "full_management", "source": "cp.users", "scope": ["membership", "is_admin"], "on_removed": "propagate the explicit CP member delete (OWUI delete wipes account + chats)" },
    "groups": { "mode": "full_management", "source": "cp.groups" }
  },
  "guards": [
    { "path": "POST /api/v1/auths/add", "reason": "Household accounts are created in Lemonseed" },
    { "path": "DELETE /api/v1/users/.*", "reason": "Household accounts are removed in Lemonseed" },
    { "path": "POST /api/v1/configs/connections", "reason": "Writes direct.enable (direct model endpoints), which Lemonseed keeps off" },
    { "path": "POST /openai/config/update", "reason": "Day one: exactly one model connection (Lemonade); OWUI's connection editor rewrites the whole set = family-data egress" },
    { "path": "POST /ollama/config/update", "reason": "No Ollama on the appliance; ollama.enable stays off" },
    { "path": "POST /api/v1/users/%CP_OWUI_ID%/update", "reason": "The “Lemonseed Control Plane” account is not editable from OWUI" },
    { "path": "DELETE /api/v1/users/%CP_OWUI_ID%", "reason": "The “Lemonseed Control Plane” account is not removable from OWUI" }
  ],
  "api": {
    "config.export": "GET /api/v1/configs/export",
    "models.sync": "POST /api/v1/models/sync"
  }
}
```

- `value` sources: `const`, `secret:<KEY>` (from module secrets), `derived:<name>` — the resolver registry in code stays tiny (today: lemond base URL, lemond API key).
- `guards` are the enforcement axis, each with its own `mode`: `advertise` (portal card only) or `enforce` (nginx `403`, rendered in 4B). **Manifest v1 ships all seven as `advertise`** — the day-one enforce set is account-list writes, the connection writers and the service-account id/delete, and it lands with the 4B nginx rendering so a guard is never declared before it can be tested. User settings and API-key endpoints stay open, and the is-admin bit is reconcile-enforced: an nginx guard cannot inspect `POST /users/{id}/update` bodies without also blocking the name/email edits that belong to users. Guard paths support `%NAME%` placeholders resolved by the CP when rendering the nginx include — `%CP_OWUI_ID%` is the service account's OWUI id, a defensive second lock behind the primary-admin immunity OWUI already enforces server-side.
- **Guard paths must match how OWUI actually mounts its routers** (verified v0.11.0): the `openai` and `ollama` routers are mounted at `/openai` and `/ollama` with **no `/api/v1` prefix**, and `POST /openai/config/update` — not `POST /api/v1/configs/connections` — is what writes `openai.enable` / `api_base_urls` / `api_keys` / `api_configs` and clears the model caches. `POST /api/v1/configs/connections` writes only `direct.enable` + `models.base_models_cache`. The CP talks to the slot ports directly, so guards never block the reconciler.
- `api` maps logical names → paths so the client hardcodes no routes: an OWUI pin bump becomes a manifest diff, not a code hunt.
- `reason` strings surface as portal tooltips — this *is* the legibility requirement.
- **Per-key values are deliberately not decided here.** Each dotted key gets its mode/cadence/value when we implement that section; the decisions accumulate in the register table in this section.

**Policy decision register** (filled per method during implementation — the framework above, not invented values, is the deliverable here):

| Config area / collection | Open decision | Owner of call |
|--------------------------|---------------|---------------|
| `openai.*` wiring (lemond URL/key) | managed/`at_boot` (presumed) | Wiring — presumed safe, confirm at implementation |
| `openai.api_configs` (model connections) | **Decided 2026-10:** managed/`at_boot`, exactly one connection (lemond); OWUI-admin-added connections are a later extension (backlog) | — |
| `web.search.enable`, `image_generation.enable`, community sharing | managed/`at_boot` until sidecars (presumed; mirrors M6.3a) | Confirm at implementation |
| `user.permissions` (default perms) | managed vs `initial_setup`; day-one intent: `features.api_keys` **on** for members so per-user OpenAI-compatible endpoints stay self-service | Per-method decision |
| `ui.default_models` / `ui.default_pinned_models` / `ui.model_order_list` | `initial_setup` vs `on_upgrade` | Per-method decision |
| `models` collection | `selective_management` vs `full_management` once friendly names land (LiteLLM) | Per-method decision |
| `users` / `groups` collections | **Decided 2026-10:** `full_management` scoped to membership + is-admin only; the loop never deletes accounts; user settings / per-user API keys are user-owned | — |

#### Day-one interface (CP → OWUI admin API)

**Client:** `control-plane/owui_client.py` — thin `urllib.request` wrapper; API-key auth; per-call timeouts; typed `{ok, status, data, error}` returns; broad exception capture confined to that boundary.

**Service identity:** the CP provisions a dedicated OWUI account — email `lemonseed-cp@localhost`, display name **“Lemonseed Control Plane”** (v0.11.0 cannot hide a user from the admin user list, so the deterrent is an unmistakable name + guards on its id) — via `POST /api/v1/auths/signin` **direct to the slot port** (`127.0.0.1:18080`/`18081`, bypassing nginx; the trusted-email middleware auto-provisions unknown emails at sign-in), then `POST /api/v1/auths/api_key` mints an `sk-` key stored in module secrets. The CP never borrows a household user session. Provisioning order is load-bearing: the reconciler signs the service account in at the slot's **first healthy**, before any household sign-in — OWUI exempts its primary admin (first-created user) from modification by other admins, so being first makes the service account tamper-proof from the OWUI UI (deletes of the primary admin are refused outright) and keeps every household admin demotable through the API. Config gets `auth.enable_api_keys=True`, and the key is restricted via `auth.api_key.endpoint_restrictions` + `auth.api_key.allowed_endpoints` to the manifest's path set. (v0.11.0 has **no** `ADMIN_API_KEY`; per-user `sk-` keys are the only machine-credential path.)

**Day-one methods** (paths verified against the v0.11.0 tag):

| Client call | Endpoint | Notes |
|-------------|----------|-------|
| `health` | `GET /health` · `/ready` · `/health/db` | Bypass session middleware; gates reconcile |
| `config_export` | `GET /api/v1/configs/export` | Full key→value map; feeds portal tree + drift diff |
| `config_import` | `POST /api/v1/configs/import` | Upsert of dotted-key map; the write path for `managed` scalars |
| `config_namespace` | `GET /api/v1/configs/namespace/{ns}` | Cheap per-section read for the portal |
| `models_admin_list` | `GET /api/v1/models/export` | Complete managed-model list (admin) — the reconciler's ground truth |
| `models_list` | `GET /api/v1/models/list` | Paginated **access-filtered** view; portal-facing only, never reconcile ground truth |
| `model_create / model_update / model_delete` | `POST /api/v1/models/create` · `/model/update` · `/model/delete` | Per-entity writes for owned ids |
| `models_sync` | `POST /api/v1/models/sync` | Admin-only **full replace**: upserts the payload and deletes everything not in it. Allowed only when the kind is declared `full_management` |
| `users_all` | `GET /api/v1/users/all` | Reconcile ground truth |
| `user_update` | `POST /api/v1/users/{user_id}/update` | Accepts `role`; role change also disconnects socket sessions |
| `user_delete` | `DELETE /api/v1/users/{user_id}` | **Never called by the loop** — only the portal member-delete lifecycle handler; true wipe (groups, chats, user, auth rows in one transaction). OWUI refuses deleting its primary admin |
| `user_create` | `POST /api/v1/auths/add` | v0.11.0 has **no** `POST /api/v1/users/create` |
| `api_key_rotate` | `POST` / `DELETE` `/api/v1/auths/api_key` | Rotate the CP key |
| `groups_*` | `GET /api/v1/groups/` · `POST /create` · `/id/{id}/update` · `/id/{id}/users/add` · `/id/{id}/users/remove` · `DELETE /id/{id}/delete` | Group collection lifecycle |

**Reconcile triggers:**

1. OWUI slot first healthy after boot, recreate, or pin bump.
2. Manifest or CP policy change (post-`lemonseed-update`) → re-assert `on_upgrade` entries.
3. Identity change events (M6.2b user/group mutations) → push `users` / `groups` collections.
4. Portal “reset chat interface settings” → the maintenance-window sequence (stop slot → drop **only** the `config` table → start → full reconcile); it never runs against a live slot — see Portal visualization & write guards.
5. Supervisor tick → **audit-only** drift report into module state (writes nothing).

Reconciliation is keyed to the **module**, not the slot, under one non-blocking lock: blue/green slots share the same Postgres DB, so two slots reconciling concurrently through the shared tables is exactly what must not happen. The pre-cutover health gate runs an **audit-only** drift check on the candidate slot (config inherits through the shared DB); writes go through the active slot under the module lock. Module state recorded in CP Postgres: `manifest_version`, `owui_version`, `policy_hash`, `last_reconciled`, `last_error`, and the latest drift summary.

#### Admin & user sync

- **Elevation already works at sign-in:** the trusted role/groups headers are honored **only inside `POST /api/v1/auths/signin`** (v0.11.0) — it auto-provisions by email, syncs groups by name, and sets `admin`/`user`/`pending`. So any Lemonseed `admin`-group member lands as an OWUI admin on first sign-in. This is **sign-in only**; there is no per-request role re-sync — in-flight JWTs keep stale claims until expiry.
- **Demotion must be pushed:** when a user leaves the `admin` group the CP immediately calls `POST /api/v1/users/{id}/update` (role `user`, which also disconnects their socket sessions) rather than waiting out JWT lifetime.
- **The reconcile loop never deletes OWUI accounts; explicit CP member deletion propagates immediately.** v0.11.0 has no account-disable concept (`UserUpdateForm` is role/name/email/password; `is_active` is presence, not enablement), so day-one policy is simple: deleting a member in the Lemonseed portal calls OWUI's `DELETE /api/v1/users/{id}` in the same lifecycle handler — a true wipe (groups, chats, user, and auth rows in one transaction), which is also the right default under right-to-erasure regimes. The loop itself stays additive-only (create/adopt + role push): an OWUI account missing from CP identity raises a drift-report entry, never a loop-side delete — one bad identity read must not mass-delete a household. Tombstoned deletion with a 7-day undelete window is backlog (§11) and must be weighed against wipe-within-N-days rules before adoption. Users auto-create on first trusted-header sign-in; the CP pre-creates via `POST /api/v1/auths/add`; there is no OWUI password path (trusted-email bypasses password checks; the CP never sets OWUI passwords).
- **User-owned settings stay user-owned:** UI preferences, status, and per-user API keys — the self-service OpenAI-compatible endpoint access — are never written or guarded by the reconciler. The enabling scalars (`auth.enable_api_keys`, default `user.permissions` with `features.api_keys` on) are Lemonseed-managed; the keys themselves are the user's.
- OWUI `admin` here = trusted household power-user surface: those users reach OWUI's own admin UI/API (config editor, model workspace). Login remains CP-gated at nginx; the egress implications of that surface are in the risk table.

#### Portal visualization & write guards

- **`GET /_lemonseed/api/owui/config`** (admin-only, joined in `owui_portal.build_view`) is the **diagnostic** payload: per-entry classification — **Lemonseed-managed** (+ `reason`), **Seeded once** (`initial_setup`), **Yours — Lemonseed won't touch** — a per-namespace rollup (`ours` / `mixed` / `yours`), entity cards (models / users with mode, owned ids, reserved-name regex) and guard cards with an explicit `enforced` flag so "advisory today" is never mistaken for "blocked". Namespace = OWUI's **top-level** config segment (`web`, `openai`, `auth`…): two segments shredded the small namespaces into one-row groups, and OWUI's own admin UI groups by the same segment. Values are **masked at serialization** (`configs/export` is unredacted, so this payload is the redaction point): `secret`-kind keys collapse to `***`, structured values keep their shape with secret leaves masked, and unmanaged secret-looking leaves (`*.token`, `*.auth_token`, `*.bearer_token`, `*.client_secret`, …) are masked too. QEMU smoke still asserts this payload (classification, rollups, one-connection shape, masking, guard state) even though the portal stopped rendering it — see §11 *Ownership report as a build artifact*.
- **The Chat panel is one box with two factual Status rows, then Controls** (second and third bare-metal reviews). One `Chat` panel. **Status row 1** names the software and its version (`Open WebUI v0.11.0`) beside the container in **docker's own words** (`Up 59 minutes (healthy)`, `Exited (137) 3 minutes ago`) from a single `docker inspect`, falling back to the CP lifecycle word where docker cannot answer; the version prefers what OWUI reports about itself and falls back to the pinned image tag, never a registry path. **Status row 2, Lemonseed configuration**: `updated 2026-10-09 05:05 UTC` when the engine's last reconcile reported ok, else **needs attention** / **not applied yet** in red with `last_error` as hover text — one UTC timestamp format (`YYYY-MM-DD HH:MM UTC`) shared by the server render and the panel JS. Pills are for **problems only** (`could not be applied N`, `changed in Open WebUI since boot N`) and the strip is `hidden` when empty, because an empty flex box still occupies a line. **Controls**: **Apply Lemonseed settings now**, **Reset chat settings**, **Refresh status** (it re-reads the config view; the bare label did not say so), and the reset progress banner. Reset confirmation is one short word matched case-insensitively (`reset`), with prompt copy on a single line because `window.prompt` wraps badly. **Removed, not collapsed**: the per-key ownership tree, entity/guard cards, machine pills (policy hash, snapshot, key counts), the `path /?embed=1` row, the deep-link bridge into OWUI admin routes, and the collapsed **Advanced details** box that had replaced them — its three facts (version, image, reconcile time) became Status rows, where they are actually read. Rule going forward: this panel shows only what a home admin can **act on**. **No DOM injection into OWUI** (locked M6 architecture).
- **The `API` panel** is the raw-endpoint disclosure, collapsed under **System API Data** (it had no heading at all and its summary said *Diagnostics*). It stays admin-facing but developer-shaped on purpose; the admin-facing thing that belongs here is the **OpenAI-compatible endpoint** toggle → §11.
- **Guards (the enforcement axis)** are nginx denies (`403`) on the method+path pairs declared in the manifest's `guards`, applied to OWUI-bound traffic. Anything reaching OWUI *through nginx* is by definition not the CP — the CP talks to the slot ports directly — so “CP allowed, UI blocked” falls out for free. Blocked XHRs will surface as errors inside OWUI's own admin UI; that is the intended draconian behavior, and the portal badge explains why. The `403` body is therefore **JSON with a `detail` field** (`{"detail": <hint>, "lemonseed": "managed-by-lemonseed", "hint": <hint>}`) served under `application/json` — OWUI renders `error.response.data.detail` and otherwise shows its own generic “Server connection failed”, which hides the reason (found on bare metal); `default_type application/json` is set on the OWUI catch-all location because nginx rejects it inside `if`. Day-one enforce (4B): the connection writers `POST /openai/config/update` + `POST /ollama/config/update` (+ `POST /api/v1/configs/connections` for the `direct.enable` valve) and the service-account `update`/`delete` — **5 enforce**. The account-list guards `POST /api/v1/auths/add` and `DELETE /api/v1/users/.*` ship **advertise-only** until M6.2b owns the user list. Explicitly **not** guarded: per-user settings and API-key endpoints, the model workspace, and any body-aware endpoint — the is-admin bit is enforced by reconciliation + drift reports instead. Everything else ships **advertise-only** until usage tells us more.
- **Reset is a maintenance window, never live.** Dropping the `config` table under a running OWUI degrades every key read to its upstream default mid-request — including `auth.enable_api_keys` (default `false`), which would silently invalidate the CP's own `sk-` key and serve a half-configured UI to logged-in users. So “reset chat interface settings” reuses the blue/green machinery: confirm in the portal → nginx serves the Getting-Ready page → stop the OWUI slot → `TRUNCATE config` (transactional, the `config` table only) → start the slot → wait for `/health/db` + `/ready` → full reconcile (managed `at_boot` + `initial_setup` backfill — post-flip, env no longer carries those values, so only the manifest restores them) → cut back over. The user-visible worst case is a brief maintenance page; nothing half-configured ever serves.

#### Risks & open gaps

| Risk / gap | Notes & mitigation |
|------------|--------------------|
| OWUI admins can add external connections | Durable `openai.api_configs` means an OWUI admin can point chat at an external API = **family-data egress**. The writer is `POST /openai/config/update` (with `/ollama/config/update` analogous); `POST /api/v1/configs/connections` only flips `direct.enable`. Product call: day-one guard on the writers vs grant power users — the view makes the managed connection visible either way |
| GUI-vs-CP write conflicts | Edits to managed keys revert on next cadence. Cadences kept coarse (`at_boot` only for security/egress/wiring); every managed key shows its `reason` in the portal. OWUI-UI role edits survive until the next boot/identity reconcile (the shared update endpoint can't be path-guarded without blocking user-owned fields); the drift report surfaces pending corrections |
| Removed-user semantics | Day one: portal member-delete propagates an immediate OWUI delete (account + chats hard-wiped); the loop itself never deletes. Deferred: tombstone + 7-day undelete (§11 Backlog) — evaluate against right-to-erasure wipe deadlines |
| `models/sync` is destructive | Upserts payload + deletes everything not in it; reachable only for kinds declared `full_management` — day-one models are `selective_management`, so sync stays unused until we own the whole collection |
| Secrets move into config rows | `openai.api_keys` + connection secrets become DB values: visible in `/export` (portal masks `secret:*` entries) and included in DB backups (already true for the whole OWUI DB) |
| Young upstream API | The per-key `config` table, missing `ADMIN_API_KEY`, and sign-in-only role sync are all recent upstream changes. An OWUI pin bump is an **API-compat event**: run the reconciler's contract tests before landing the pin in the rebase pipeline |
| Trusted-header spoofing on `lemon` | Any container on the `lemon` network can `POST /signin` to an OWUI slot port with forged `X-Forwarded-*` (pre-existing M6.2 property). The CP itself uses `sk-` keys, not headers; future network isolation or SCIM would close this |
| Forward-only DB | OWUI alembic migrations run against the shared DB at startup; an A/B rollback after a newer OWUI migrated is unsafe (pre-existing exposure — chat/users tables already share this). Mitigation: the reset action drops the `config` table only |
| SCIM deferred | v0.11.0 ships `ENABLE_SCIM` / `SCIM_TOKEN` provisioning; considered and deferred — trusted headers + pushes cover household scale, and SCIM would *replace*, not extend, the current identity path |

#### Implementation

1. `control-plane/owui_client.py` — stdlib `urllib` client per the day-one table; modeled on `lemond_client.py`.
2. `control-plane/owui_policy.py` — manifest load/validate, resolver registry (`const` / `secret` / `derived`), reconcile planner (diff desired vs `config_export` + entity lists), drift-report generator.
3. `control-plane/modules.py` — reconcile worker inside the existing supervisor: non-blocking module-keyed lock, the triggers above, module-state fields in CP Postgres; the existing “recreate after 5 failed probes” supervisor behavior stays.
4. `enrich_open_webui_secrets()` shrinks to the bootstrap set; `_policy_env_ok()` → `_bootstrap_env_ok()`. DB-managed keys are removed from the env payload even though leaving them would still be harmless as first-boot defaults — the env file should show only what env actually governs.
5. `nixos/modules/gateway.nix` — nginx `guards` snippet rendered from the manifest (deny matched method+path to OWUI upstreams).
6. Portal — the **Chat** panel (Status + Controls): container status in docker's own words, one settings-applied pill with actionable drift counts, **Apply** / **Reset chat settings** / **Refresh**, and an advanced block limited to version + image + last reconcile time; “reset chat interface settings” drives the stop → truncate → start → reconcile maintenance sequence; the member-delete action propagates an OWUI `DELETE /api/v1/users/{id}` in the same handler (its only caller). The ownership tree, entity cards and guard cards render nowhere in the UI.
7. CP endpoints under the established namespace: `GET /_lemonseed/api/owui/config`, `POST /_lemonseed/api/owui/reconcile`, `POST /_lemonseed/api/owui/reset-config`.

#### Testing

| Tier | Target | Command |
|------|--------|---------|
| **A — unit** | Manifest load/validate; resolver registry; reconcile planner diffs (create/fix/delete/skip); mode semantics per kind; cadence gating; guard rendering; `owui_client` against mocked HTTP | `make test` (OWUI mocked; no live container) |
| **C — QEMU** | Post-flip: assert config **through the OWUI API** (what the reconciler wrote), not `docker inspect` env; **upgrade-invisibility** — first-boot seed equals today's M6.3a env values; portal config endpoint shape (badge + rollup join); guard live — same call direct-to-slot `200`, via nginx `403`; reset window — only the Getting-Ready page serves during stop→truncate→start→reconcile, and post-reset config matches the manifest | `make build-qemu && make test-qemu` |

#### Acceptance checklist

- [x] `ENABLE_PERSISTENT_CONFIG` flipped; existing appliances upgrade invisibly (first-boot seed == today's env defaults) — proven on a pre-flip QEMU disk; smoke now asserts `configs/export` seeded values + a marker key surviving container recreation (2026-10-06)
- [x] Manifest ships in the image; every key moved out of env has an entry with mode + `reason` (values from the decision register) — manifest v1 (24 settings + models/users entities) ships beside `owui_policy.py`; smoke asserts a converged reconcile against it (2026-10-07)
- [x] CP service account provisioned; `sk-` key minted and endpoint-restricted to the manifest's path set — `service_ready` + `key_ready` asserted in smoke; the `auth.api_key.*` restriction entries are manifest-managed (mint ordered after settings apply to avoid the fresh-install 403 deadlock)
- [x] Reconcile runs on first-healthy + manifest bump; periodic audit-only drift lands in module state — two-run QEMU proof: boot `at_boot` run reverts the previous run's out-of-band flip (`fixed`), the +300 s audit tick reports a fresh flip as `pending` without fixing it (2026-10-07 ×2)
- [ ] Admin sync green: elevation via sign-in trusted headers; demotion pushed via `users/{id}/update`
- [x] Portal tree with ownership badges, tooltips and namespace rollup (`GET /_lemonseed/api/owui/config`, admin-only) — `owui_portal.py` + the settings-panel tree; smoke asserts badges, the one-connection shape, key masking and the model/user/guard cards against real OWUI (2026-10-07)
- [ ] Day-one guards (account create/delete + connection writers + service-account id) enforced at nginx — declared `advertise` in manifest v1, enforcement lands with the 4B rendering
- [ ] Reset runs as a stop→truncate→start→reconcile window with no half-configured serving state; the reconcile loop never deletes OWUI accounts
- [ ] `make test` green; `make test-qemu` green with API-based config assertions

**Backlog:** moved to §11 (consolidated) — group *Open WebUI integration*.

