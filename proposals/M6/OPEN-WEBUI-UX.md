# M6 — Open WebUI as the primary household UX (design record)

> **Milestone design artifact.** Moved out of `DESIGN.md` during the
> M6 close-out docs cleanup. `DESIGN.md` describes the product **as built**;
> this file is the historical design record for the milestone and may
> describe intent that later work changed. Check `DESIGN.md` first.

---

### M6 — Open WebUI as primary UX (decided)

**Decision (2026-08):** **Open WebUI** is the single primary household interface. A separate NanoClaw-style agentic messaging UI is **deferred** — Open WebUI can deliver a sufficient “light agentic” experience (automations, tasks, sub-agents, channels) without a second front door.

**Rationale:** Familiar chat UX + upstream feature velocity + strong OpenAI-compatible backend fit with `lemond`. NanoClaw-style stacks are harder to onboard and overlap with Open WebUI’s agent/task/channel features. [Conduit](https://conduit.mobile/) provides a credible path to mobile later because it targets Open WebUI APIs.

| Decision | Primary module | Deferred |
|----------|----------------|----------|
| **Locked** | Open WebUI (Docker) | NanoClaw-style standalone agentic UI |

**Locked architecture:**
- Inference stays **headless `lemond` in SquashFS** on `127.0.0.1:13305` (API key from CP).
- Open WebUI runs as a **Docker module** managed by the control plane; nginx :80 is the only LAN entry.
- **Primary UX** is a **Lemonseed app shell** (CP-served) that embeds Open WebUI in an iframe — not DOM injection into OWUI, and not “OWUI as the whole product.”
- **Module and CP durable state** live in shared **PostgreSQL** (and **Valkey** where needed) — not bind-mounted SQLite files inside containers.
- CP owns catalog, model load, `chat-ready`, module lifecycle, DB/credential provisioning, group/user accounts (Postgres), and the edge-auth gate at nginx (M6.2).

```
Browser / Conduit (later)
        │
        ▼
   nginx :80  ── routing + edge auth via Authelia (M6.2; modules stay auth-naive)
        │
        ├── /              → Lemonseed shell (document) or OWUI (iframe / ?embed=1)   [gated]
        ├── /auth/         → Authelia (sign-in UI; /auth/api/verify internal)
        ├── /settings/…    → control plane portal (admin, status)                     [gated; admin group]
        ├── /setup, /healthz, /api/{boot,status,…} → CP            (open; rest of /api/* gated)
        └── /*             → Open WebUI (assets, /api/v1, /c/…, websockets)           [gated]

Open WebUI (Docker) ──► nginx /_lemonseed/lemond/ ──► lemond 127.0.0.1:13305/v1
                     └──► PostgreSQL (+ pgvector)

CP ──► PostgreSQL (appliance + module metadata + user/group accounts)
       └──► derives users.yaml, gate snippets, module env/secrets (see §3)
Authelia (SquashFS, 127.0.0.1:9091) ──► users.yaml (file backend)
                                └─────► Valkey (sessions; key TTL → auto-GC)
```

**Shell chrome (M6.1):** thin brand bar, Chat / Modules nav, settings gear, load/storage meters; Open WebUI kept alive in a background iframe when switching tabs.
#### Shared data & module infrastructure

**Principle:** minimize **bind-mounted application state** in module containers. Chat history, OWUI config, embeddings metadata, CP module registry, and (later) user records belong in **managed databases** on user-data — not opaque host directory trees per container.

**Why not SQLite in modules:** module updates should use **start-new → health-check → retire-old** (blue/green). SQLite ties state to a single container filesystem and blocks running old and new instances against the same data concurrently. PostgreSQL (and Valkey) support shared clients, online backup, and transactional restore.

| Service | Role |
|---------|------|
| **PostgreSQL** | Primary durable store: Open WebUI, CP state, future modules; **pgvector** extension for embeddings/RAG |
| **Valkey** | Redis-compatible cache/queue/session scratch (exact use per module TBD) |
| **Object storage (later)** | Large blobs (uploads, generated images) may still use `/var/lib/lemonseed/` paths — but **metadata and indexes in Postgres** |

**Layout (direction):**
- Data services and app modules run on the user-defined Docker network **`lemon`**.
- Host-facing ports bind **only** to `127.0.0.1` (CP / nginx); never `0.0.0.0` or LAN NICs.
- Inter-container traffic uses Docker DNS names (e.g. `lemonseed-postgres`); host processes use localhost publishes.
- CP **provisions per-module** database + role + password; secrets injected at container start — not checked into config.
- **Backups:** Postgres `pg_dump`/basebackup on a schedule; Valkey snapshot policy TBD; both participate in preserve-on-reinstall user-data story (see backlog: backup/restore).
- **Bind mounts:** limited to secrets drop-ins, read-only config seeds, and **large media** — not primary OLTP state.

**CP owns container lifecycle (data plane + modules):** the control plane is the source of truth for which containers should exist and be serving. A CP supervisor (`data_services.ensure_data_worker` for Postgres/Valkey, `modules.ensure_module_worker` for Open WebUI) ticks on a ~10s loop and reconciles intent with reality: missing container → pull (registry-reachability preflight, cached, throttled error logging) → run with the exact pinned args; stopped container → start; running but port not serving after a grace window → recreate. Docker `--restart` policies are **not** relied on for recovery. The systemd oneshot units (`lemonseed-postgres`, `lemonseed-valkey`) are a best-effort boot accelerator only: `network-online.target` can be reached while DNS is still broken, so a failed `docker pull`/`run` at boot is expected to be healed by the supervisor on a later tick — the data plane must never be dead for the whole boot because of a one-shot race. Container state + last error is surfaced in `/api/data` (`postgres`/`valkey` `state` + `error`) so the portal can explain *why* chat is not ready (e.g. "Cannot reach the container registry") instead of failing silently.

**Module update strategy (blue/green):**
1. Pull new image tag.
2. Start **new** container against **same** Postgres/Valkey endpoints (new credentials or shared role per policy).
3. **Health-check** new instance (HTTP ready + DB migration success if applicable).
4. Flip nginx upstream to new instance.
5. Stop old container.

Failed health-check → old container keeps serving; appliance stays up.

#### Gateway & auth

**M6.1 (done):** **Fully disable Open WebUI authentication** — no OWUI login screen, no OAuth hooks inside the module. LAN access matches the bring-up trust model until M6.2 edge auth ships.

**M6.2 (design locked):** **CP-native login at the nginx edge** — full design below. **Do not** delegate OAuth/OpenID to containers; modules stay auth-naive behind nginx.

- Open WebUI container receives Lemonade URL + API key + Postgres/Valkey DSN via env/secrets from CP.
- No host network; no direct LAN exposure of `lemond`, Postgres, Valkey, or CP auth endpoints (all `127.0.0.1`-bound; login is only reachable via nginx `/login`).

#### M6.2 — Edge auth (CP-native login) (design)

**Decision (2026-09):** Replace Authelia with a **CP-native login** (signed session tokens, branded login page). Authelia 4.x hardcodes `https` requirements for `default_redirection_url`, `rd=` validation, and the `Secure` cookie flag — none configurable. The appliance is HTTP-only on the LAN; self-signed TLS produces a browser warning every new session (poor UX for less-technical users), and Let's Encrypt requires a user-owned domain (too much friction). Building a minimal login in the CP eliminates all https workarounds, gives us full control over UX/theming, and removes an external service.

**What we keep from the Authelia implementation (M6.2a, committed):**
- CP + Postgres is the **source of truth** for users, groups, password hashes (bcrypt). Schema: `users`, `groups`, `user_groups` (many:many, Linux-style).
- nginx **reloadable gate** pattern: `auth-gate.conf` (empty at boot; CP writes `auth_request` + `auth_request_set` lines and reloads nginx once the first admin exists).
- **X-Forwarded-\* hygiene:** identity headers set only from `auth_request_set` variables; nginx `proxy_set_header` overrides client-supplied values.
- **Admin bootstrap** via `/setup` (single path; first admin only while `admin` group is empty).
- **Lockout recovery** via installer reset marker (physical access only).
- **OWUI interstitial** (`error_page 502/503/504 → @owui_loading → CP shell`).

**What changes (M6.2 migration):**

| Aspect | Authelia (M6.2a) | CP-native (M6.2) |
|--------|-------------------|-------------------|
| Login page | Authelia's (white/black, unbranded) | CP-rendered, Lemonseed-branded |
| Session store | Valkey (Redis) with TTL | **Stateless signed token** in cookie (HMAC-SHA256) |
| Session GC | Valkey key TTL auto-expiry | **None needed** (stateless; token has embedded expiry) |
| Auth verify | `/_lemonseed/auth-verify` → Authelia `:9091` | `/_lemonseed/session-verify` → CP `:8080` |
| Login endpoint | `/auth/` → Authelia `:9091` | `/login` → CP `:8080` |
| Logout | `/auth/` → Authelia | `/logout` → CP `:8080` |
| Password hash | bcrypt (Authelia file backend) | bcrypt (CP/Postgres, already implemented) |
| TOTP | Authelia built-in (M6.3) | `pyotp` in CP (M6.3) |
| External service | `authelia.service` on `:9091` | **None** (CP handles everything) |
| https workarounds | 3 (X-Original-URL, default_redirection_url, nosecure) | **None** |

**Session token design:**
- **Format:** `base64url(payload).base64url(hmac_sha256(payload, secret))` where payload = `username|expiry_epoch|nonce`.
- **Secret:** 256-bit random, generated at first-admin bootstrap, stored in `/var/lib/lemonseed/session-secret` (0600). Regenerated on admin password reset (invalidates all sessions).
- **Cookie:** `lemonseed_session` — `HttpOnly`, `SameSite=Lax`, **no** `Secure` flag (HTTP-only LAN). Path `=/`. No user data in the cookie — just the signed token.
- **Expiry:** 1h inactivity (CP checks `Last-Activity` header or re-issues on each request), 10h absolute. Token embeds `expiry_epoch`; CP rejects expired tokens.
- **Verification:** `/_lemonseed/session-verify` (nginx `auth_request` target) reads the cookie, verifies HMAC + expiry, returns 200 + `X-Forwarded-User/Email/Groups` headers on success, 401 on failure.
- **Why stateless:** no session store to GC (addresses the "GC leftover records" concern), no Valkey dependency for auth, survives CP restarts, trivially horizontal-scalable if we ever need it.

**Gateway (nginx) integration (simplified from M6.2a):**
- `location /login` → CP `:8080` (branded login page + POST handler).
- `location /logout` → CP `:8080` (POST; clears cookie).
- `location = /_lemonseed/session-verify` → CP `:8080` (internal `auth_request` target; replaces `/_lemonseed/auth-verify`).
- **Remove:** `location /auth/` (Authelia proxy), `authelia.service`, `proxy_cookie_flags`, `X-Original-URL` https hack.
- **Keep:** reloadable gate pattern, `auth_request_set` for identity headers, `error_page 401 = @auth_unauthorized` (302 to `/login?rd=$request_uri`), OWUI interstitial.
- **Open (bypass):** `/healthz`, `/health`, `/setup`, `/login`, `/api/boot`, `/api/status`, `/_lemonseed/brand/`, `/favicon.ico`, `/_lemonseed/lemond/`.
- **Gated:** `/` (shell + OWUI), OWUI catch-all, `/ws/`, `/settings/` (+ admin group via CP check), remaining `/api/*`, `/logout`.

**Security requirements (login system):**

| Requirement | Implementation |
|-------------|----------------|
| **Credential hashing** | bcrypt (already in CP; `ps.bcrypt` in Nix pythonEnv). Argon2id is preferred but bcrypt is acceptable and already wired. |
| **MFA (TOTP)** | M6.3: `pyotp` (pure-Python, RFC 6238). **Required for `admin` group** accounts. Device remembered once MFA complete (session token includes `mfa_verified` flag; no re-prompt within session). |
| **Rate limiting** | CP tracks failed login attempts per source IP (in-memory sliding window, 5 min). After 5 failures → 15 min lockout for that IP. Response is always the same (see below). |
| **Cookie security** | `HttpOnly` (no JS access), `SameSite=Lax` (CSRF protection), no `Secure` (HTTP-only LAN). Cookie contains **only** the signed token — no username, no groups, no PII. |
| **Session expiry** | 1h inactivity / 10h absolute. Token embeds expiry; CP rejects expired tokens. Admin password reset regenerates the session secret (invalidates all active sessions). |
| **Vague errors** | All login failures return the same message: `"Invalid username or password."` No user enumeration. Rate-limit lockout returns the same message (no "too many attempts" hint). |
| **Admin group check** | Enforced by CP (not nginx `if` — which runs in the rewrite phase, before `auth_request` sets variables). CP reads `X-Forwarded-Groups` and returns 403 for non-admins on `/settings/`. |

**Accounts & CP surface (unchanged from M6.2a design):**
- Separate **users** and **groups** with a **many:many** mapping in CP Postgres: `users` (username PK, display name, email, password hash, disabled, reserved nullable `account_type`), `groups` (name PK), `user_groups` (join).
- **No singular "owner":** elevation = **`admin` group** membership. Default groups: `admin` + `users`.
- `GET/POST /api/auth` (`admin`-gated, M6.2b): user + group management. **Guardrail:** refuse to remove/disable the last enabled admin.
- `/api/status` gains `auth: {state, admins, users}` (no external service probe needed — CP knows its own state).

**Phases (revised):**

| Phase | Deliverable |
|-------|-------------|
| **M6.2 (migration)** | Remove Authelia. Add CP login/logout/session-verify endpoints + branded login page. Update nginx gate to point to CP. Remove https workarounds. Keep all M6.2a account/group/bootstrap/lockout logic. |
| **M6.2b** | `/api/auth` users & groups management + portal Users & Groups section. (Unchanged from prior plan.) |
| **M6.3** | TOTP MFA via `pyotp`. Required for `admin` group. Device remembered per session. |
| **M6.4** | AMD GPU Vulkan support. Mesa RADV ICD + `vulkaninfo` detection + `select_backend` runtime dispatch. Single production image covers NVIDIA (CUDA) and AMD (Vulkan). |

**Testing (revised):**
- **Unit** (`make test`): token sign/verify/expire, login POST (valid/invalid/rate-limited), session-verify endpoint, admin group check, bootstrap gate, reset-marker idempotency, `/api/auth` handlers. Subprocess/psycopg2 mocked.
- **QEMU smoke:** fresh box → gate empty → `POST /setup` (name + timezone + admin creds) → wait for `auth.state=enabled` → assert: unauth `GET /` → 302 `/login`; `POST /login` (valid creds) → 302 + `Set-Cookie` → `GET /` 200; `GET /settings/` 200 as admin; `POST /logout` → cookie cleared → `GET /` → 302 `/login`. `/healthz` + `/api/status` still open.

**Post-M6 backlog (not in M6.2):**
- **Cloud service / DynDNS:** `<random>.client.lemonseed.ai` FQDN + Let's Encrypt DNS-01 for remote access. Part of the CLOUD-SERVICE.md zero-trust roadmap (see `proposals/CLOUD-SERVICE.md`). Requires: cloud API, DNS provider integration, cert management, E2E encryption proxy. See `proposals/AUTH-TOTP.md` for TOTP-over-HTTP security analysis.
- **TLS at the gateway:** if/when the cloud service lands, the appliance will have a valid Let's Encrypt cert for its public FQDN. The local `.local` experience remains HTTP-only (LAN trust model). The CP-native login works over both HTTP and HTTPS without changes.

**Risks / accepted:**
- We are building a login system in-house. Mitigated by: keeping it minimal (~200 lines of new CP code), using well-understood primitives (bcrypt, HMAC-SHA256, RFC 6238 TOTP), and the security requirements table above. The CP already owns the user/group data — we're adding the session layer, not reinventing the entire auth stack.
- No SMTP / no email reset links → recovery is an admin CP reset (M6.2b) or the physical reset marker.
- Pre-admin window: gate off = today's LAN trust model; closes when the first `admin` is created via `/setup`.
- Stateful rate limiting is in-memory (CP process). A CP restart resets the counter — acceptable for a single-appliance LAN threat model (an attacker would need to sustain the attack across a CP restart).
- Docker modules can reach `/login` via `host.docker.internal:80` (first-party containers only) — accepted; the login page is inert without valid credentials.

#### Open WebUI container policy

| Area | Policy |
|------|--------|
| **OpenTerminal** | **Multi-user mode** — one container serves all household users (not per-user containers). |
| **Web search** | Use **SearXNG** or another self-hosted/external search backend; built-in Open WebUI web search is not good enough for product use. Evaluate [community web search improvement](https://openwebui.com/posts/web_search_acff6f71) for UX. |
| **Autocomplete** | **Disabled** — not worth token burn on appliance hardware. |
| **Automations** | **Enabled** — core “light agentic” path. |
| **Task management, sub-agents, timers** | **Enabled** where hardware supports the backing model (see model tiers). |
| **Memory** | **Enabled**, automatic, for all models. |
| **Workspace** | **Disabled**. |
| **Notes** | **Enabled**. |
| **Calendar** | **Enabled** — seed a shared **family/group calendar** by default at first setup. |
| **Channels** | **Enabled** — seed a shared **“everyone”** channel by default at first setup. |
| **Speech** | **Local only** — enable STT/TTS paths that run on-appliance; **no cloud speech dependencies**. |
| **Images** | **ComfyUI** workflows for creation and editing; **VRAM-gated** (ComfyUI has improved GPU/CPU sharing in recent versions — still policy-gated on small boxes). |

#### Model management (planner + Open WebUI)

Catalog/planner extends M5 tiering; CP pushes defaults into Open WebUI config.

| Hardware class | Models | Agentic features |
|----------------|--------|------------------|
| **Small** (&lt; 64 GiB VRAM) | **One** catalog model with good defaults (current M5 path: e.g. Gemma E2B on ~6 GiB GPU) | Automations, channels, notes, calendar — **no** heavy sub-agent/task stack unless model fits |
| **Large** (≥ 64 GiB VRAM) | **Fast** model for daily chat (e.g. Gemma MoE class) + **dense** model for deeper research / heavy agent work | Task management, sub-agents, timers on **dense** model tier |

**All models:** system prompts that establish identity (**“Lemonseed”**), role, and per-user context (who the user is in the household). CP or seeded OWUI config owns baseline prompts; user-specific layers added after auth lands.

#### Community plugins — evaluate before ship

Verify **license compatibility** and security before bundling or auto-installing any community plugin.

| Plugin | URL | Purpose |
|--------|-----|---------|
| SMB file operations | [openwebui.com/…/smb_file_operationstool_145f7e79](https://openwebui.com/posts/smb_file_operationstool_145f7e79) | LAN file share access |
| Spreadsheets | [openwebui.com/…/create_and_edit_spreadsheets_f2211b30](https://openwebui.com/posts/create_and_edit_spreadsheets_f2211b30) | Office-style docs |
| Secure mail agent | [openwebui.com/…/securemail_agent_99b0e788](https://openwebui.com/posts/securemail_agent_99b0e788) | Email integration |
| Data plot (Plotly) | [openwebui.com/…/data_plot_plotly_f375c4fc](https://openwebui.com/posts/data_plot_plotly_f375c4fc) | Visualization |

Default appliance image: **no** community plugins until reviewed; optional “enable in settings” or post-M6 module pack.

#### M6 phased deliverables

| Phase | Deliverable |
|-------|-------------|
| **M6.0** | **Done** — UX decision recorded (this section) |
| **M6.0a** | **Done** — shared PostgreSQL (+ pgvector) + Valkey; CP credential provisioning; CP state in Postgres; QEMU + hardware smoke |
| **M6.1** | **Done** — Open WebUI on Postgres; auth disabled; Lemonade wired; Lemonseed shell + blue/green; durable chat history |
| **M6.2a** | **Authelia (file backend)** edge auth at nginx `/auth/`; Valkey session store (TTL-GC); first-admin bootstrap via `/setup` (installer supplies only the lockout reset marker); identity headers; OWUI remains authless behind nginx |
| **M6.2b** | `/api/auth` users & groups management (user add / reset / disable / remove; group create / map) + portal Users & Groups section |
| **M6.3** | OWUI policy seeding (**M6.3a** done for env defaults); SearXNG sidecar; calendar/channel defaults |
| **M6.3b** | OWUI durable config (DB-authoritative) + CP reconciliation via the admin API from a versioned manifest — design below; SearXNG/calendar/channel defaults land as manifest entries + API writes instead of new env seeds |
| **M6.4** | Household smoke; `make test` green |
| **M6+** | Conduit mobile, ComfyUI VRAM gating, multi-model tiers, vetted community plugins |

**Sequencing:** M6.3a policy seeding and M6.2 edge auth are done. **M6.3b** (durable config + reconciliation) is next; **SearXNG**, channels/calendar, and ComfyUI wait until it lands, so their defaults (sidecar connections, shared-state toggles) ship as manifest entries + API writes rather than new env seeds.

**M6.3a (done):** Open WebUI env seeds via `enrich_open_webui_secrets()` with `ENABLE_PERSISTENT_CONFIG=False`: autocomplete off; automations on (incl. non-admin permission); workspace access off; notes/memory on; channels off until shared-channel seeding; web search / image gen / community sharing off until sidecars; `DEFAULT_MODEL_PARAMS` Lemonseed identity prompt + `function_calling=legacy` (v0.11 native tools caused empty KB/file queries); `DEFAULT_MODEL_METADATA` disables builtin_tools / knowledge / files until curated sources exist; `ENABLE_VERSION_UPDATE_CHECK=False` (with `OFFLINE_MODE`) to suppress update/What’s New noise. Shell/setup/installer use the lemon brand mark under `/_lemonseed/brand/` (installer: `/static/brand/`). Open WebUI image pin rebased to **v0.11.0** (latest stable at bump time). Shell **Getting Ready** bubble gates the OWUI iframe on **both** module `http_ready` and `chat.state=ready` (model pulled, loaded, and verified via a short Lemonade completion).

**Backlog:** consolidated into §11 (groups *Models & chat UX*, *Portal & platform UX*, and *Modules & infrastructure*). The two formerly-listed items — OWUI model/policy via API and the CP model picker — were promoted to M6.3b and delivered in M6.5; they now live only in the decision log.

#### M6 acceptance checklist

- [x] Written UX decision: Open WebUI primary; NanoClaw-style UI deferred
- [x] Shared Postgres (+ pgvector) + Valkey; minimal bind-mount module state
- [x] Open WebUI runs as Docker module on Postgres; `lemond` stays localhost-only
- [x] Open WebUI auth **disabled** (M6.1); nginx proxy auth deferred to M6.2
- [x] Blue/green module updates with health-check before cutover
- [x] Lemonseed shell at `/` embeds Open WebUI (supersedes portal “Open chat” CTA)
- [x] Policy defaults applied (autocomplete off, automations on, workspace off, etc.)
- [x] `make test` still green
- [x] No second co-equal “main” UI shipped by default (shell embeds OWUI)
- [ ] M6.2a: Authelia (file backend) + Valkey sessions behind the nginx `/auth/` gate; locked once the first `admin` exists
- [ ] Admin bootstrap: single `/setup` path (name + timezone + admin creds, synchronous); installer supplies only the lockout reset marker (`ESP/lemonseed-reset-admin` → `/var/lib/lemonseed/reset-admin`)
- [ ] Identity `X-Forwarded-*` sourced only from Authelia (client-supplied overridden); `/settings/` admin-gated
- [ ] M6.2b: `/api/auth` users & groups management + portal Users & Groups section
- [ ] QEMU auth phase green (redirect → sign-in → authenticated 200s; status endpoints stay open)

