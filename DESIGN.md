# Lemonseed Appliance — Design

Status: **living design** (M1–M5 done; **M6 in progress** — Open WebUI chosen as primary UX)  
Related: [Concept.md](./Concept.md) (product vision), [Requirements.md](./Requirements.md)

This document captures architectural decisions. It is the source of truth for implementation milestones. Product intent lives in Concept.md; this file translates that intent into buildable decisions.

---

## 0. Product framing (from Concept.md)

Lemonseed is a **local, multi-user home AI hub** — not a cloud client and not a DIY container toolkit. The OS appliance (read-only SquashFS, A/B updates, installer, control plane) exists so non-technical households can run open models privately on LAN hardware.

**Release 1 product surface (Concept):** chat (text; voice later), background workers/notifications, image gen/edit.  
**Release 2 (Concept):** short video, documents, speaker/voice appliance.

**Differentiator:** always-on LAN quality above phone/laptop on-device models; trusted sharing inside one household; open-source stack inspection; **no cloud AI dependency** for core chat.

**Stack intent (locked for M5 direction):**
| Layer | Choice |
|-------|--------|
| Inference backend | **Embeddable `lemond` in SquashFS** (headless; no Lemonade web/desktop UI). Built from source via Nix, not the Ubuntu release tarball. |
| First UX | **Open WebUI** (Docker module) — primary household chat + light agentic features; portal deep-links when chat-ready |
| Mobile (later) | [Conduit](https://conduit.mobile/) — open-source Open WebUI client; evaluate after LAN UX is solid |
| Shared data | **PostgreSQL** (+ **pgvector**), **Valkey** (Redis-compatible) — appliance-wide; modules get provisioned DB/role creds |
| Image gen | **ComfyUI** via Open WebUI workflows (VRAM-gated); not Lemonade llama-sd / sd-cpp |
| Agents / workers | **Light agentic** inside Open WebUI (automations, tasks, channels, timers) — not a separate NanoClaw-style front door |
| Brand | **lemonseed.ai** |
| Docker | Remains for **modules** (Open WebUI, etc.); **not** the primary Lemonade runtime |

**Hardware product goal (later device):** silent shelf appliance, 1–10 users, 64GB+ class SoC (Ryzen AI Max / NVIDIA Spark-class). **Software validates first** as an OSS machine image on off-the-shelf PCs before ODM packaging.

---

## 1. Goals and non-goals

### Goals
- Build a **repeatable, version-controlled** NixOS-based appliance image for **x86_64**.
- Produce a **minimal, read-only** SquashFS root plus matching `vmlinuz`.
- Run **systemd** as the root of control; Docker as the container platform; a **control plane** we own for orchestration and UI.
- Support **A/B root updates** with GRUB one-shot boot and lock-in on successful startup.
- Keep artifact layout **ready for detached signature verification** and **HTTP distribution**.
- Prefer **no initrd** when practical; keep initrd as an open design option if hardware bring-up requires it.
- Discover hardware capabilities at runtime and **fit open models** to those constraints without requiring cloud accounts.

### Non-goals (for now)
- ARM builds
- Legacy BIOS / MBR boot support (EFI-only assumption)
- Offline USB image payload (installer downloads from lemonseed.ai; offline option later)
- Installer WiFi AP / captive portal (Ethernet-only for M4)
- CI builders (local builds first)
- Secure Boot / TPM root of trust (later; detached sigs first)
- Shipping application container images inside the base SquashFS
- Writable overlay / tmpfs overlay on root
- Cloud model burst / anonymized cloud GPU relay (Concept “future”; not M5)
- Delegated auth inside modules (per-container OAuth / OpenID) — auth happens only at the nginx edge (M6.2, Authelia); Open WebUI native login **disabled**
- LUKS/encrypted user-data (deferred past M6; plan later)

---

## 2. Milestone plan

| Milestone | Deliverable |
|-----------|-------------|
| **M0 — Design** | This document |
| **M1 — Artifacts** | **Done** — Flake + Makefile → `root.squashfs`, `vmlinuz`, `initrd.img`, `manifest.json`, optional `.sig`; `build-production` / `build-qemu` / `run-qemu` / `clean` |
| **M2 — Bootable skeleton** | **Done** — LVM PV → initrd provisioner → btrfs `@etc`/`@var`; stub control plane; QEMU userdata disk + `qemu-clean` |
| **M3 — Partition & A/B tooling** | **Done** — `partitions.yaml` → `out/repart.d`; `boot/grub.cfg`; slot-aware root; `lemonseed-ab` + lock-in; `lemonseed-update` in-place updater |
| **M4 — USB installer** | **Nearly done** — install + portal + first-boot setup; polish remaining |
| **M4.1 — Preserve known user-data on reinstall** | **Done** — namespaced storage identity; preserve checkbox; layout-compatible reinstall |
| **M5 — Hardware-aware local chat** | **Done** — `lemond` + NVIDIA/CUDA; inventory + catalog planner + CP pull/load/pin + portal “Local chat ready”; unit tests + QEMU API smoke |
| **M6 — Open WebUI household UX** | **In progress** — M6.1 + M6.3a done; next **M6.2** edge auth (Authelia, file backend + Valkey sessions); SearXNG/ComfyUI after auth |
| **M6+ (later)** | Conduit/mobile polish, AMD/UMA profiles, heterogeneous GPU planner, encryption, offline install, Secure Boot/TPM |

**Immediate next step:** **M6.2a** — edge auth via **Authelia (file backend)** behind nginx at `/auth/`, first-admin bootstrap via `/setup` (the USB installer only supplies the lockout reset marker), Valkey session store. Multi-user / shared-LAN features (SearXNG, channels/calendar, ComfyUI) follow once the edge is authenticated.

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
| **M6.4** | Household smoke; `make test` green |
| **M6+** | Conduit mobile, ComfyUI VRAM gating, multi-model tiers, vetted community plugins |

**Sequencing:** M6.3a policy seed is done; **M6.2 edge auth is next**, before any multi-user / shared-LAN feature that assumes distinct people. **SearXNG**, channels/calendar, and ComfyUI wait until after M6.2 — they create shared durable state or widen the attack surface on an open LAN.

**M6.3a (done):** Open WebUI env seeds via `enrich_open_webui_secrets()` with `ENABLE_PERSISTENT_CONFIG=False`: autocomplete off; automations on (incl. non-admin permission); workspace access off; notes/memory on; channels off until shared-channel seeding; web search / image gen / community sharing off until sidecars; `DEFAULT_MODEL_PARAMS` Lemonseed identity prompt + `function_calling=legacy` (v0.11 native tools caused empty KB/file queries); `DEFAULT_MODEL_METADATA` disables builtin_tools / knowledge / files until curated sources exist; `ENABLE_VERSION_UPDATE_CHECK=False` (with `OFFLINE_MODE`) to suppress update/What’s New noise. Shell/setup/installer use the lemon brand mark under `/_lemonseed/brand/` (installer: `/static/brand/`). Open WebUI image pin rebased to **v0.11.0** (latest stable at bump time). Shell **Getting Ready** bubble gates the OWUI iframe on **both** module `http_ready` and `chat.state=ready` (model pulled, loaded, and verified via a short Lemonade completion).

**Backlog (not current milestone):**
- **Custom model catalog:** friendly display names (e.g. flagged labels), Lemonade `user_models.json` / registry overrides so Open WebUI and Lemonade agree — not alias hacks at load time.
- **OWUI model/policy via API:** evaluate configuring models through Open WebUI’s admin/API instead of (or in addition to) `DEFAULT_MODEL_*` env seeds — may survive container recreate more cleanly and avoid env/UI drift; keep env for bootstraps that must work before first login.
- **CP-owned module lifecycle:** Docker module containers use `--restart no`; CP starts/replaces them after boot and on policy drift (blue/green + nginx reload). A long-lived supervisor starts a missing container and force-recreates after **5 contiguous** failed HTTP health probes. Continue hardening rollback (failed cutover keeps old slot) rather than relying on Docker auto-start of stale Envs.
- **CP model picker:** let households choose/experiment with alternate catalog models from the control panel (not only the automatic planner pick).
- **Account types (child/adult):** per-account indicator for content filtering (OWUI policy/prompt layering); `users.account_type` column reserved in M6.2, population + policy later.
- **Local Wikipedia cache:** experimental indexed Wikipedia snapshot on spacious, well-connected appliances as an optional knowledge source for OWUI (when native KB tools are re-enabled).
- **Live API health:** CP status fields (`/api/data`, `/api/status`, subsystem snapshots) should report **current health** via cheap live checks (e.g. `pg_isready` / `SELECT 1`, Valkey PING, HTTP ready), not a one-shot or cached reflection of “daemon started once.” If Postgres stops accepting connections after boot, the API should flip to unhealthy.
- **Module catalog specs:** Define a Lemonseed **module configuration file** (one file per module under a catalog directory in-tree). Each spec encapsulates execution params (docker-compose fragment / pinned image), UX (logo, name, description, help), and dependencies (Postgres, Valkey, future object store). Catalog entries are what users see and what the CP downloads/sets up. The catalog itself needs a **test/release cycle**: pull candidate images → integration test → advance the version pin only when validated.
- **Module image pins (strategy):** Every Docker module tracks the **newest upstream stable** tag we have validated. Keep an explicit pin in code (Open WebUI: `ghcr.io/open-webui/open-webui:v0.11.0` via `OWUI_IMAGE_DEFAULT` in `control-plane/modules.py`) so field appliances never float on `:latest` / `:main`. The build/rebase pipeline should regularly bump pins to current stable, run unit + QEMU smoke (and hardware when the module is user-facing), then land the pin — not leave modules on stale releases for “safety.” Lab overrides use `LEMONSEED_OWUI_IMAGE` only.
- **Backup / restore:** Transactionally clean, space/time-efficient backup including Postgres dump, Valkey dump, and per-module **durable** volumes declared in the module spec. Targets: USB (preflight free-space check; optional encryption) and local SMB/NAS shares.
- **Installer stream-to-disk:** partition first, stream squashfs to `root-a`, avoid multi-GB RAM staging on the live ISO.
- **Internationalization &amp; accessibility:** Support for multiple languages and dialects, number units, and for assistive technology (e.g. image ALT tags, screen reader support).
- **UX Theme Support:** Enable multiple preset themes (e.g. light mode, dark mode, professional, playful) to give the website a specific look and feel to align with the user preferences.

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

### M3 verification

```bash
make repart-generate          # partitions.yaml → out/repart.d/
sudo make repart-test         # 20G loopback: ESP + root-a/b + grow user-data
make disk-repart DISK=/dev/sdX DRY_RUN=1   # dry-run on real hardware
```

### M4 verification (as implemented)

```bash
make build-installer          # → out/installer/lemonseed-installer.iso
# Flash ISO to USB, boot target PC, open http://lemonseed.local/
```
### M4 — USB installer (design)

**Product goal:** non-technical user buys a blank PC, boots Lemonseed USB, finishes install from a phone/laptop browser on the LAN. No assumption of KVM or serial console.

#### UX / discovery
| Surface | Behavior |
|---------|----------|
| Boot | Auto-start installer (no GRUB/isolinux “press Enter” menu) |
| Discovery | Avahi/mDNS advertise `lemonseed.local` on all interfaces |
| Install UI | HTTP on port **80** (no TLS during install); mobile-friendly; progress bar |
| Local console (optional) | Status splash on **tty4** (auto `chvt`): URL + IPs; not tty1 (avoids getty races) |
| Debug | Root shell on **tty2** (`Ctrl+Alt+F2`); return to splash with `Ctrl+Alt+F4` |

#### Disk policy
- **Auto-pick** the preferred internal disk; confirmation required before wipe.
- **Disallow** install onto USB-attached or **eMMC** media (not suitable for appliance user-data).
- Eligible disks must be **≥ 20 GiB** (raised from 16 GiB).
- If multiple eligible disks exist, confirmation screen allows selecting an alternate.
- Inspect existing labels/partition tables; show a **warning bar** if an existing OS would be wiped.
- Never target the installer USB itself (boot medium exclusion).
- Reinstall over a previous Lemonseed disk is expected: leftover `appliance` LVM maps (even with nothing mounted) hold the disk busy and `systemd-repart` cannot reread the GPT. The wipe path deactivates those maps and the installer ISO disables LVM autoactivation.

#### Minimum hardware (preflight)

Before download/repart, the installer refuses hosts that cannot usefully run Lemonseed (`installer/preflight.py`, also on `/api/disks` / `/api/preflight`):

| Check | Floor |
|-------|-------|
| Target disk | ≥ 20 GiB |
| System memory | **8 GB class** (≥ 7.2 GiB `MemTotal`; 10% reservation budget for firmware/GPU) |
| Processor | ≥ 4 CPU cores |

`MemTotal` is used (not `MemAvailable`) because x86 boxes sold as “8 GB” often report ~7.6–7.8 GiB after BIOS and GPU reservations. Floor: `8 GiB × 0.9` → **7,549,747 kB**.

**GPU VRAM is not checked on the installer USB** (lean ISO, no NVIDIA drivers). VRAM and model fit are validated after install on the appliance (`control-plane/hardware.py`, catalog planner).

Aligned with Lemonade’s small-model Docker guidance (8 GB RAM / 4 cores). Failures use plain language with next steps (not opaque ENOSPC).

**Backlog:** stream `root.squashfs` directly to `root-a` after partitioning (skip full RAM staging); add `size_bytes` to release `manifest.json`.

### M4.1 — Preserve known user-data on reinstall

**Why now:** `/var` will hold chat history, agent configuration, user data, Docker volumes, and M5 model state. Reinstalling the immutable OS must not silently erase that data.

#### Ownership proof
A partition is preservable only if all of these checks succeed (M4.1+ only; no pre-M4.1 layout support):

1. GPT partition label `lemonseed-user-data` and LVM type.
2. LVM VG/LV `appliance` / `user-data` with tag `lemonseed.storage=v1` on a single PV on that partition.
3. The LV is btrfs labeled `lemonseed-user-data` with required subvolumes (`@etc`, `@var`).
4. Top-level `.lemonseed-storage.json` identity (`format=lemonseed-storage-v1`).

Any ambiguity means **do not offer preservation**.

#### Installer behavior
- Disk selection API reports `preservable_user_data` plus non-sensitive size/version summary only after the proof succeeds.
- If true, show **“Preserve existing user information on this appliance”**, checked by default. Explain that it retains chat/workers/models and replaces Lemonseed system software.
- Fresh install and unchecked preservation retain the current destructive `systemd-repart --empty=force` path.
- Checked preservation is a **layout-compatible reinstall** only: do not repartition or resize user-data; deactivate its LVM/DM maps **without** signature wiping; rewrite `root-a`, clear/reseed `root-b`, reformat/reseed ESP and GRUB.
- The installer must refuse preservation if ESP/root/user-data geometry or labels are incompatible. It may offer a destructive install, never a best-effort data move.
- After boot, storage provisioning recognizes the preserved identity and is strictly non-destructive.

#### Verification
- [ ] Fresh install writes identity and namespaced storage metadata.
- [ ] Reinstall detects a known volume and defaults Preserve on.
- [ ] Preserve path retains a sentinel under `@var` and setup state under `@etc`.
- [ ] Uncheck path destroys that sentinel.
- [ ] A generic LVM/btrfs disk never exposes Preserve.

#### Image source
- Download appliance artifacts from **lemonseed.ai** (channel default: `https://lemonseed.ai/releases/stable/`).
- Fetch `manifest.json`, then `vmlinuz` / `initrd.img` / `root.squashfs`; verify SHA-256 from the manifest.
- Avoids stale SquashFS baked onto old sticks.
- Offline/bundled payload and alternate channels (e.g. beta) are **later** options.
- **Lab overrides** (kernel cmdline or env, not in the UI):
  - `lemonseed.image=http://host:port/` — HTTP directory containing `manifest.json` + artifacts
  - `lemonseed.artifacts=/path` or `LEMONSEED_ARTIFACT_DIR` — local directory of artifacts

#### Network
- **Ethernet + DHCP only** for M4.
- WiFi AP SSID (e.g. `lemonseed-install`) is roadmap, not M4.

#### Install pipeline (high level)
1. Confirm disk → release leftover LVM/DM holders on the target (reinstall-safe; freeze udev so `appliance-user--data` cannot be remapped) → `systemd-repart --empty=force` from `partitions.yaml` (wipe)
2. Download + verify artifacts → write SquashFS to **root-a** (root-b left empty for the updater)
3. Seed ESP: GRUB (`grub-install` + `EFI/BOOT/BOOTX64.EFI`), kernels/initrds for slot a, `grubenv` (`boot_default=a`)
4. Register EFI boot entry (`efibootmgr`, label **Lemonseed**) on the target disk
5. Prompt: remove USB → reboot into appliance. The installer page stays open and polls `http://lemonseed.local/` (and the current origin) until the appliance `/api/status` reports `role=control-plane`, then loads the first-boot setup UI.

#### EFI reboot / USB stick
Always **prompt the user to remove the installer CD/USB**, then reboot into the installed system.

Before the prompt, `sync` so the target disk is fully flushed. Reboot uses **sysrq-b** (`echo b > /proc/sysrq-trigger`), not a graceful `systemctl reboot` — the live root is on the removable media, and a clean shutdown after eject floods SquashFS read errors.

Do not rely on `BootNext` / BootOrder tricks to skip the stick — firmware behavior is too inconsistent, and a single “remove media → reboot” step is clearer for non-technical users.

Installer still registers a normal EFI boot entry on the internal ESP so the machine boots Lemonseed once the stick is gone.

#### Roadmap (post-M4 installer)
- WiFi AP `lemonseed-install`
- Expandable web terminal for live install logs
- Advanced options: download rate limit, custom storage, image channel (beta/offline)

### M5 — Hardware-aware local chat (design)

**Pass/fail target:** existing **NVIDIA** prototype (Broadwell Xeon-D + NVIDIA class per lab notes). QEMU/`lemonade.corddry.net`-style VMs without GPU are **install-only**, not M5 acceptance hosts.

**Workload:** text chat only — Lemonade serves **one** local LLM; complete a known prompt/response. No image model, no Open WebUI in M5.

**Model policy:** versioned, benchmarked **model catalog**; control plane selects the best fit for detected hardware. Do **not** hardcode a single model for all boxes; do **not** require the user to pick during M5 setup.

#### Hardware inventory (control plane API)
Canonical report (e.g. `/api/hardware`), persisted under `/run/appliance/hardware.json` (or equivalent):
- CPU model, sockets/cores, system RAM
- Per-GPU devices: vendor, PCI ID, name, **per-device** accelerator memory (do **not** sum VRAM across GPUs)
- Driver/toolkit readiness (NVIDIA driver, CUDA/container toolkit visibility)
- User-data free space on the btrfs LV
- Optional later: thermals/power

**NVIDIA path (M5):** host NVIDIA open kernel modules in the SquashFS base; `lemond` runs on the host (systemd) with CUDA/Vulkan backends wired from Nix packages — **not** via NVIDIA Container Toolkit for inference.

**AMD / Ryzen AI Max (post-M5):** treat usable accelerator memory as an **UMA / system-RAM budget**, not discrete VRAM. Inventory-only until a dedicated profile exists. Reuse patterns from [nix-amd-ai](https://github.com/noamsto/nix-amd-ai) (ROCm/Vulkan/NPU wiring) when that profile lands.

**Heterogeneous GPUs (post-M5):** physical devices stay separate. Planner assigns **workloads to devices**; mixed AMD+NVIDIA is **not** pooled memory for one model.

#### Capability planner + catalog
Catalog is a versioned artifact (served/signed like other release metadata) with entries such as:
- model id, backend (`llamacpp:cuda`, …), quantization, weights size, license/hash
- minimum accelerator memory + host RAM reserve + context/KV cost notes
- rank / quality tier from our benchmarks

Planner picks the highest-ranked entry that fits **one** supported execution target. CPU-only install is diagnostic/fallback and does **not** qualify a hardware profile as product-ready.

Legacy intuition from `/home/wago/setup-lemonade.sh` (VRAM buckets → Gemma-class sizes) informs **catalog tiers**, but that script is **not** reused: one-vendor assumption, string-concat VRAM bug, guessed AMD memory, mutates upstream systemd, `/home` model paths.

Rough tier anchors (catalog will own exact ids/hashes):

| Accelerator mem | Example LLM tier (chat-only M5) |
|-----------------|----------------------------------|
| ≥64 GiB | large MoE / 26B+ class |
| ≥32 GiB | ~26B class |
| ≥16 GiB | ~12B class |
| ≥8 GiB | E4B class |
| ≥6 GiB | E2B class |
| below | best small catalog entry or “not ready” |

#### Lemonade bring-up (locked: embeddable `lemond` in SquashFS)
- **Primary path:** ship headless `lemond` (+ `lemonade` CLI for bring-up/debug) **inside the SquashFS**, supervised by systemd; control plane owns lifecycle, catalog selection, pull/load, and health.
- **Not primary:** Lemonade Docker image, Lemonade web UI, Tauri desktop app. Docker remains for later *modules* (Open WebUI, ComfyUI, …).
- Bind **127.0.0.1** only; require `LEMONADE_API_KEY` known only to the control plane; `no_broadcast=true`.
- Mutable state on user-data: **`/var/lib/lemonseed/lemonade/`** (`config.json`, backends cache if any, recipe options) and **`/var/lib/lemonseed/models/`** — never SquashFS, never `/tmp`.
- Prefer **Nix-built backends** (`llama-cpp` CUDA/Vulkan/… as available) wired into `lemond` via store paths / defaults seeding, rather than letting `lemond` download foreign FHS backend trees at runtime.
- One-shot / idempotent bring-up owned by the control plane (not `/.lemonade-setup-done` on the rootfs).

##### Lessons from [nix-amd-ai](https://github.com/noamsto/nix-amd-ai) (adopt patterns; do not vendor their whole NPU stack in M5)
That flake already solves “Lemonade on NixOS” the hard way. Steal these ideas:

| Pattern | Why it matters for Lemonseed |
|---------|------------------------------|
| **Build `lemond` from source** with `withWebApp=false` / `withDesktopApp=false` | Avoids Ubuntu glibc tarball/`patchelf` pain; keeps SquashFS headless and smaller |
| **Do not let Lemonade fetch TheRock / foreign backend trees** | On NixOS those shadow RPATHs and break loaders; ship self-contained Nix backends |
| **Pin `backend_versions.json` to the backends we ship** | Stops “needs update → download” loops against our store paths |
| **Seed defaults via `LEMONADE_DEFAULTS_PATH` (or equivalent)** | Declarative `*_bin` paths survive read-only `/usr` and SquashFS layout |
| **Re-apply declarative settings on every `lemond` start** | Mutable `config.json` on `@var` must not silently win over appliance policy forever |
| **systemd unit with `RuntimeDirectory`, groups for GPU devices, optional `nix-ld`** | Matches how foreign helper binaries (ASR/TTS later) actually run |
| **Separate package vs module** | Package = store closure in SquashFS; module/CP = port, key, models_dir, backend enable flags |

We may later take `nix-amd-ai` as a flake input for AMD/NPU profiles, or vendor a slim `pkgs/lemonade` derivation inspired by theirs. M5 NVIDIA starts with a **minimal headless packaging spike**, not a full import of their AMD module.

#### First-boot UX (M5)
1. Existing name/timezone setup completes (unchanged).
2. **Background** download/load of the catalog-selected model with progress in the portal (“Preparing local chat…”). Setup must not block Finish on multi-GB downloads.
3. Portal shows **Local chat ready** (or a clear non-ready reason), selected model, and rough resource use.
4. Expandable diagnostics can expose inventory + planner rationale.

#### Acceptance checklist
- [x] `/api/hardware` reports the NVIDIA device(s) and per-GPU memory
- [x] Planner returns a catalog selection with a human-readable reason
- [x] `lemond` systemd unit is healthy on localhost with API key auth
- [x] Host GPU/backend is visible to `lemond` (e.g. `/v1/system-info` / installed recipe)
- [x] Selected model is pulled with hash/license check, then loaded *(CP background pull/load; verify on hardware)*
- [x] Known prompt completes via Lemonade’s local OpenAI-compatible API *(validated manually on prototype; CP loads catalog model)*
- [x] Portal shows ready / not-ready without requiring a manual refresh loop beyond normal polling

**M5 platform note (2026-08-21):** Nix-wired `llamacpp` cpu/cuda, FHS probes, correct `config.json` reseed, and manual Gemma load/chat were verified on the GTX 1660 SUPER prototype before CP automation landed. CP background pull/load/pin + boot reload verified on hardware.

#### Testing (M5)
| Tier | Target | Command |
|------|--------|---------|
| **A — unit** | Update/artifacts, gen-repart, control-plane catalog/hardware/chat/HTTP | `make test` (no root) |
| **B — loopback** | systemd-repart layout, A/B updater | `sudo make repart-test`, `sudo make update-test` |
| **C — QEMU HTTP** | CPU-profile appliance boot; `/healthz`, `/api/status`, `/api/boot` | `make test-qemu` (needs `make build-qemu`) |
| **D — GPU lab** | Full M5 acceptance on NVIDIA hardware / future Proxmox GPU VM | manual / lab automation (later) |

#### Explicitly out of M5
Open WebUI, gateway auth Phase 2, ComfyUI/sd-cpp images, Lemonade web/desktop UI, multi-user accounts, LUKS, AMD production path, multi-GPU scheduling, A/B lock-in gated on model load (lock-in stays on OS/CP health for now).

---

## 3. High-level architecture

```
┌─────────────────────────────────────────────────────────────┐
│  HTTP distribution                                          │
│  appliance-<version>/                                       │
│    vmlinuz  root.squashfs  manifest.json  *.sig             │
└─────────────────────────────┬───────────────────────────────┘
                              │ install / update
                              ▼
┌──────────────┐  ┌─────────────┐  ┌─────────────┐  ┌──────────────────────────┐
│ ESP (FAT32)  │  │ squashfs-a  │  │ squashfs-b  │  │ user-data (LVM PV)       │
│ GRUB +       │  │ (dd image)  │  │ (dd image)  │  │ → VG appliance           │
│ vmlinuz(s)   │  │             │  │             │  │ → LV user-data (btrfs)   │
│ grub.cfg     │  │             │  │             │  │ → @etc,@var → /etc,/var  │
│ grubenv      │  │             │  │             │  │   (+ docker volumes)     │
└──────┬───────┘  └──────▲──────┘  └──────▲──────┘  └──────────────────────────┘
       │                 │                │
       │    GRUB picks A or B (default / once)
       └─────────────────┴────────────────┘
                         │
                         ▼
              read-only SquashFS root
              systemd → control-plane → lemond (localhost API)
                         │                 └── llama.cpp / backends (Nix-wired)
                         └── Docker modules later (Open WebUI, …)
```

**Configuration authority (M6 principle):** The **control plane + Postgres is the source of truth** for appliance intent — user/group accounts, module registry, model pins, credentials. Configuration files and environment variables (OWUI env/secrets, Authelia `users.yaml`, nginx gate/upstream snippets, `lemond.env`, data-service secrets) are **derived artifacts**, regenerated by CP on change and reconciled at boot. Nothing downstream of CP is hand-edited as truth.

---

## 4. Disk layout (EFI-first GPT)

**Assumption for now:** EFI-capable hardware only. Non-EFI/MBR support is deferred (GPU appliances are typically EFI).

| # | Role | Type | FS / content | Size |
|---|------|------|--------------|------|
| 1 | ESP | GPT EF00 | FAT32 — GRUB EFI, kernels, `grub.cfg`, `grubenv` | 0.5–1 GiB |
| 2 | root-a | Linux / custom label | SquashFS written with `dd` (no FS layer) | ~4 GiB |
| 3 | root-b | Linux / custom label | SquashFS written with `dd` | ~4 GiB |
| 4 | user-data | GPT 8E00 (LVM) | **LVM PV only at install**; first boot creates VG/LV/btrfs | all remaining |

Notes:
- ESP is **not mounted in normal operation**; mount on demand for updates and A/B lock-in.
- **In-place updates:** `lemonseed-update` on the appliance writes verified SquashFS + ESP kernels into the **inactive** slot, sets GRUB `boot_once`, and leaves `@etc`/`@var` untouched. On the next reboot GRUB tries the new slot once; `lemonseed-ab-lock-in` promotes `boot_default` only after platform services (network, nginx, control plane, Avahi, Docker) are healthy. A failed boot falls back to the previous `boot_default`. Lab overrides: `--url` / `--dir` (same conventions as the installer).
- Root partitions are **raw SquashFS images**, bootable without unpacking to ext4.
- **Installer** creates the GPT layout and LVM PV; it does **not** format btrfs.
- **First-boot provisioner** (`appliance-storage-provision.service`, shell oneshot every boot): if LV missing, create VG `appliance` / LV `user-data`, `mkfs.btrfs`, subvols `@etc` / `@var`. If LV exists, no-op aside from ensuring subvols.
- Later: additional disks → extra PVs → `vgextend` / `lvextend` / btrfs resize (control plane; not M2).
- Exact sizes and type UUIDs live in a **separate partition config** (see §5), not hardcoded in Makefiles.

### Bootloader
- **GRUB** on EFI (`grubx64.efi` on ESP).
- A/B selection via **GRUB environment block** (`grub-editenv`), reusing stock GRUB mechanisms.

### A/B boot protocol

Variables (names indicative; finalize at M3):

| Variable | Meaning |
|----------|---------|
| `boot_default` | Permanent slot: `a` or `b` |
| `boot_once` | Optional one-shot slot for next boot |
| `boot_attempted` | Slot being tried (set when consuming `boot_once`) |

Flow:
1. Update writes new SquashFS + matching `vmlinuz` into the inactive slot / ESP.
2. Updater sets `boot_once=<new>` (and clears prior attempt state as needed).
3. GRUB: if `boot_once` set → boot that slot once, set `boot_attempted`, clear `boot_once`.
4. On **successful startup**, control plane (or early systemd unit) mounts ESP and sets `boot_default` to the running slot, clears `boot_attempted`.
5. On **failure** (crash/hang before lock-in), power cycle → GRUB uses last `boot_default`.

“Successful startup” criteria for lock-in will be defined with the control plane (e.g. control plane healthy + Docker up). Stub in M2 can lock-in after a simple local health check.

---

## 5. Partition configuration format

### Evaluation of existing tools

| Tool | Format | Strengths | Fit for us |
|------|--------|-----------|------------|
| **systemd-repart** | INI `repart.d/*.conf` | GPT-native; grows partitions to fill disk; `CopyBlocks=` for raw image install; btrfs `Subvolumes=`; already on a systemd appliance | **Best runtime/install engine** |
| **disko** | Nix | Excellent for NixOS installs; reproducible | Strong if installer is Nix-based; less ideal as a portable text contract for non-Nix helpers |
| **genimage** | libconfuse `.cfg` | Great for **building** fixed disk images offline | Weak for installing onto **arbitrary disk sizes** in the field |
| **Yocto WIC (.wks)** | Kickstart-like | Familiar A/B patterns in embedded | Yocto-centric; pulls us off Nix |
| **sfdisk** | Dump script | Simple, scriptable | Low-level; no FS/subvol/copy semantics |

### Decision (proposed)
- **Source of truth in-repo:** human-editable **YAML** (preferred), describing roles, sizes, FS, labels, mount policy.
- **Install/apply engine (M3/M4):** generate or map to **`systemd-repart`** definitions and invoke `systemd-repart` (plus thin helpers for GRUB and SquashFS `CopyBlocks`/`dd`).
- Rationale: YAML stays easy to review and edit; repart already solves “same layout, different disk sizes,” raw block copy into partitions, and btrfs subvolume creation—aligned with an appliance USB installer.

Example shape (illustrative only):

```yaml
# partitions.yaml
table: gpt
firmware: efi   # efi-only for now

partitions:
  - name: esp
    role: esp
    size: 1G
    type: ef00
    filesystem: vfat
    label: ESP
    mount:
      path: /boot
      policy: on-demand   # not mounted in normal operation

  - name: root-a
    role: squashfs-root
    size: 4G
    label: root-a
    content: raw-squashfs

  - name: root-b
    role: squashfs-root
    size: 4G
    label: root-b
    content: raw-squashfs

  - name: user-data
    role: lvm-pv
    size: grow
    type: 8e00
    label: lemonseed-user-data
    # First boot (not installer): VG appliance / LV user-data / btrfs
    # subvolumes @etc → /etc, @var → /var
```

Final mount flags per path will be tightened during bring-up; principle: **`noexec` / `nodev` / `nosuid` wherever practical**.

### Storage ownership
| Layer | Responsibility |
|-------|----------------|
| Installer (M4) | GPT + LVM PV for user-data |
| `appliance-storage-provision` | Every boot; create VG/LV/btrfs/subvols if missing |
| Control plane | Future multi-disk expand; Docker/app orchestration |

---

## 6. Root filesystem model

### Read-only SquashFS
- NixOS build produces a SquashFS root.
- Installed onto A/B partitions with **`dd` / block copy** so the kernel can use the partition as root.
- **No overlayfs / tmpfs overlay** on `/`.

### Mutable paths
- btrfs subvolumes on the **user-data LV**, mounted over paths that need RW (`subvol=@etc`, `subvol=@var`).
- Initial set: **`/etc`**, **`/var`** only.
- **`/tmp`:** size-capped **tmpfs** (`boot.tmp.useTmpfs`, 128M) — not a btrfs subvol and not a symlink to `/var/tmp`. Needed early (before user-data), and must not compete with GPU/model RAM. Persistent scratch belongs on `/var`.
- Docker volume data lives under user-data (typically under `/var/lib/docker` once `/var` is mutable).
- Additional mounts only as future need appears.
- Device discovery for the PV: `/dev/disk/by-partlabel/lemonseed-user-data`, else kernel cmdline `appliance.storage=` (QEMU).
- **`/etc` population:** empty `@etc` is fine; NixOS `setup-etc.pl` activation recreates managed `/etc` from the Nix store each boot. We do **not** copy SquashFS `/etc` into `@etc`.
- **Metadata convention:**
  - `/run/appliance/version` — image version from the booted closure (tmpfiles symlink; not on `@etc`)
  - `/etc/appliance-*` — mutable markers on `@etc` (e.g. `/etc/appliance-first-boot` ISO-8601 UTC, written once after `time-sync.target`)
- **`/var`:** empty `@var` is fine; systemd-tmpfiles and services create what they need; state persists across image updates.

### Initrd (open option)
- Soft goal: **no initrd** — kernel mounts SquashFS root directly with needed drivers built-in.
- Practical risk: diverse storage (NVMe/SATA/USB) and btrfs/ESP bring-up may force a small initrd.
- Decision deferred to implementation spikes in M1/M2; design must not assume initrd is impossible.
- If initrd is required, keep it **minimal** (storage + filesystem modules, mount logic) and version/sign it like other boot artifacts.

---

## 7. Image contents

### In the SquashFS base
- systemd
- Docker (host networking) — for **modules**, not for `lemond`
- **Headless Lemonade (`lemond` + CLI)** built from source; no Lemonade web/desktop UI
- GPU driver stacks in-base for discovery at boot (**NVIDIA open kernel modules** preferred over legacy closed; AMD later)
- Inference backends as Nix packages where practical (`llama-cpp` CUDA/Vulkan/…); kernel selected by **GPU support matrix**
- **Control plane** (systemd-supervised)
- Tools needed for updates: mount ESP on demand, `grub-editenv`, verify signatures, write inactive slot

### Not in the SquashFS base
- Open WebUI, ComfyUI, PostgreSQL, Valkey, and other **module/data** images — pulled at runtime via control plane / Docker
- Model weights and Lemonade mutable config — live on user-data under `/var/lib/lemonseed/`
- PostgreSQL/Valkey data dirs — on user-data under `/var/lib/lemonseed/data/` (preserve-on-reinstall)
- User data

### Networking
- Docker **host networking** to avoid bridge/CNI complexity (for modules).
- `lemond` listens on **loopback only**; nginx :80 is the only LAN entry.

### Control plane (direction; stub in M2)
- **Language:** Python, if image overhead stays low–moderate (revisit Go if footprint hurts).
- **Process split:**
  1. **Privileged daemon** — Docker lifecycle, image updates, A/B lock-in, disk ops; binds **127.0.0.1:8080** only.
  2. **Web UI** — served by the same process for bring-up; later may split to an unprivileged UI talking to the privileged API.
- systemd starts and restarts the privileged control plane; that plane brings up containers/volumes.

### HTTP gateway (M4+)
- **Public entry is always nginx on :80** (firewall opens 80; CP stays loopback-only).
- **Phase 1 (first boot / unset appliance):** nginx is a straight reverse proxy to `127.0.0.1:8080`. The CP serves a **no-login** readiness + setup wizard (`/`, `/setup`, `/api/boot`). Completing setup writes `/etc/appliance-setup-complete` (+ JSON state).
- **Phase 2 (post-setup / M6.1+):** nginx routes document `/` → Lemonseed shell (CP); **iframe** `/` (or `/?embed=1`) → Open WebUI at path `/` (root SPA — must not be mounted under `/chat/` or the client router 404s); catch-all → Open WebUI (assets/`/api/v1`/`/ws/`); reserved `/settings/`, `/setup`, `/healthz`, and Lemonseed `/api/{boot,status,…}` → CP. Reloadable upstream file enables blue/green. **Proxy auth** at nginx when Lemonseed user management is ready (M6.2). Modules stay **auth-naive**.
- **mDNS:** Avahi publishes the slug of the appliance **Name** from setup (`Matt Lemonseed 1` → `matt-lemonseed-1.local`). Before setup the name defaults to `Lemonseed`, so a fresh appliance is reachable at `lemonseed.local` for the installer hand-off. After a preserve reinstall of a **renamed** appliance, `lemonseed-avahi-alias` briefly publishes `lemonseed.local` as a CNAME so the installer can discover the box; the installer (and any leftover `lemonseed.local` hit) then redirects to `<slug>.local` and the alias is released (`/run/lemonseed/mdns-handoff-done`). Fully confirmed appliances never claim `lemonseed.local`, so multiple units can share a LAN.
- **Appliance identity (one name, not two):** setup collects a single user-facing **Name**; its slug is the system hostname, the mDNS name, and the hostname dhcpcd advertises. `networking.hostName` is therefore **empty** in the NixOS config — a non-empty value makes `/etc/hostname` a read-only store symlink and pins `host-name=` in `avahi-daemon.conf`, both of which would defeat the chosen name. `lemonseed-identity.service` seeds `/etc/hostname` on `@etc` (from `/etc/appliance-setup.json`, else the existing value, else `lemonseed`) before dhcpcd/Avahi start.
- **Name collisions:** setup resolves `<slug>.local` over mDNS before offering a default; if another host owns it, the suggested name becomes `Lemonseed 2` (editable), and submitting a name owned by a different host is rejected. Avahi's effective hostname (queried over D-Bus, including any collision suffix) identifies our own announcement; IP comparison is only a fallback because DHCP leases can change. Multiple appliances therefore never share one advertised name.
- **Local console:** tty4 status splash (IP / URL / boot hints, `docker*` interfaces excluded); tty2 root autologin for field debug (empty root password during bring-up only).

---

## 8. Build system

### Nix
- **Flakes** + pinned `nixpkgs` for reproducibility.
- Project root: this directory (`nix/`), intended for GitHub.
- No absolute paths in Makefiles or scripts — everything relative to repo root / `CURDIR` / Nix store outputs.

### Makefile (M1)
| Target | Behavior |
|--------|----------|
| *(default)* / `help` | Print help; no build |
| `build-production` | Production SquashFS + `vmlinuz` (+ initrd) → `out/latest` |
| `build-qemu` | QEMU smoke profile (different root device / tmpfs mounts) → `out/latest-qemu` |
| `run-qemu` | `build-qemu` if artifacts missing, then boot serial console |
| `clean` | Remove `out/` |
| `keys` | Generate local ed25519 test keys |

Production and QEMU are **separate NixOS builds**, not a repackage of each other.

### Versioning
- Default: `YYYYMMDD.NN` where `NN` is a **local build counter** (file in repo tree, gitignored).
- Override: environment variable (e.g. `VERSION=20260810.01 make build-production`) for official releases.

### Outputs (HTTP-ready layout)

```
out/appliance-<version>/
  vmlinuz
  root.squashfs
  manifest.json          # names, versions, hashes, slot metadata
  vmlinuz.sig            # optional until signing enabled
  root.squashfs.sig
  manifest.json.sig
```

Distribution may later wrap this as a tarball or serve discrete objects; layout stays the same either way.

### Signing
- **Detached signatures** over artifacts (and/or manifest that lists hashes).
- M1: signing **disabled by default** or driven by **local test keys** (gitignored); release keys later.
- Verifier used at download/install time (M4+). Secure Boot / TPM are explicit follow-ons.

### `make run-qemu`
- Kernel + SquashFS + serial console only.
- Full A/B disk and GPU validation on **real hardware** (Broadwell Xeon-D + NVIDIA first; AMD later). Dev VM (`lemonade.corddry.net`) is insufficient for GPU integration tests.

---

## 9. Security posture (v1 direction)

| Layer | Approach |
|-------|----------|
| Transport / update authenticity | HTTPS + **detached signatures** on artifacts |
| Root filesystem | Read-only SquashFS; no overlay |
| Mutable state | Minimal btrfs binds; restrictive mount flags |
| Edge auth (M6.2) | **Authelia (file backend)** behind nginx `auth_request` at `/auth/`; sessions in **Valkey** (key TTL → auto-GC); identity `X-Forwarded-*` only from Authelia; modules auth-naive |
| Privileged ops | Isolated control-plane daemon vs UI |
| Boot trust / TPM / Secure Boot | Deferred |
| Driver redistribution | All-in-image for simplicity; revisit if licensing blocks redistribution |

---

## 10. Proposed repo layout (M1+)

```
.
├── Requirements.md
├── DESIGN.md                 # this file
├── Makefile
├── flake.nix
├── flake.lock
├── partitions.yaml           # layout source of truth (used heavily from M3)
├── nixos/
│   └── configuration.nix     # appliance module(s)
├── scripts/                  # versioning, packaging, qemu run helpers
├── keys/                     # optional local test keys (gitignored contents)
├── control-plane/            # Python stub from M2
└── out/                      # build outputs (gitignored)
```

Exact NixOS module split can evolve; keep hardware/GPU, docker, and control-plane as separate modules early.

---

## 11. Open questions / deferred decisions

Resolved for early milestones where noted; remaining items do not block M5 start on NVIDIA HW:

1. **Initrd:** soft goal of none; still shipping initrd practically.
2. **Exact GPT type UUIDs** for A/B SquashFS — linux-generic in use for bring-up.
3. **Lock-in success criteria** — OS + control plane (+ Docker) for now; **not** gated on Lemonade model load (M5 decision).
4. **`/var` mount flags** — validate empirically under Docker/Lemonade.
5. **Python vs Go** — Python for CP through M5.
6. **HTTP channel details** — versioned dirs + `manifest.json` (current CDN path).
7. **NVIDIA open module + nixpkgs kernel pin + CUDA `llama-cpp`** — **blocks M5 GPU tests**; lock a known-good combo on the NVIDIA prototype before catalog load work.
8. **Model download timing** — **background after setup** (M5 decision).
9. **Encryption** — deferred past M5; document a later LUKS/migration plan before friends-and-family data. See `./AUTH-TOTP.md` for one idea.
10. **ComfyUI vs Lemonade sd-cpp** — ComfyUI preferred when image gen lands; skip images in M5.
11. **Heterogeneous GPU planner** — inventory-ready schema now; scheduling later.
12. **`lemond` packaging** — from-source Nix derivation (nix-amd-ai patterns); whether to flake-input `nix-amd-ai` later for AMD only, or vendor a slim copy.

---

## 12. Decision log (summary)

| Topic | Decision |
|-------|----------|
| Product | Local multi-user home AI hub; Concept.md is product source |
| First coding milestone | Artifacts only (M1) — done |
| Arch | x86_64; ARM later |
| Firmware | EFI-only for now |
| Bootloader | GRUB EFI |
| A/B | GRUB `grubenv` one-shot + lock-in |
| Root | SquashFS via `dd`; no overlay |
| User-data | Installer creates the LVM PV only; the first-boot initrd provisioner runs the idempotent VG/LV/btrfs provisioning (no installer-side provisioner, no interstitial) |
| Mutable state | btrfs subvols `@etc`/`@var` mounted on `/etc`/`/var` |
| `/tmp` | size-capped tmpfs (128M) |
| ESP | On-demand mount only |
| Apps / modules | Not baked into image; Docker at runtime; **state in Postgres/Valkey**, not bind-mounted SQLite |
| Data services | Shared PostgreSQL (+ pgvector), Valkey; CP-provisioned credentials |
| Inference runtime | **`lemond` embedded in SquashFS** (headless); Docker is not the Lemonade path |
| Module updates | Blue/green: start new → health-check → nginx cutover → stop old |
| GPU | Drivers in base; NVIDIA first (M5); AMD/UMA later; no VRAM pooling |
| Inference UX | Open WebUI primary (M6); catalog-selected LLM via `lemond` |
| Models | Stored under `/var/lib/lemonseed/`; versioned catalog fit |
| Lemonade packaging | From-source Nix; wire Nix backends; patterns from nix-amd-ai |
| Control plane | systemd root; Python; privileged API + web UI; **CP state in Postgres** |
| Builds | Local; flakes; `build-production` / `build-qemu` / `run-qemu` / `qemu-clean` |
| Version | Date + local counter; `VERSION` override |
| Partition config | YAML source → systemd-repart as apply engine |
| Signing | Layout ready; detached sigs; optional/local keys in M1 |
| Distribution | HTTP; versioned artifact directory |
| Installer | M4; partitions (+ optional lockout reset marker, M6.2); no auth work for fresh installs; reinstall releases LVM/DM |
| Encryption | Deferred past M5 |
| Open WebUI / auth | **M6.1:** OWUI auth off. **M6.2:** edge auth only at the nginx boundary — modules stay auth-naive (no per-container OAuth) |
| Edge auth (M6.2) | **CP-native login** (signed session tokens, branded login page). Replaces Authelia (https workarounds were fragile). Stateless HMAC-SHA256 tokens in cookie; no session store to GC. nginx `auth_request` at `/_lemonseed/session-verify` → CP. Login at `/login`, logout at `/logout`. TOTP (M6.3) via `pyotp`, required for `admin` group. |
| CP authority | **CP + Postgres is the source of truth** (accounts = standard **users + groups**, many:many, Linux-style; elevation = `admin` group, no singular owner); config files / env / secrets are derived and regenerated (see §3) |
| Admin bootstrap | **Single `/setup` path** (synchronous; name + timezone + admin creds) — first admin only while the `admin` group is empty; no installer interstitial, no Authelia/`mkpasswd` in the installer; lockout = preserve-reinstall + “Reset admin credentials” marker (physical access) clears the `admin` group so `/setup` reopens |

---

## 13. Review checklist

Historical M1 checklist (kept for archive):

- [x] M1 scope (artifacts + QEMU `run`)
- [x] EFI-only GPT layout (ESP + A + B + user-data)
- [x] YAML → systemd-repart approach for partitions
- [x] No-overlay / btrfs bind-mount model
- [x] Signing optional/local for M1 but always present in layout
- [x] Python control plane direction

**M5 review (please confirm or correct):**
- [x] NVIDIA prototype is the M5 pass/fail host
- [x] Chat-only + catalog selection is the right M5 cut
- [x] Background model download after setup (not blocking Finish)
- [x] Soft A/B lock-in (no model-load gate)
- [x] Encryption deferred past M5
- [x] `/var/lib/lemonseed/` for models/state
- [x] Headless `lemond` in SquashFS (not Docker; no Lemonade UI)
- [x] Tier A unit tests (`make test`)
- [x] M5 closed — see commit on `main` (hardware-aware local chat + tests)

**M6 review:**
- [x] UX evaluation complete — Open WebUI primary; NanoClaw-style UI deferred
- [x] Conduit noted as future mobile path
- [x] Shared Postgres + Valkey + pgvector; minimal bind mounts; blue/green module updates
- [x] Auth: OWUI disabled for M6.1; edge auth at nginx (M6.2 design locked; not container OAuth)
- [x] M6.0a data services + CP credential provisioning
- [x] M6.1 Open WebUI on Postgres + Lemonade backend + Lemonseed shell
- [x] M6.2a: Authelia (file backend) + Valkey sessions + `/auth/` gate + first-admin bootstrap via `/setup` — **superseded by CP-native login** (Authelia https workarounds were fragile; see M6.2 migration)
- [ ] M6.2 (migration): Remove Authelia; CP-native login (signed tokens, branded page, `/login` + `/logout` + `/_lemonseed/session-verify`); remove https workarounds
- [ ] M6.2b: `/api/auth` users & groups management (user add / reset / disable / remove; group create / map) + portal Users & Groups section
- [ ] M6.3: TOTP MFA via `pyotp` (required for `admin` group; device remembered per session)
- [ ] Post-M6: Cloud service / DynDNS (`proposals/CLOUD-SERVICE.md`) for remote access + Let's Encrypt
