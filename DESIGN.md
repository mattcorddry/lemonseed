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
| **M6 — Open WebUI household UX** | **In progress** — M6.1 + M6.2 + M6.3a + M6.4 + M6.5 done; M6.3b partial (persistence flip + policy-reconcile engine + portal ownership view + guard enforcement / reset window landed; LiteLLM models pending); M6.2b user/group management next; SearXNG/ComfyUI after |
| **M6+ (later)** | Conduit/mobile polish, AMD GPU driver optimization (ROCm/HRX), AMD NPU (XDNA), heterogeneous GPU planner, **omni (LMX `collection.omni`) models** (blocked on lemond load path — see M6.5), **geoip-based model-family preference** (simple GeoIP lookup; prefer the family made in the user's own country — e.g. China → Qwen, US → Gemma — with the static default as fallback), **advanced model picker** ("advanced" tab exposing the full live `lemonade list` catalog so users can pick off-menu models beyond the curated families), encryption, offline install, Secure Boot/TPM |

**M6.4 status (2026-09):** AMD Vulkan GPU support is **implemented** (code + unit tests green). QEMU smoke test pending (CPU path only; no AMD GPU in QEMU). Real-hardware validation pending on RX 6800 (gfx1030) and Ryzen AI 385 (gfx1151 iGPU + XDNA NPU).

**M6.5 status (2026-09):** Model picker is **implemented** (code + unit tests green). QEMU smoke test **green** at the 16 GiB CPU guest (picker payload + `selected` plan). The **model-load e2e is green**: `QEMU_E2E_CHAT=1` makes the QEMU test pick Gemma (family-level choice), pull + load the model, and assert `/api/chat` reaches `state=ready` with `Gemma-4-E4B-it-GGUF` (the ~6 GB weights are cached on the persistent userdata disk, so re-runs skip the download). The **disk-space GC e2e** (`QEMU_E2E_GC=1`, opt-in on top of `QEMU_E2E_CHAT=1`) pulls a second model (LFM2.5 8B), fills `/var` past the 90% high-water mark with a test-only CP filler, pulls a third (LFM2.5 1.2B), and asserts the LRU cached model (Gemma E4B) was evicted via lemond's API while the pinned model survives — **implemented, not yet run** (adds ~13 GB of downloads; run on a clean 32 GB userdata disk). **GPT-OSS removed** from the catalog (segfaults on every backend — Lemonade incompatibility). **LFM2 added** (Liquid AI family: LFM2.5 8B A1B + LFM2.5 1.2B Instruct); the catalog is now 4 families / 12 models. **Model disk-space maintenance** added (90% high-water LRU eviction, 10% reserve, keep-1-fallback, refuse-on-no-room) and **validated on hardware** (NVIDIA box: GC correctly evicted LRU models while keeping the pinned model; the earlier pinned-model `rmtree` race is fixed by the rmtree-safety rule). Real-hardware validation (best-fit per family, choice persistence, download estimate) still pending on a GPU box.

**Immediate next step:** finish M6.3b — the durable-config persistence flip, the manifest reconcile engine, the admin-only portal ownership view (`/_lemonseed/api/owui/config`) and **4B guard enforcement** (nginx include rendered from the manifest, admin manual reconcile, admin config-reset window) are landed and QEMU-proven. Next is **M6.2b user/group management** (which also owns flipping the two account guards to enforce), then LiteLLM friendly models. SearXNG/ComfyUI follow: their defaults land as M6.3b manifest entries + API writes.

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

### M6.4 — AMD GPU Vulkan support (design)

**Decision (2026-09):** M6.4 adds **AMD GPU support via the Vulkan backend** so `lemond` + `llama-server` can use AMD GPUs (dGPU and Strix iGPU) on user hardware. The scope is **Vulkan only** — ROCm and the new HRX backend (Lemonade 11.9) are deferred to the backlog as "AMD GPU driver optimization (ROCm/HRX)". Long-term the system should auto-detect and enable any combination of NVIDIA GPU, AMD dGPU, AMD iGPU, AMD NPU, or CPU.

**Why Vulkan:**
- **Broad coverage:** Mesa RADV (the AMD Vulkan ICD) supports RDNA1/2/3/4 dGPUs and Strix iGPUs with a single driver. No per-GPU driver selection needed.
- **Lightweight:** No ROCm closure (which is multi-GB and lags kernel updates). The Vulkan ICD is already in `pkgs.mesa` (default for `hardware.graphics.enable`).
- **Simple driver model:** The kernel `amdgpu` module ships in the base kernel. Userspace is just the Mesa Vulkan ICD + loader. No display server required.
- **Runtime dispatch:** The control plane selects the backend at runtime from detected GPU vendor (`select_backend`), so a single production image works on both NVIDIA and AMD hardware.

**llama.cpp release strategy (decision 2026-09-19):** We **build llama.cpp from source at image-build time**, pinned to a **tagged upstream release** — never from `HEAD`. The build is part of the release process, so every shipped image's llama.cpp is tested by `make test` + QEMU + real-GPU validation before it reaches a user. We never build from a moving `main`/`HEAD` branch.

- **Tag selection.** We track the **latest stable tagged release** of llama.cpp (e.g. `v0.4.1`, 2026-09-14). When we rebuild, we compare the latest stable tag against the one currently pinned in-tree and bump to it if newer. This keeps us from silently drifting behind the catalog Lemonade curates (the b9190→b10273 gap that first broke `eagle3` loads, and the b9190 multi-slot `common_context_seq_rm` abort that crash-looped lemond on NVIDIA). The tag is recorded in `lemond.nix` (`LLAMA_BUILD_NUMBER` + the `fetchFromGitHub` hash) so the build is reproducible and the rebase is a one-line, reviewable diff.
- **Single build, multi-arch, dedicated per-backend binaries.** One source tree is compiled **once per backend** with the matching architecture flags, producing the dedicated `llamacpp-cpu` / `llamacpp-cuda` / `llamacpp-vulkan` binaries that lemond invokes via its `llamacpp.cpu_bin` / `cuda_bin` / `vulkan_bin` config keys (each key takes a *file path* to a `llama-server` binary). We do **not** ship a single fat binary with all backends compiled in: lemond's config model is one binary per backend slot, a per-backend build keeps each closure lean (a Vulkan-only QEMU box never pays for the CUDA toolchain), and it mirrors how Lemonade itself ships one `llama-server` per backend. The CPU and Vulkan builds come from the **`nix-amd-ai` flake** (built from the same pinned nixpkgs as `lemonadePackage`, carrying their patches — e.g. the RDNA3.5 numerics fix from llama.cpp#28211); the **CUDA build is ours** because nix-amd-ai ships no CUDA package, so we `override` our nixpkgs `llama-cpp` to the same pinned tag with `cudaSupport = true` (source build, no binary cache). All three track the **same pinned build number** so they never drift from each other.
- **Backend selection is the control plane's job, not lemond's.** lemond is told *which* binary to exec via the `--llamacpp <backend>` flag the control plane passes at `lemonade load` time (see `catalog.select_backend`: NVIDIA→cuda, AMD→vulkan, else→cpu). lemond does not choose; it runs the binary we hand it. This is what makes a single production image work on any GPU vendor with runtime dispatch.
- **Upstream platform patch sets.** Where a platform maintains an out-of-tree patch set for llama.cpp (e.g. **AMD ROCm** via `lemonade-sdk/llamacpp-rocm`, a nightly ROCm-7 build pipeline with no source patches today), we pull and build those patch sets into **dedicated targets** that lemond can invoke as an additional backend slot (e.g. `llamacpp-rocm`), and the control plane's `select_backend` decides when to route to them. This is deferred to the backlog item "AMD GPU driver optimization (ROCm/HRX)" — Vulkan is the AMD path until then.
- **Provenance policy (supersedes the 2026-09-18 "never override separately" rule).** The 2026-09-18 rule that we must consume llama.cpp *only* from the `nix-amd-ai` input is **retired** because nix-amd-ai provides **no CUDA package**, so the CUDA build necessarily comes from our own source build. The new policy: **CPU + Vulkan track the `nix-amd-ai` flake ref** (bumped together with `lemonadePackage`), and **CUDA tracks a tagged upstream llama.cpp release** that we pin and rebase ourselves. The `nix-amd-ai` flake input stays pinned — do **not** `.follows` nix-amd-ai's nixpkgs (it would break the binary-cache closure hash and force source rebuilds).
- **Rebase procedure.** (1) Check the latest stable llama.cpp tag on GitHub. (2) If newer than `LLAMA_BUILD_NUMBER`, bump the tag + recompute the `fetchFromGitHub` hash in `lemond.nix`. (3) `make test` + `make build-qemu` + `make test-qemu`, then validate on one real-GPU box (NVIDIA for the CUDA build, AMD for Vulkan). (4) Commit. A rebase is a small, reviewable diff — never a silent drift.
- **Lemonade-patch tracking.** We watch the `lemonade-sdk` GitHub org for llama.cpp patches and other capability packages. Today the only llama.cpp-related artifact is the **ROCm build pipeline** (`lemonade-sdk/llamacpp-rocm`) — incorporated when ROCm lands. Other org packages (e.g. `lemonade-sdk/ryzenai-sd-server` for Ryzen stable diffusion) are **future capability candidates** to evaluate as we bring online more inference features.

**What is NOT in M6.4 (backlog):**
- **CUDA backend alignment (in progress, 2026-09-19):** The CUDA `llama-server` was pinned to nixpkgs 26.05's `pkgs.llama-cpp` (b9190), which lacks `eagle3` and other newer archs and crash-loops on lemond's multi-slot path. Per the llama.cpp release strategy above, the CUDA build is now **our own source build of a tagged upstream release** (initially the latest stable, `v0.4.1`), via `pkgs.llama-cpp.override { cudaSupport = true; }` with the tag pinned in `lemond.nix`. This fixes the `eagle3` load failure (GPT-OSS) and the b9190 `common_context_seq_rm` crash. The rebase procedure (bump tag + hash, `make test` + QEMU + one real-GPU box) is documented in the strategy section. Validating on the NVIDIA VM (RTX Pro 2000 Blackwell) is the gate.
- **ROCm/HIP backend:** `llama-cpp` with `cudaSupport`-equivalent for AMD (`hipSupport`). Requires the multi-GB ROCm closure and kernel module alignment. Deferred until Vulkan proves out on real hardware.
- **HRX backend (Lemonade 11.9):** AMD's new `llamacpp-hrx` backend is experimental, initially targeting gfx1100 (RDNA3) and gfx1151 (Strix Halo) only. Not in our pinned Lemonade 11.5.2 nor nixpkgs 26.05. The RX 6800 (gfx1030/RDNA2) is NOT covered by HRX's initial targets. Re-evaluate when HRX stabilizes and broadens GPU coverage.
- **AMD NPU (XDNA):** The `amdxdna`/XRT/FastFlowLM stack is not in nixpkgs 26.05 (only in the nix-amd-ai flake). Out of scope for M6.4.
- **Lemonade version bumps:** nix-amd-ai has since moved on (11.9 adds the HRX backend, Qwen3-Next support, and a newer llama.cpp pin). Rebase the `nix-amd-ai` flake ref when the HRX/ROCm optimization item is scheduled; that single bump moves lemonade + llama.cpp together.
- **Heterogeneous GPU scheduling:** Mixed AMD+NVIDIA boxes assign workloads to devices; no memory pooling. Post-M5 backlog.
- **CPU RAM reserve re-evaluation:** The APU split reserves a fixed 16GB for the CPU. Re-evaluate once multiple Docker modules run (image generation, etc.) so container memory is reserved without adding swap.
- **Serial console strategy:** Move the kernel console to `/dev/ttyS0` only (RS232 becomes the debug port), keep gettys on KVM for now, and later disable VGA gettys — the final product's VGA should show only a Lemonseed ASCII splash (URL, system/network status, instructions).
- **Installer log hardening:** `/var/lib/lemonseed/installer.log` is currently written by `install.py` only; extend to capture all install-invoked shell scripts (repart, release, amdgpu-kernel-params) via a shared `LEMONSEED_INSTALL_LOG` env convention, and ship the log to the appliance's journal at first boot.
- **MoE preference for low-bandwidth platforms:** When two models of similar size are available (one dense, one MoE), prefer MoE for iGPU/low-memory-bandwidth platforms and dense for dGPU/high-bandwidth. Relevant when the catalog has both variants (e.g. Gemma4 27B MoE vs Gemma4 31B dense).
- **GeoIP model preference:** Do a simple geoip lookup and prefer "local" model families when the user is in a country that produces them (e.g. China → Qwen, US → Gemma) to reduce perceived licensing/political friction.
- **Advanced model picker tab:** Expose the full `lemonade list` catalog in an "Advanced" tab so users with special requirements can pick off-menu models.

#### Implementation

**Nix modules:**
- `nixos/modules/amd-gpu.nix` (new): `hardware.amd-gpu.enable` option. Sets `hardware.graphics.enable = true` (defaults to Mesa/RADV Vulkan ICD), adds `pkgs.vulkan-tools` (vulkaninfo) to systemPackages. Kernel `amdgpu` ships in base kernel (no module needed). No display server.
- `nixos/modules/lemond.nix`: Added `"vulkan"` to `llamacppBackend` enum (now `["cuda" "vulkan" "cpu" "auto"]`). `enableVulkan = cfg.llamacppBackend == "vulkan" || cfg.llamacppBackend == "auto"`. Backend binaries come from the **nix-amd-ai flake** (`llama-cpp-vulkan` / `llama-cpp` for the Vulkan + CPU symlinks; `pkgs.llama-cpp.override { cudaSupport = true; }` for CUDA — see the provenance decision above). Added `vulkan_bin` to defaults JSON + jq re-apply + `environment.etc` symlink. Added `char-kfd` to `DeviceAllow`. `enableCuda` changed to `== "cuda" || == "auto"` (so a `vulkan`-only box doesn't pull the CUDA closure). `flake.nix` passes `nix-amd-ai` via `specialArgs`.
- `nixos/configuration.nix`: Both `lemonseed.nvidia.enable` and `hardware.amd-gpu.enable` default to `true` so the single production image works on NVIDIA and AMD. QEMU profile overrides both to `false` + `llamacppBackend = "cpu"`.
- `nixos/modules/amd-gpu.nix`: `lemonseed-amdgpu-kernel-params` oneshot service (runs `after` `lemonseed-ab-lock-in` so a param-triggered reboot lands on the now-default slot). Exports `LEMONSEED_APPLY_REBOOT=1` so the script reboots when the params changed; `SuccessExitStatus = [ "0" "1" ]` (reboot returns non-zero from the service's view).

**Strix Halo iGPU shared memory (AMD APUs only, PCI 1002:1586):**
- `scripts/amdgpu-kernel-params.sh`: reads *physical* RAM from DMI via `lshw -class memory -json | jq` (NOT `/proc/meminfo` — that reports CPU-addressable memory, already reduced by the GPU split). Allocates `physical_total − 16GB` to the GPU (min 4GB; systems < 32GB keep the default split). Writes a **GRUB stub** to the ESP: `/grub/kernel-params` containing `set kernel_params='ttm.pages_limit=… ttm.page_pool_size=…'` — GRUB has no command substitution and `export` is not a GRUB command, so `boot/grub.cfg` does `source /grub/kernel-params` and appends `${kernel_params}` to every menuentry's `linux` line.
- **Installer path:** `seed_esp()` runs the same script (lspci fallback for iGPU detection — no Vulkan ICD in the installer) *before* first boot, so a fresh install gets the correct split on boot #1 with **zero extra reboots**.
- **Update path:** the service runs after lock-in; if the ESP value differs from the running `/proc/cmdline` it writes the stub, drops `/var/lib/lemonseed/reboot-pending.json` (so the portal shows "Rebooting to apply GPU memory settings…" and keeps polling), and reboots once. The updater also refreshes the ESP `grub.cfg` on every update (new images ship the template at `/usr/share/lemonseed/grub.cfg`; older images had `/etc/lemonseed/grub.cfg` — the updater falls back across both) so stale ESPs self-heal.
- **Manual override:** `LEMONSEED_AMDGPU_SHARED_MEM_GB` env var or `/etc/lemonseed/amdgpu-shared-mem-override` (GB) pins the split instead of auto-calculating.
- **Installer logging:** the install pipeline logs to syslog (journal) and appends to `/var/lib/lemonseed/installer.log`; `_run()` mirrors every subprocess's output (repart, grub-install, amdgpu-kernel-params, …) into the log with the command line prefixed. At the end of the install the log is copied to the target's user-data at `/var/lemonseed/lemonseed/installer.log` so it survives the first boot.

**Control plane:**
- `hardware.py`: New `_amd_gpus_llama_cli()` parses `llama-cli --list-devices` output (reports real GTT/VRAM size, e.g. `56210 MiB` on a Strix Halo iGPU), with `_amd_gpus_vulkaninfo()` as fallback when the Vulkan llama-cpp binary is absent. `collect_hardware()` merges AMD GPUs with NVIDIA GPUs (dedup by vendor+name).
- `catalog.py`: New `select_backend(hardware)` — returns `"cuda"` for NVIDIA, `"vulkan"` for AMD, `"cpu"` otherwise. `plan_model` is now vendor-aware: gates on the *selected* backend's state (not hard-coded CUDA). VRAM fit is skipped when `vram == 0` (AMD Vulkan) and delegated to lemond.
- `catalog/models.json`: Model `backend` field changed from `llamacpp:cuda` to `llamacpp` (vendor-agnostic).
- `chat_ready.py`: `lemonade load` uses `select_backend(hardware)` instead of hard-coded `--llamacpp cuda`. `chat_snapshot()` reports `state="rebooting"` while the reboot-pending marker exists (portal keeps polling until the box is back); CP clears the stale marker at startup. **Load-failure give-up gate (2026-09-18):** lemond's internal Router retries a failed `llama-server` start indefinitely ("evicting all models and retrying"), and the CP worker re-issues `lemonade load` on every `/api/chat` poll — together they form an unbounded crash loop on deterministic load failures (e.g. `unknown model architecture: 'eagle3'`). Fix: `prepare_local_chat` now counts *load* failures (pull/load/health) in addition to *verify* failures. When the cap (`_MAX_VERIFY_FAILURES = 3`) is reached, the model is marked in `_gave_up_models` (keyed by hardware ref). Subsequent non-force calls short-circuit with a terminal error ("failed to load on this hardware. Try a different model family in Settings.") without re-issuing `lemonade load`. The gate is cleared when (a) the user makes a new model choice (`POST /_lemonseed/api/models` → `clear_gave_up()`), or (b) the hardware reference changes (GPU upgrade). `force=True` (explicit user retry) bypasses the gate.

#### Testing

| Tier | Target | Command |
|------|--------|---------|
| **A — unit** | `select_backend` (nvidia→cuda, amd→vulkan, no-gpu→cpu), `plan_model` AMD/NVIDIA/no-GPU cases, `vulkaninfo` parsing, `collect_hardware` merge | `make test` (no root, no GPU) |
| **C — QEMU HTTP** | CPU-profile appliance boot (no GPU in QEMU); verifies the image builds and boots with the Vulkan closure present but unused | `make test-qemu` (needs `make build-qemu`) |
| **D — GPU lab** | Real AMD hardware validation: `vulkaninfo` lists the GPU, `lemond` loads a model on the Vulkan backend, chat completes | Manual on RX 6800 (gfx1030) / Ryzen AI 385 (gfx1151) |

**VRAM reporting:** AMD Vulkan GPUs report real memory via `llama-cli --list-devices` (shipped as `/etc/lemonseed/lemond-backends/llamacpp-vulkan-cli` from the Vulkan llama-cpp closure). On APU iGPUs this is the GTT allocation (shared system RAM), not dedicated VRAM. The planner uses this for model-fit validation. `vulkaninfo` fallback (no memory size) is used only when the llama-cli binary is absent.

#### Acceptance checklist
- [x] `make test` green (96 control-plane tests including 11 new AMD/Vulkan tests)
- [x] `vulkaninfo` detects AMD GPUs (dGPU + Strix iGPU) with `vendor: "amd"`
- [x] `select_backend` returns `"vulkan"` for AMD, `"cuda"` for NVIDIA, `"cpu"` otherwise
- [x] `plan_model` is vendor-aware (gates on selected backend, not hard-coded CUDA)
- [x] `chat_ready` uses `select_backend` for `lemonade load`
- [x] Single production image includes both CUDA and Vulkan closures (`llamacppBackend = "auto"`)
- [ ] QEMU smoke test green (CPU path; no AMD GPU in QEMU)
- [ ] Real-hardware validation: RX 6800 (gfx1030) — `vulkaninfo` + lemond load + chat
- [ ] Real-hardware validation: Ryzen AI 385 (gfx1151 iGPU) — `vulkaninfo` + lemond load + chat

### M6.5 — Model picker (design)

**Decision (2026-09):** M6.5 adds a **curated model-family picker** to the control panel so users can choose which model family to run, rather than relying solely on the automatic planner. The picker shows four families (Gemma, Nemotron, Qwen, LFM2) with maker, country, flag emoji, and a one-line description. (GPT-OSS was removed from the catalog — it segfaults on every backend we tested, a Lemonade-side incompatibility rather than a hardware issue.) **Gemma is the default family** (trusted US vendor + wide size range); Qwen remains available as a non-default option. For each family the picker shows the **best-fit model** for the current hardware (largest model that fits VRAM + RAM). The user's choice is persisted to `/var/lib/lemonseed/model-choice.json` and honored by the planner while it still fits; if hardware changes (e.g. GPU upgrade) and the chosen model no longer fits, the planner falls back to the default family's best fit.

**Catalog (v8):** `catalog/models.json` has a `families` array (4 families with `id`, `name`, `maker`, `country`, `flag`, `description`; Gemma is `default: true`) and a `models` array (13 entries) with a `family` field per model. Fit numbers (`min_vram_gb` / `min_ram_gb`) are estimates calibrated from `lemonade list` weights; they should be sanity-checked on real hardware. v8 swaps the LFM2 1.2B dense model for its **LFM2.5 1.2B Instruct** successor (the LFM2.5 variant is the current generation; LFM2 is superseded) and drops **LFM2 24B A2B** (superseded by the larger Gemma/Qwen options in the 14–24 GB slot; the 24B MoE is not worth the extra catalog surface until we have a hardware class that needs it). The LFM2 family is now: LFM2.5 8B A1B (rank 40, MoE) + LFM2.5 1.2B Instruct (rank 48, dense — the CPU/embedded-GPU option).

**Generation-aware ranking:** Within a family, newer generations always outrank older ones, and an older generation is only kept when it opens a **new size class** the newer generation lacks (e.g. Qwen3.5 4B/9B were added because they fill the 4–10 GB slot below Qwen3.6). Older-generation models that are strictly superseded (Qwen3 4B/8B/14B, Gemma 3 4B, LFM2 1.2B → LFM2.5 1.2B, LFM2 24B A2B) are **not** in the catalog — `rank` alone must never let an older model win over a newer one. Only Qwen 3.5+ is offered (Qwen3.x models are superseded); only LFM2.5 is offered (LFM2 is superseded). Ranks are assigned accordingly (lower = better): Qwen3.6-35B (20) → Qwen3.6-27B (25) → Qwen3.5-9B (42) → Qwen3.5-4B (45) → LFM2.5 8B A1B (40) → LFM2.5 1.2B (48) → Gemma-4-31B (65) → Gemma-4-26B-A4B (70) → Gemma-4-12B (75) → Gemma-4-E4B (90) → Gemma-4-E2B (100).

**RAM fit uses rounded system RAM:** the hardware report rounds `MemTotal` to the nearest whole GiB (`_mem_total_gb`), because MemTotal is always below installed DIMM size (kernel reservation, PCIe/VA space, iGPU GTT carve-out — 8 GiB boxes report ~7.6–7.7). Nearest-GB keeps `min_ram_gb` floors honest without over-crediting.

**Best-fit logic:** `best_fit_model(family_id, hardware)` filters the family's models to those that fit the largest GPU's VRAM + system RAM, then picks the **lowest rank number** (best model). When VRAM is unknown (`vram == 0`, e.g. AMD Vulkan without `llama-cli`), the VRAM check is skipped and lemond decides at load time.

**Planner integration:** `plan_model()` honors an explicit user choice (family + optional model) while it fits, else picks the default family (Gemma) best-fit, else falls back to the best overall catalog model that fits (e.g. E2B on tiny hardware), else `not-ready`. The plan now includes a `family` key.

**Download speed estimate (removed):** the picker once carried a throughput probe (`measure_download_speed()` + `download_estimate()` + `GET /_lemonseed/api/models/estimate`) that guessed an ETA. It is gone: the probe URL 404'd, and re-checking the pinned lemond server before deleting showed **no download-progress API of any kind** (every candidate progress route answers `The requested endpoint does not exist`; model detail carries only `downloaded` / `size` / `checkpoint`), so no ETA is computable from our side — see §11, *Download-ETA speed probe*. The preparing message now names the model and its size only ("Downloading Google Gemma for your hardware (~4.1 GB)…"). Live rate for the household is the toolbar **NET** meter in §11.

**Model disk-space maintenance:** The model cache lives on the durable user-data volume (`/var/lib/lemonseed/models`), so a series of large downloads could fill the filesystem. Before each `lemonade pull`, `model_store.ensure_model_space()` guards the download: it reads free/total bytes for the cache (from lemond's `GET /v1/system-info` → `model_storage`, falling back to `shutil.disk_usage`) and, if the download would push the filesystem to **≥ 90% full**, evicts old model subdirectories **LRU** (oldest directory `mtime` first) until there is room — while always preserving (a) the target model's own directory, (b) the **single most-recent prior model** as a fallback in case the new download fails, and (c) any model **currently loaded** in lemond (from `GET /v1/health`; a resident model is never evicted). Per-model sizes come from `GET /v1/models/{id}/files` (sum of `size_bytes`), falling back to a filesystem walk. A **10% free reserve** is always kept. If the new model cannot fit while still keeping that one fallback model, the download is **refused** with a user-facing error ("Insufficient disk space to download this model …") rather than risking a full disk. If disk usage cannot be read from either source, the download is refused for the same reason. LRU ordering uses directory `mtime` as a coarse proxy — lemond exposes no per-model access timestamp.

**Catalog id ↔ directory name translation:** lemond's HTTP API is keyed by the **catalog id** (e.g. `Gemma-4-E2B-it-GGUF`), but each model is stored on disk under `models--<org>--<repo>` (derived from the catalog entry's `checkpoint` field, e.g. `unsloth/gemma-4-E2B-it-GGUF:Q4_K_M` → `models--unsloth--gemma-4-E2B-it-GGUF`). The disk-space guard walks directories, so it must translate between the two before calling `model_files()` / `delete_model()` — passing a directory name to the API yields 404/422 and forces the unsafe `rmtree` fallback. `catalog.model_dir_name()` provides the forward mapping (catalog id → dir); `_catalog_id_for_dir()` provides the reverse. The loaded-model set from `GET /v1/health` is also translated from catalog ids to directory names so the guard never evicts a resident model.

**rmtree safety rule:** a raw `rmtree` of a model directory is only attempted when lemond is **genuinely unreachable** (no SDK client, or the `system-info` round-trip fails). When lemond is alive but its `delete_model()` API call fails (model busy, in a transitional state, etc.), the guard **skips** that model instead of falling through to `rmtree` — deleting a directory from underneath a running lemond orphans its `user_models.json` metadata, leaving a phantom entry in `lemonade list` with no files on disk (observed on hardware: a pinned model was `rmtree`'d mid-GC and lingered in `lemonade list` until lemond lazily rebuilt its cache). GC continues with the next-oldest model; if every candidate is refused, the download is refused with the insufficient-space error rather than force-deleting anything.

**lemond API over CLI (2026-09):** The control plane interacts with lemond via its HTTP API rather than the `lemonade` CLI wherever possible. We use the `lemonade-python-sdk` (unofficial, MIT, pure-Python, depends only on `requests`) as the HTTP client, extended by a thin subclass in `control-plane/lemond_client.py` for the three endpoints the SDK lacks (`POST /v1/delete`, `GET /v1/system-info`, `GET /v1/models/{id}/files`) plus a `loaded_model_names()` helper over `GET /v1/health`. The CLI is itself a thin HTTP client, so wrapping it in a subprocess only adds a process spawn and a parsing layer for no benefit. The API is also the only **safe** way to delete a model: `POST /v1/delete` unloads the model first if it is loaded, whereas a raw `shutil.rmtree` deletes from underneath a running lemond. The SDK wheel is not in nixpkgs, so it is vendored as a local `fetchPypi` + `buildPythonPackage` override in `pkgs/lemonade-python-sdk.nix` (pinned to a release tag, never `latest`).

**Picker-first gate (awaiting-choice):** A fresh box must **not** auto-download the default model before the user has chosen. `catalog.choice_exists()` returns true only once `/var/lib/lemonseed/model-choice.json` exists — so "no choice yet" (first boot) is distinct from an explicit family pick (which writes the file with the chosen family id; Gemma is the default). `prepare_local_chat()` gates on it: when no choice file exists (and not forced), it writes a new chat state `awaiting-choice` (workflow "Choose a model") and returns **before** planning/pulling. The portal **landing** renders the model picker inline in this state (see Portal UI), and the Settings page shows it permanently. After the user picks a family via `POST /_lemonseed/api/models`, the choice file is written and the worker proceeds to pull with the friendly label. `force=True` (manual re-prepare) bypasses the gate.

**API endpoints (nginx-gated):**
- `GET /_lemonseed/api/models` — families view (per-family best-fit, fits, selected) + current plan. Auth + admin gate.
- `POST /_lemonseed/api/models` — save user choice (`{family, model_id?}`). Auth + admin gate. Triggers `ensure_chat_worker()` to re-plan and pull if needed.

These are namespaced under `/_lemonseed/` (not `/api/models`) on purpose: Open WebUI's own frontend also calls `GET /api/models` for its OpenAI-style model list, and nginx's exact-match `= /api/models` location would win over the `/` prefix that routes to OWUI — silently returning the CP's picker payload to OWUI and leaving its model dropdown empty. Keeping the CP picker on a `/_lemonseed/` path leaves `/api/models` free for OWUI.

**Portal UI:** The model picker is a **card grid** rendered in **two places**: (1) inline on the first-login **landing** (the "Getting Ready" bubble) while the box is in `awaiting-choice`, so a fresh user picks a model as part of the download experience instead of being sent to Settings; and (2) permanently in the **Settings** page under the **"Lemonade AI Engine"** box (renamed from "Local inference" so it can host other Lemonade status/settings later). Each card is itself the button: a narrow flag column, a ~30%-width name column in the form `$COMPANY $FAMILY ($SIZE)` where `$SIZE` is the best-fit model's weight in GB (e.g. "Alibaba Qwen (23.3 GB)") — the raw model id (e.g. "Qwen3.6 35B A3B") is deliberately not shown as it is meaningless to end users; the name wraps rather than truncates — and a 2–3 sentence plain-English description column. Hover lights the 1px border in the accent color; the selected card gets an accent border + ~14% accent-tinted background. Unfit families are dimmed and non-clickable. The landing picker hides automatically once the user picks a family and the state leaves `awaiting-choice`.

**LMX Omni (deferred):** LMX models are monolithic `collection.omni` bundles (LLM + image gen + STT + TTS). On Lemonade 11.5.2 the bundle **pulls** fine, but `lemonade load` never brings the collection up: lemond's load path logs `Skipping unknown component: Flux-2-Klein-9B-GGUF / Whisper-Large-v3-Turbo / kokoro-v1` (the pull path registers them as `user.*` models, the load path doesn't see them), so the collection never appears in `/v1/health` and `chat_ready` retries forever. The LMX family and both LMX models were removed from the catalog (Gemma is now the default family). **TODO: get omni (`collection.omni`) models working** — suspected gap in our custom headless lemond install (missing image/STT/TTS runtime components or config) rather than a Lemonade bug; re-test on Lemonade 11.9, then re-add the family to the catalog with whole-bundle fit accounting (52B-Halo is ~44.8 GB total, mostly the Flux image model).

#### Implementation

**Control plane:**
- `catalog.py`: New `families_view(hardware)` (picker payload), `best_fit_model(family_id, hardware)` (lowest-rank fit), `read_model_choice()` / `write_model_choice(family, model_id)` (persistence at `/var/lib/lemonseed/model-choice.json`), `resolve_plan_choice(hardware)` (honors stored choice while it fits). `plan_model()` rewritten to use family-based selection with fallback.
- `chat_ready.py`: `_user_choice_active(hardware, model_id)` (user pick survives lemond restart), `_auto_plan_stale(hardware)` (re-plan after hardware upgrade when no explicit choice). Skip-reload condition updated. `prepare_local_chat()` gates on `catalog.choice_exists()` — no choice file → `awaiting-choice` state (no pull). New chat state `awaiting-choice` + `_workflow_for_state` mapping.
- `stub.py`: New `GET /_lemonseed/api/models`, `POST /_lemonseed/api/models` (admin-gated). Settings page: model picker section with family cards + JS.

**Gateway (nginx):**
- `gateway.nix`: New `locations."= /_lemonseed/api/models"` (authGate + adminGate + cpProxy + interstitial).

#### Testing

| Tier | Target | Command |
|------|--------|---------|
| **A — unit** | `best_fit_model` (rank ordering, fit filtering), `families_view` (best-fit, selection, unfit), `plan_model` (honors choice, auto default, fallback, not-ready), choice roundtrip | `make test` (no root, no GPU) |
| **C — QEMU HTTP** | CPU-profile appliance boot; picker payload shape (`/_lemonseed/api/models` 4 families + plan). The 16 GiB guest fits Gemma E4B on CPU, so the plan is `selected` — asserted as such | `make test-qemu` (needs `make build-qemu`) |
| **C+ — QEMU model-load e2e** | Opt-in full path: pick Gemma 12B explicitly via `POST /_lemonseed/api/models` (`model_id`), pull + load the model, assert `/api/chat` reaches `state=ready` with `Gemma-4-12B-it-GGUF`. Weights cached on the persistent userdata disk so re-runs are fast | `QEMU_E2E_CHAT=1 make test-qemu` |
| **C++ — QEMU GC e2e** | Opt-in disk-space guard e2e: after the 12B is loaded, pull LFM2.5 8B (second cached model), fill `/var` to ~95% via a test-only CP filler endpoint, pull LFM2.5 1.2B, and assert the LRU model (LFM2.5 8B) was evicted via lemond's API while the pinned Gemma 12B survives. Adds ~13 GB of downloads — run on a clean 32 GB userdata disk | `QEMU_E2E_CHAT=1 QEMU_E2E_GC=1 make test-qemu` |

#### Acceptance checklist
- [x] `make test` green (unit tests for planner, picker, GC algorithm)
- [x] QEMU smoke test green (CPU path; picker payload + `selected` plan at 16 GiB guest)
- [x] QEMU model-load e2e green (`QEMU_E2E_CHAT=1`: pick Gemma → pull + load → `/api/chat` `state=ready` with `Gemma-4-E4B-it-GGUF`)
- [ ] QEMU GC e2e implemented (`QEMU_E2E_GC=1`: pull 2nd model → fill /var → pull 3rd → assert LRU eviction via lemond API; pinned model preserved) — **not yet run**
- [x] Hardware validation: GC routine works on the NVIDIA box (LRU eviction keeps the pinned model; the earlier pinned-model `rmtree` race is fixed)
- [ ] Real-hardware validation: picker shows correct best-fit per family on a GPU box; user choice persists across reboots; download estimate is reasonable

**Backlog:** moved to §11 (consolidated) — groups *Models & chat UX* and *Portal & platform UX*.

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

**AMD / Ryzen AI Max (M6.4):** Vulkan backend via Mesa RADV ICD. `llama-cli --list-devices` detects AMD GPUs and reports real GTT/VRAM size; `vulkaninfo` is the fallback. See **M6.4** section for details. ROCm/HRX optimization is backlog. NPU (XDNA) is out of scope for M6.4.

**Heterogeneous GPUs (post-M5):** physical devices stay separate. Planner assigns **workloads to devices**; mixed AMD+NVIDIA is **not** pooled memory for one model.

#### Capability planner + catalog
Catalog is a versioned artifact (served/signed like other release metadata) with entries such as:
- model id, backend (`llamacpp:cuda`, …), quantization, weights size, license/hash
- minimum accelerator memory + host RAM reserve + context/KV cost notes
- rank / quality tier from our benchmarks

Planner picks the highest-ranked entry that fits **one** supported execution target. When a GPU is present, `min_vram_gb` applies to VRAM and `min_ram_gb` to system RAM (OS + KV spillover). When no GPU is detected, CPU inference is used and the requirement becomes `ram >= min_vram_gb + min_ram_gb` (weights + overhead all in system RAM), which naturally restricts selection to the smallest models (e.g. E2B, E4B). System RAM is rounded to the nearest whole GiB before comparison (see M6.5).

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
- **llama.cpp must come from the `nix-amd-ai` flake** (the same source `lemonadePackage` builds against), never an independent pin — see the M6.4 provenance decision. An independent llama.cpp drifts behind the model architectures Lemonade's curated catalog ships (first casualty: `eagle3` in GPT-OSS GGUFs).
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

## 11. Backlog (consolidated review)

One home for deferred ideas, grouped for periodic review; promoting an item means assigning it a milestone. Origin in brackets; superseded/done work lives in the decision log, not here.

**Models & chat UX**
- **Custom model catalog:** friendly display names (e.g. flagged labels), Lemonade `user_models.json` / registry overrides so Open WebUI and Lemonade agree — not alias hacks at load time. [was M6]
- **LiteLLM friendly model names (M6.3b):** stable, friendly models (“Lemonseed AI”, “Lemonseed AI with Content Filters”) fronting lemond, so model name/path changes never churn OWUI model config; the friendly models become the manifest-owned entities (`lemonseed-ai*` ids under the reserved `Lemonseed *` name namespace). Links to `account_type` content filtering (see Account types below) — this replaces the `DEFAULT_MODEL_METADATA`/`DEFAULT_MODEL_PARAMS` env-era approach. [was M6.3b]
- **Account types (child/adult):** per-account indicator for content filtering (OWUI policy/prompt layering); `users.account_type` column reserved in M6.2, population + policy later. [was M6]
- **MoE vs dense preference by platform:** Prefer **MoE** models over dense models of similar size on low-memory-bandwidth platforms (iGPUs), and dense on high-bandwidth dGPUs. Only relevant when a family has two similarly-sized options, one MoE and one dense (e.g. Gemma 4 26B-A4B MoE vs Gemma 4 31B dense). Needs a `moe: true` flag on catalog models + a bandwidth heuristic (or explicit dGPU/iGPU class) in `best_fit_model`. [was M6.5]
- **GeoIP model preference:** Do a lightweight GeoIP lookup and prefer "local" model families for the user's country (e.g. Qwen for China, Gemma for the US) when ranking the default/automatic pick. [was M6.5]
- **Advanced model tab:** an "advanced" picker tab exposing the full `lemonade list` catalog so power users can pick off-menu models. [was M6.5]
- **Catalog models not in the Lemonade catalog:** support loading models that are in our catalog but not yet offered by the main Lemonade catalog (e.g. Qwen3.8 before Lemonade curates it). Requires a direct HF checkpoint path + `lemonade load` with an explicit checkpoint (bypassing Lemonade's model resolution), plus the llama.cpp arch-support caveat (our pinned llama.cpp must support the model's architecture). [was M6.5]
- **Local Wikipedia cache:** experimental indexed Wikipedia snapshot on spacious, well-connected appliances as an optional knowledge source for OWUI (when native KB tools are re-enabled). [was M6]
- **More complete user profiles:** full name, email, etc. (low priority; username is sufficient for now). [was M6.5]
- **Model weights must survive an upgrade (landed; diagnosis corrected):** the bare-metal report was "the LLM re-downloaded after an upgrade", but the journal proves **no bytes moved** — lemond logged `File already exists and hash verified` for both weight files, and the ~96 s was its Hugging Face file-list plus a SHA-256 re-verification of the 7.3 GB file. The CP asked for that `pull` because `prepare_local_chat`'s fast path keyed on the model being *resident in memory* (always false after a reboot), and the progress copy then announced a download. Landed: read lemond's `downloaded` flag (`GET /v1/models/{id}`, verified against the pinned 11.5.2 server) before deciding, skip both the pull and the disk-space guard when the weights are cached, say "…is already on disk — loading it…" instead of "Downloading…", and fall back to exactly one repair `pull` if a cached model refuses to load. The earlier orphaned-pre-policy-directory theory is dead: the cache holds exactly one correctly-named directory. Still needs a bare-metal reboot to confirm the ~96 s is gone. [new 2026-10, corrected 2026-10-08]
- **Download-ETA speed probe: removed, not fixed.** `catalog._SPEED_PROBE_URL` (a "small" Gemma GGUF on Hugging Face) returns **404**, verified 2026-10-08, and `measure_download_speed()` swallowed it — so the ETA never populated and every uncached prepare wasted one doomed HTTPS request (10 s timeout) before starting a real download. Before deleting, the premise was re-tested against the **pinned** lemond 11.5.2: `openapi.json` is disabled (404) and every candidate progress route (`/api/v1/download/{id}/status`, `/download-status/{id}`, `/downloads/{id}`, `/progress`, `/models/{id}/download`, `/pull/status`) answers `404 {"error":{"message":"The requested endpoint does not exist"}}`; model detail carries only `downloaded` / `size` / `checkpoint`, and `/api/v1/jobs` is the batch-job runner, not download progress. **No rate or ETA is computable from our side.** Landed: the probe, its `/run/appliance/net-speed.json` cache and `GET /_lemonseed/api/models/estimate` are deleted (unit tests, the nginx location and the QEMU assertion with them); the preparing message states size only. Live rate moves to the toolbar **NET** meter below, which reads `/proc/net/dev` and cannot rot this way. If lemond ever exposes per-download bytes, revisit *with* a progress API rather than a guess. [new 2026-10-08, resolved by removal 2026-10-08]
- **No silent model re-fetch (landed):** lemond ships `auto_check_model_updates=true`, which re-downloads a model when its upstream revision moves — turning a reboot into a multi-GB download that fails outright offline. Now pinned false in both the defaults file and the every-start `config.json` policy rewrite. **Confirmed on the prototype 2026-10-09:** a reboot goes straight to *Loading … onto the GPU* with no re-fetch. [new 2026-10, confirmed 2026-10-09]
- **Catalog checkpoints must match the upstream registry exactly:** our catalog said `unsloth/gemma-4-12B-it-GGUF` where the pinned lemond registry has `gemma-4-12b-it-GGUF`. Downloads were unaffected (lemond resolves its own registry), but `checkpoint` is what derives the cache directory name, so the disk-space guard could neither protect the target nor recognise a loaded model. Guard comparisons are now casefolded (`catalog.dir_key`); the missing piece is a build-time check that every catalog `checkpoint` exists in the pinned `server_models.json`. [new 2026-10]

**Open WebUI integration (M6.3b)**
- **Guard surface expansion:** promote more sections to enforce mode once portal usage data shows what admins actually try to change. Day one (4B): five mutating OWUI paths enforce; the two account guards stay advisory until M6.2b.
- **Synthetic identity emails are immutable — the domain is now a named constant:** Open WebUI's trusted-header provider keys identity on **email**, so changing an existing account's domain does not rename it, it silently creates a duplicate on next sign-in (with its chats orphaned under the old row). That rules out hostname-derived addresses (the appliance renames itself after the factory `lemonseed.local` handoff). Day one now mints *every* identity — household users and the control-plane service account — under one domain, `auth.OWUI_EMAIL_DOMAIN = "localhost"`, because `localhost` is the only no-dot domain OWUI's own `validate_email_format` exempts: `@lemonseed` would be rejected by `POST /api/v1/auths/add`, so OWUI's admin UI could never create or edit such a row. Nothing on the appliance sends mail, so resolution is irrelevant. **Migration note:** the value is read from the stored user row, never re-derived, so upgrading boxes keep their existing `@lemonseed.local` rows — changing them would duplicate accounts, and the superseded row is OWUI's *earliest* user, which it makes immune to modify and delete. Revisit only before appliances reach fields we cannot ask someone to reinstall. [new 2026-10, domain landed 2026-10]
- **OWUI auth-provider config is admin-editable:** an admin can add LDAP/OAuth/SCIM config in Open WebUI's own admin UI (guards do not cover it), and OWUI's own OAuth is already half-configured by our trusted-header setup. It cannot actually be used — every Open WebUI API call traverses the Lemonseed session gate — and a settings reset clears it, so day one leaves it alone. Decide whether to guard those paths or keep relying on reset. [new 2026-10]
- **Directory authentication (choose the layer before building):** add LDAP/OAuth/enterprise-directory sign-in for household accounts. Unresolved: whether that rides OWUI's built-in LDAP/OAuth providers (admin-editable today, unguarded, and it bypasses our portal session + TOTP story), a CP-native directory integration, or host-level PAM/sssd with the CP as the only consumer. Must not weaken the current invariant that the nginx `auth_request` gate is the sole login path. [new 2026-10]
- **Anonymous-caller exemption for guards (A2):** an admin whose session expired mid-form gets “managed by Lemonseed” instead of a sign-in prompt. Fallback: require a non-empty `$cookie_lemonseed_session` in the rendered pattern so anonymous traffic falls through to `auth_request` and gets the redirect. Costs a subtler pattern per guard and adds a way to shoot ourselves (a cookie rename would silently disable enforcement), so it stays out of 4B. Trigger: the §8 bare-metal session-expiry spot check feels wrong in practice. [new 2026-10]
- **LKG restore as an admin action:** the last-known-good config snapshot is used today only to recover a *broken* reset. “Restore my settings” as its own `POST …/restore-config` is not built, because a reconcile rebuilds everything Lemonseed owns anyway. [new 2026-10]
- **Config-table snapshot for a failed reset:** before `TRUNCATE TABLE config`, copy the table so a reset that cannot rebuild can be rolled back byte-for-byte instead of relying on the version-gated LKG snapshot. Add this only if reset proves buggy in practice. [new 2026-10]
- **Manual reconcile button may be unnecessary:** the 4B “Apply Lemonseed settings now” endpoint exists as the escape hatch for “an admin broke something and wants it back now”. If we never use it, delete it (P4-D). [new 2026-10]
- **Selective connection management:** let OWUI admins create their own model connections alongside ours (Lemonseed owns its entry, others untouched). Needs per-entry merge semantics on `openai.api_configs` — deferred; day one writes the whole set via raw import and enforces exactly one connection. [was M6.3b]
- **Body-aware request-time guards:** block OWUI-UI writes by request *content* — e.g. reject `POST /api/v1/models/create` when the body's `name` matches `^Lemonseed.*$` — instead of sweeping collisions after the fact at reconcile. Path-only nginx guards cannot see bodies; needs an nginx lua module or a small CP-side validation shim in front of OWUI. [new 2026-10]
- **SCIM evaluation:** revisit if trusted-header provisioning shows friction (see the M6.3b risk table). [was M6.3b]
- **Group namespace / destructive header sync (gates M6.2b):** trusted-header sign-in re-syncs OWUI group membership on **every** sign-in — `Groups.sync_groups_by_group_names` (auths.py) deletes memberships missing from `X-Forwarded-Groups`, so a group an admin creates in OWUI silently loses our users at their next sign-in. Upstream skips the sync when the header is empty, so the options are: stop sending the header (groups become OWUI-owned), or send a closed, CP-provisioned namespace so the sync stays additive. Must be settled before we build group management. [new 2026-10]

**Accounts & data lifecycle**
- **Optional real email addresses + basic SMTP:** synthetic `@localhost` identities are an internal join key today. Let users optionally attach a real address to their account, and give the appliance minimal outbound SMTP (password-reset and notification mail) — which needs real addresses to be per-account rather than derived, and probably a CP-managed SMTP relay credential. The control-plane service account never needs this. [new 2026-10]
- **Tombstoned user deletion (undelete window):** mark a deleted account unusable immediately (tombstone), then an async worker wipes it across CP + OWUI + pending backups after a ~7-day undo window. Supports “oops” recovery and a staged right-to-erasure pipeline; must be evaluated against jurisdictional wipe-within-N-days rules (the tombstone revokes access instantly, but data persists during the window). Day one is immediate hard delete on the delete action. [new 2026-10]

**Portal & platform UX**
- **Power controls in the portal:** user-friendly **Reboot**, **Shut down**, and (when an update is staged) **Update** actions. Today these need a `tty2` console, which is clumsy for a home appliance. Needs an admin-gated CP endpoint (`POST /_lemonseed/api/power/{reboot|shutdown|update}`) plus a confirm step and a "the box is going away" state in the shell, and the reboot path must interlock with the A/B boot-attempt bookkeeping so a planned reboot is not counted as a failed update. **Scheduled for the next sprint.** [new 2026-10-08]
- **Ownership report as a build artifact, not admin UI:** the config-ownership classification (`owui_portal.build_view`: per-key mode, entity ownership, guard enforcement) is developer/agent detail — the portal stopped rendering it after bare-metal review, but the payload is still assembled at request time. If anyone needs to read it, generate it at build time (e.g. a text dump next to `version.json`) so the runtime payload can shrink and the detail cannot creep back into the UI. [new 2026-10-08]
- **OpenAI-compatible endpoint toggle:** let an admin expose the appliance as an OpenAI-compatible endpoint so other apps on the LAN can point at the Lemonseed. Two candidate implementations: OWUI's own OpenAI-compatible server surface, or a CP-side endpoint in front of lemond (the LiteLLM work in this milestone makes the latter cheaper). The portal's `API` panel is where the toggle belongs. Constraint already decided: `auth.enable_api_keys` is **Lemonseed-managed** (the CP's own `sk-` key depends on it) while per-user API keys stay user-owned, so the toggle must not fight that. Needs a call on which engine serves it and what credential LAN clients present. [new 2026-10-09]
- **Network throughput meter:** Add a **NET** section to the app toolbar (alongside LOAD + STORAGE) showing Tx/Rx rate in bytes/s over a trailing ~10s average, plus a bar where 100% = interface link speed. Read the default-route interface (`ip route get 8.8.8.8`) then sample `/proc/net/dev`. Build for Ethernet first; keep it modular so WiFi can be tuned separately. Expose rates via a small CP API (e.g. extend `/api/status` telemetry). [was M6.5]
- **Internationalization & accessibility:** Support for multiple languages and dialects, number units, and for assistive technology (e.g. image ALT tags, screen reader support). [was M6]
- **UX Theme Support:** Enable multiple preset themes (e.g. light mode, dark mode, professional, playful) to give the website a specific look and feel to align with the user preferences. [was M6]

**Modules & infrastructure**
- **CP-owned module lifecycle:** Docker module containers use `--restart no`; CP starts/replaces them after boot and on policy drift (blue/green + nginx reload). A long-lived supervisor starts a missing container and force-recreates after **5 contiguous** failed HTTP health probes. Continue hardening rollback (failed cutover keeps old slot) rather than relying on Docker auto-start of stale Envs. [was M6]
- **Live API health:** CP status fields (`/api/data`, `/api/status`, subsystem snapshots) should report **current health** via cheap live checks (e.g. `pg_isready` / `SELECT 1`, Valkey PING, HTTP ready), not a one-shot or cached reflection of “daemon started once.” If Postgres stops accepting connections after boot, the API should flip to unhealthy. [was M6]
- **Module catalog specs:** Define a Lemonseed **module configuration file** (one file per module under a catalog directory in-tree). Each spec encapsulates execution params (docker-compose fragment / pinned image), UX (logo, name, description, help), and dependencies (Postgres, Valkey, future object store). Catalog entries are what users see and what the CP downloads/sets up. The catalog itself needs a **test/release cycle**: pull candidate images → integration test → advance the version pin only when validated.
  * We should be able to host this file on lemonseed.ai or github and allow lemonseeds to download updates to the file so that new catalog offerings & versions come online without users needing to do a full lemonseed upgrade. 
- **Module image pins (strategy):** Every Docker module tracks the **newest upstream stable** tag we have validated. Keep an explicit pin in code (Open WebUI: `ghcr.io/open-webui/open-webui:v0.11.0` via `OWUI_IMAGE_DEFAULT` in `control-plane/modules.py`) so field appliances never float on `:latest` / `:main`. The build/rebase pipeline should regularly bump pins to current stable, run unit + QEMU smoke (and hardware when the module is user-facing), then land the pin — not leave modules on stale releases for “safety.” Lab overrides use `LEMONSEED_OWUI_IMAGE` only. [was M6]
- **Registry reachability preflight:** `network-online.target` can be reached while DNS is still broken (or the WAN link is down); a `docker pull` then fails with an opaque `no such host` and the supervisor would hammer the registry every 10s. Before pulling, `modules._pull_image()` checks the registry host (DNS + TCP 443, cached 60s) and, when unreachable, surfaces a user-actionable message ("Cannot reach the container registry — check this appliance's internet connection. Retrying automatically.") on the Getting Ready / settings pages. A single diagnostic `docker pull` probe per 60s keeps the journal informative without hammering the registry; the box self-heals when connectivity returns. [was M6]
- **Backup / restore:** Transactionally clean, space/time-efficient backup including Postgres dump, Valkey dump, and per-module **durable** volumes declared in the module spec. Targets: USB (preflight free-space check; optional encryption) and local SMB/NAS shares. [was M6]
- **Installer stream-to-disk:** partition first, stream squashfs to `root-a`, avoid multi-GB RAM staging on the live ISO. [was M6]
- **Direct access to OpenAI compatible API:** Allow users to configure & toggle LAN access to the LiteLLM based API, including simple key management for security. This can run on a high port so that it does not traverse our auth layer.
- **Lemonseed update/shutdown/restart controls:** The user control panel should allow users to shutdown or restart the Lemonseed. When updates are available, the user should be able to apply them from the control panel. **Scheduled for the next sprint** (with the portal cleanups from the M6.3b bare-metal pass): today the only way is a root shell on tty2, which is not a home-user path. Needs admin-only gating, a confirm step, and the reboot-pending interstitial the GPU-memory split already uses. [was M6, scheduled 2026-10]

---

## 12. Open questions / deferred decisions

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
13. **Per-key OWUI policy values (M6.3b)** — the manifest framework is locked; mode / cadence / value for each dotted key are decided per method as each config section is implemented (decision register in M6.3b).
14. **OWUI egress guardrails (M6.3b)** — durable config lets OWUI admins add external model connections (family-data egress). v0.11.0 fact: the writer is `POST /openai/config/update` (`/ollama/config/update` analogous), which rewrites the whole connection set; `POST /api/v1/configs/connections` only flips `direct.enable` + base-model cache. Product call: guard the writers vs grant power users (Lemonseed keeps its own entry either way).
15. **Removed-user OWUI history (M6.3b)** — day one: the delete action propagates an immediate OWUI delete (account + chats wiped; the reconcile loop never deletes on its own). Open: whether to adopt the tombstoned 7-day-undelete flow (§11 Backlog) and how it squares with jurisdictional right-to-erasure timelines.

---

## 13. Decision log (summary)

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
| GPU | Drivers in base; NVIDIA CUDA (M5) + AMD Vulkan (M6.4); single image covers both; no VRAM pooling |
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
| AMD GPU (M6.4) | **Vulkan backend** via Mesa RADV ICD (single driver for dGPU + Strix iGPU). `vulkaninfo` detection + `select_backend` runtime dispatch. Single production image covers NVIDIA (CUDA) + AMD (Vulkan). ROCm/HRX deferred to backlog (experimental, gfx1100/gfx1151 only). NPU (XDNA) out of scope. |
| llama.cpp release (M6.4) | **Build from source at image-build time, pinned to a tagged upstream release** (never HEAD). One build per backend → dedicated `llamacpp-cpu`/`-cuda`/`-vulkan` binaries lemond invokes via its per-backend `*_bin` config. CPU+Vulkan track the `nix-amd-ai` flake ref; **CUDA is our own source build** (nix-amd-ai ships no CUDA package) pinned to the latest stable tag, rebased by bumping tag+hash and re-validating. Platform patch sets (e.g. ROCm via `lemonade-sdk/llamacpp-rocm`) become dedicated backend targets when enabled. Backend choice is the control plane's (`select_backend`), not lemond's. |
| CP API namespacing (M6.5) | **CP model-picker API lives on `/_lemonseed/api/models`**, not `/api/models`. Open WebUI's frontend also calls `GET /api/models` for its OpenAI-style model list, and nginx's exact-match `= /api/models` location wins over the `/` prefix that routes to OWUI — so the CP's picker payload was silently returned to OWUI and its model dropdown rendered empty. The `/_lemonseed/` prefix is the established internal namespace (brand, lemond proxy, session-verify); moving the picker there frees `/api/models` for OWUI. |
| OWUI config authority (M6.3b) | **DB after first boot.** Open WebUI's Postgres `config` table is authoritative; env = bootstrap-only (`DATABASE_URL`, `WEBUI_SECRET_KEY`, auth/trusted headers, `OFFLINE_MODE`, `WEBUI_NAME`, `ENABLE_VERSION_UPDATE_CHECK` + a CP env-generation marker). The CP reconciles via OWUI's admin API from a versioned manifest (`control-plane/owui-policy.json`) declaring per-key mode (`initial_setup` / `managed`; collections add `selective_management` / `full_management`) + cadence + `reason` — behavior legible outside Python. Portal ownership badges + nginx guards enforce declared sections. |
| OWUI access (M6.3b) | **Own thin stdlib client** (`owui_client.py`; the PyPI `owui_client` lib is async — wrong fit for the threaded stdlib CP). Dedicated CP service account (`lemonseed-cp@localhost`) via trusted-header sign-in **direct to the slot ports**; endpoint-restricted `sk-` key; Managed models owned by **CP-defined id** (`lemonseed-ai*`) — v0.11.0 `ModelForm.id` is a client-supplied string stored verbatim, so the ids never churn (we write them; a wiped DB re-creates them under the same ids); display names in `Lemonseed *` are a reserved namespace, collision-swept at reconcile because path-only nginx guards can't block by body. Users collection managed as membership + is-admin only: the loop never deletes accounts; an explicit CP member-delete propagates an immediate OWUI delete (account + chats wiped — the delete-button semantics right-to-erasure regimes want). Service account display name **“Lemonseed Control Plane”**; OWUI cannot hide users from the admin list, so protection = unmistakable name + id-targeted guards + primary-admin immunity (the CP account must be OWUI's first user). Admin elevation rides trusted headers at sign-in; demotion is pushed via `POST /api/v1/users/{id}/update`. |

---

## 14. Review checklist

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
- [ ] M6.3b: OWUI durable config + CP reconciliation from manifest-declared admin-API writes (design locked — see M6.3b). **Landed:** persistence flip, policy-reconcile engine, portal ownership view, and **4B guard enforcement** (rendered nginx include, manual reconcile, admin reset window). **Pending:** LiteLLM friendly models; user/group management (M6.2b) owns the account guards.
- [ ] Post-M6: Cloud service / DynDNS (`proposals/CLOUD-SERVICE.md`) for remote access + Let's Encrypt
