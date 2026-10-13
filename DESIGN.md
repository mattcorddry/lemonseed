# Lemonseed Appliance — Design

Status: **living design, as built** — M1–M5 shipped; M6 landed through **M6.5** (Open WebUI durable config + control-plane reconciliation, AMD Vulkan support, model picker; 2026-10). Users & groups management and LiteLLM friendly model names moved to **M7**.

Related: [Concept.md](./Concept.md) (product vision), [Requirements.md](./Requirements.md), [AGENTS.md](./AGENTS.md) (how to change code and close a sprint).

**How to read this repo's docs**

| Document | Role |
|----------|------|
| `DESIGN.md` (this file) | How the product works **as built**, plus the consolidated backlog. The product/architecture contract. |
| `AGENTS.md` | Engineering process: conventions, test lifecycle, command map. |
| `proposals/M*/` | **Milestone design records** — the reasoning, alternatives and phase plans behind each milestone. Historical: where they disagree with this file, **this file wins** on current behavior. |
| `Concept.md` / `Requirements.md` | Product intent and historical requirements. Defer to this file when unsure. |

This document captures architectural decisions. Product intent lives in Concept.md; this file translates that intent into buildable decisions and records what the appliance actually does today.

---

## 0. Product framing (from Concept.md)

Lemonseed is a **local, multi-user home AI hub** — not a cloud client and not a DIY container toolkit. The OS appliance (read-only SquashFS, A/B updates, installer, control plane) exists so non-technical households can run open models privately on LAN hardware.

**Release 1 product surface (Concept):** chat (text; voice later), background workers/notifications, image gen/edit.  
**Release 2 (Concept):** short video, documents, speaker/voice appliance.

**Differentiator:** always-on LAN quality above phone/laptop on-device models; trusted sharing inside one household; open-source stack inspection; **no cloud AI dependency** for core chat.

**Stack intent (locked):**
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
- Cloud model burst / anonymized cloud GPU relay (Concept “future”)
- Delegated auth inside modules (per-container OAuth / OpenID) — authentication happens **only** at the nginx edge via the control plane's session gate (§3.3); Open WebUI's own login is disabled and its identity comes from trusted headers
- LUKS/encrypted user-data (deferred; see §11 backlog)
- Community plugins in the base image until each is license- and security-reviewed

---

## 2. Milestone history

Shipped milestones are summarized here; the reasoning and phase plans live in `proposals/`.

| Milestone | Deliverable | Design record |
|-----------|-------------|---------------|
| **M0 — Design** | This document | — |
| **M1 — Artifacts** | Flake + Makefile → `root.squashfs`, `vmlinuz`, `initrd.img`, `manifest.json`, optional `.sig` | — |
| **M2 — Bootable skeleton** | LVM PV → storage provisioner → btrfs `@etc`/`@var`; stub control plane; QEMU userdata disk | — |
| **M3 — Partition & A/B tooling** | `partitions.yaml` → `out/repart.d`; `boot/grub.cfg`; slot-aware root; `lemonseed-ab` + lock-in; in-place updater | — |
| **M4 — USB installer** | Install + portal + first-boot setup; preserve known user-data on reinstall (M4.1) | [`proposals/M4/USB-INSTALLER.md`](./proposals/M4/USB-INSTALLER.md) |
| **M5 — Hardware-aware local chat** | `lemond` + NVIDIA/CUDA; inventory + catalog planner + CP pull/load/pin; portal “Getting Ready” | [`proposals/M5/HARDWARE-AWARE-CHAT.md`](./proposals/M5/HARDWARE-AWARE-CHAT.md) |
| **M6.1 — Open WebUI as primary UX** | OWUI module on Postgres, Lemonade backend, Lemonseed shell + iframe | [`proposals/M6/OPEN-WEBUI-UX.md`](./proposals/M6/OPEN-WEBUI-UX.md) |
| **M6.2 — Edge auth** | CP-native login (signed session cookies), nginx `auth_request` gate, trusted-header identity into OWUI | same record, §M6.2 |
| **M6.3a — Policy seeding** | Open WebUI env seeds for household defaults (autocomplete off, workspace off, identity prompt, …) | same record |
| **M6.3b — Durable config + reconciliation** | OWUI `config` table authoritative; manifest-driven reconcile engine; nginx write guards; admin reset window | [`proposals/M6/OWUI-DURABLE-CONFIG-M63B.md`](./proposals/M6/OWUI-DURABLE-CONFIG-M63B.md) + `OWUI-POLICY-ENGINE.md`, `OWUI-PHASE4-PORTAL-GUARDS.md`, `OWUI-PHASE4B-ENFORCEMENT.md` |
| **M6.4 — AMD GPU support** | Vulkan backend (Mesa RADV) for dGPU + Strix iGPU; runtime backend dispatch. **Implemented; untested on AMD hardware** (none available) — unit-covered only, since the QEMU profile is CPU-only | [`proposals/M6/AMD-VULKAN-M64.md`](./proposals/M6/AMD-VULKAN-M64.md) |
| **M6.5 — Model picker** | Portal model picker (families + best-fit), model disk-space maintenance | [`proposals/M6/MODEL-PICKER-M65.md`](./proposals/M6/MODEL-PICKER-M65.md) |
| **M6.6 — Session & error contract** | Sliding sessions in Valkey + Postgres generation counter, one XHR status contract (`portalFetch`: 401 → `/login` in the top frame, 403 surfaced, 5xx capped backoff), `/api/status` public subset + gated `/api/system`, branded gateway error pages + self-reload, one shared stylesheet root, container-image pins | [`proposals/M6/SESSION-AND-ERROR-CONTRACT-M66.md`](./proposals/M6/SESSION-AND-ERROR-CONTRACT-M66.md) |

M6.1–M6.5 are all landed. **M6.4 is untested on AMD hardware** — the backend dispatch is unit-covered and NVIDIA works on real hardware, but the QEMU profile pins `llamacppBackend = "cpu"`, so no automated test ever loads a model through Vulkan. It ships as *implemented, AMD hardware unvalidated*; see [`proposals/M6/AMD-VULKAN-M64.md`](./proposals/M6/AMD-VULKAN-M64.md).

M6 close-out also carries **P0** (lint gate + test determinism; `make lint` is now the gate, and the suspected suite flake was disproven) and **P7** (build cycle time; [`proposals/M6/BUILD-CYCLE-TIME-P7.md`](./proposals/M6/BUILD-CYCLE-TIME-P7.md)).

**Moved out of M6 into M7:** **users & groups management** (formerly M6.2b — which also owns flipping the two account guards from `advertise` to `enforce`), **LiteLLM friendly model names**, and **portal power controls** (reboot / shutdown / update-check / logout / the merged SYSTEM panel). Sprint pulls are listed in [`proposals/M7/PLAN.md`](./proposals/M7/PLAN.md), which holds M7 feature work only.

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
│ grubenv      │  │             │  │             │  │   (+ docker, models,     │
│              │  │             │  │             │  │     Postgres, Valkey)    │
└──────┬───────┘  └──────▲──────┘  └──────▲──────┘  └──────────────────────────┘
       │                 │                │
       │    GRUB picks A or B (default / once)
       └─────────────────┴────────────────┘
                         │
                         ▼
              read-only SquashFS root
              systemd → nginx :80 (only LAN entry)
                          │
                          ├── control plane (127.0.0.1:8080) — portal + JSON API + session gate
                          ├── lemond (127.0.0.1:13305) ── llama.cpp backends (Nix-wired)
                          └── Docker network `lemon`: Open WebUI ⇄ PostgreSQL/Valkey containers
```

### 3.1 Configuration authority

The **control plane + PostgreSQL is the source of truth** for appliance intent — user and group accounts, module registry, model pins, credentials, CP state (`control-plane/db.py`). Configuration files, environment variables and container arguments are **derived artifacts**, regenerated by the CP on change and reconciled at boot. Nothing downstream of the CP is hand-edited as truth.

Derived artifacts in scope: the OWUI container env file, the nginx session gate / admin gate / OWUI upstream / OWUI guard snippets under `/run/lemonseed/`, data-service credentials, and `lemond`'s config policy. The rule that keeps this honest: **behavior that an admin should be able to reason about is declared in a manifest or stored in Postgres, not buried in Python.**

### 3.2 HTTP surfaces

nginx on **:80** is the only LAN-facing listener. Everything else binds loopback or the Docker network `lemon`.

**Routing (nginx, `nixos/modules/gateway.nix`)**

| Path | Upstream | Gate |
|------|----------|------|
| `= /` | CP shell for documents; OWUI for iframes (`$lemonseed_root_upstream` map on `Sec-Fetch-Dest` / `?embed=1`) | session |
| `= /index.html` | CP | session |
| `/settings/` | CP portal settings | session + admin |
| `= /settings` | → `302 /settings/` | — |
| `= /login` | CP login page | **open** (it *is* the sign-in form) |
| `= /logout` | CP | session |
| `= /setup` | CP first-boot setup | **open** (pre-admin trust window) |
| `= /healthz`, `= /health` | CP | **open** |
| `= /_lemonseed/session-verify` | CP | nginx `internal` (auth_request target only) |
| `= /api/boot` | CP | **open** (installer handoff) |
| `= /api/status` | CP | **open** — public subset only: `status`, `role`, `version`, `setup_complete`, `setup.mdns_name`, `portal_url` |
| `= /api/system` | CP full snapshot (telemetry, chat, modules, data, auth, session store) | session |
| `= /api/hardware`, `/api/plan`, `/api/data`, `/api/modules`, `/api/chat` | CP | session |
| `= /_lemonseed/api/models` | CP model picker | session + admin |
| `= /_lemonseed/api/owui/{config,reconcile,reset-config}` | CP OWUI surfaces | session + admin |
| `= /_lemonseed/api/gc-test/*` | CP disk-GC test hooks | session + admin (QEMU e2e only) |
| `/_lemonseed/brand/`, `= /favicon.ico` | static brand assets (7 d cache) | open |
| `= /_lemonseed/lemonseed.css` | the one stylesheet root, `try_files` out of the image | open, revalidated |
| `/_lemonseed/lemond/` | `127.0.0.1:13305` | `allow 127.0.0.0/8; allow 172.16.0.0/12; deny all` — for modules on `lemon`, never LAN |
| `= /chat`, `= /chat/` | → `302 /?embed=1` | — |
| `/ws/` | Open WebUI (Socket.IO) | session, **no OWUI guards** |
| `/` (catch-all) | Open WebUI: SPA assets, `/api/v1`, `/openai`, `/ollama`, `/c/…` | session + **OWUI guards** |

Failure states are carried by two named locations. `@auth_unauthorized` (navigations get `302 /login?rd=$request_uri`, unencoded — see §3.3; anything labelled `X-Requested-With: fetch` gets `401` + JSON instead, because `fetch()` follows a redirect and would hand the portal login HTML at HTTP 200) and `@owui_loading`, which is genuinely branded because OWUI and the CP are independent processes, so a 502 from `/` while OWUI starts is normal. Every other upstream failure is answered by a **static** page: each gated CP location ends with `error_page 500 502 503 504 =503 /_lemonseed/cp-down`, an `internal` location that `try_files` `cp-down.html` out of the CP's store-sourced static directory, so it renders even when the CP is the thing that is down. `location = /` chooses its target from the map `$lemonseed_error_target` (document → the static page; `?embed=1` or an iframe `Sec-Fetch-Dest` → `/_lemonseed/interstitial`, which proxies the CP shell so an embedded frame keeps its path). The page polls `/api/status` and reloads `_top` when the CP answers. Two nginx rules shaped this and both are asserted against a live nginx by `make test-gateway`: `alias` is rejected inside a named location, and **`error_page` does not run inside a location reached through `error_page`** — which is why the old `error_page 502 → @cp_interstitial → proxy_pass :8080` chain could never render during the startup window it existed for. `proxy_intercept_errors` is off, so a 5xx the CP itself produced passes through untouched: a real bug stays visible. **500 belongs in those lists** for a reason that is easy to delete by mistake — an `auth_request` subrequest that *answers* with anything other than 401/403 fails the main request as **500** (Valkey down ⇒ verify 503 ⇒ main 500), while one that **cannot connect at all** surfaces as **502** (both measured on bare metal during the startup window).

`/_lemonseed/` is the reserved internal namespace for CP surfaces; the model picker lives there rather than on `/api/models` because Open WebUI's own frontend needs that path (see decision log).

**Control-plane HTTP surface (`control-plane/stub.py`)** — a single stdlib `socketserver`-based process on `127.0.0.1:8080`, no WSGI framework. GET: `/healthz`, `/api/boot`, `/api/status`, `/api/system`, `/api/hardware`, `/api/plan`, `/api/chat`, `/api/data`, `/api/modules`, `/settings/`, `/settings/content/`, `/login`, `/_lemonseed/session-verify`, `/_lemonseed/api/models`, `/_lemonseed/api/owui/config`, the brand/favicon files. POST: `/login`, `/logout`, `/setup`, `/_lemonseed/api/models`, `/_lemonseed/api/owui/reconcile`, `/_lemonseed/api/owui/reset-config`.

Portal settings render as **two URLs**: `/settings/` is the shell (nav + iframe) and `/settings/content/` is the inner document. Panels, in order: **Lemonade AI Engine** (chat status + active model), **Large Language Model** (the picker), **Status** (version / setup / timezone / GPU), **Chat** (a *Status* subsection — Open WebUI version + container state, and the Lemonseed configuration row — then a *Controls* subsection with *Apply Lemonseed settings now* / *Reset chat settings* / *Refresh status*), **API** (collapsed "System API Data"; the intended home for the OpenAI-compatible endpoint toggle in §11). The rule for anything added here: **show only what a home admin can act on** — developer-facing detail belongs in build artifacts (§11).

### 3.3 Identity, sessions and access control

**As built:**

- **One login path:** the nginx `auth_request` gate (`auth.GATE_SNIPPET`, CP-written to `/run/lemonseed/auth-gate.conf` after the first admin exists). Session cookie `lemonseed_session` (`Path=/; HttpOnly; SameSite=Lax`, deliberately **no** `Secure` — the appliance is HTTP-only on the LAN and self-signed TLS was worse UX than none) carries an HMAC-SHA256 token that also embeds a session id (`sid`) and the account's session generation (`gen`). The token alone is not authority.
- **Session state lives in Valkey** (`control-plane/sessions.py`), keyed by boot id and `sid`. **Sliding** idle window `SESSION_INACTIVITY_SECONDS = 3600`, refreshed on every verify, clipped by the absolute ceiling `SESSION_ABSOLUTE_SECONDS = 36000`. Keying by `/proc/sys/kernel/random/boot_id` means a reboot drops all sessions — `valkey-server --save 60 1` would otherwise resurrect them from an RDB snapshot. Retaining sessions across a reboot would be nicer UX but is not a requirement, and persistence would cost more than it buys.
- **Revocation works.** `users.token_generation` (schema v4) is embedded in the token and compared on every verify, so disable, demote, delete and logout-all land on the affected account's next request; `auth.invalidate_sessions()` is the single bump point. Household scale makes the extra Postgres read cheap — no cache layer.
- **Session verify** (`GET /_lemonseed/session-verify`) needs three things to agree: the signature, a live Valkey record, and the generation in Postgres. It answers `200` plus `X-Forwarded-{User,Email,Groups,Name,Role}`, which nginx reads with `auth_request_set`. Those headers are only ever set by nginx from the verify response, so client-supplied identity is always dropped. Because the CP now depends on a container it also starts (Valkey, `nixos/modules/data-services.nix`), the gate **fails closed**: store down ⇒ `503` JSON ⇒ the branded not-ready page, never an authenticated surface. A down data plane is a broken appliance; an unauthenticated one is not acceptable.
- **Accounts** live in Postgres (`users(username, display_name, email, password_hash, disabled, created_at)`, `groups`, `user_groups` many:many with cascade). Roles are **derived** from group membership (`auth.role()`, admin iff member of `admin`); there is no stored role column. Bootstrap is a single `/setup` path, open only while the `admin` group is empty. `users.disabled` *is* honored when authenticating (`auth.get_user` rejects it, and group joins add `AND u.disabled = FALSE`) — only the write path is missing (M7).
- **Admin gating** for `/settings/` and privileged APIs is enforced **in the CP** from `X-Forwarded-Groups`, not in nginx (`auth.ADMIN_SNIPPET` is intentionally empty: nginx `if` runs in the rewrite phase, before `auth_request` sets its variables).

**M6.6 — session and error contract** (the reasoning and the build deviations from the first draft: [`proposals/M6/SESSION-AND-ERROR-CONTRACT-M66.md`](./proposals/M6/SESSION-AND-ERROR-CONTRACT-M66.md) and [`proposals/M6/GATEWAY-ERROR-AND-PULL-CONTRACT-M66.md`](./proposals/M6/GATEWAY-ERROR-AND-PULL-CONTRACT-M66.md)):

- **One XHR status contract** for every portal request. `stub.PORTAL_FETCH_JS` defines a single `portalFetch()` that every control-plane call in the shell and the settings iframe goes through, and it labels each request `X-Requested-With: fetch` — which is how the gateway knows to answer an XHR with a real **401** + JSON instead of a **302** that `fetch()` follows into login HTML at HTTP 200 (the reason `if (!res.ok)` could never fire). The contract: **401** → `window.top.location = '/login?rd=…'`; **403** → render `detail`, never redirect ("not you" and "not yours" are different failures); **5xx** → capped backoff (three tries), then surface. Only **GET** is retried — a reset or reconcile whose response was lost must not run twice. Three calls stay outside it, each for a stated reason: the setup page's `/api/boot` poll and the login form's own `POST /login` (both pre-auth; a 401 on login through `portalFetch` would reload the login page forever), and the `/_app/version.json` reachability probe (retrying and auth-navigating would hide the failure it exists to detect). `control-plane/test_portal_fetch.py` **executes** the shipped JS in a V8 context with `fetch`/`window` stubbed rather than asserting on its text.
- **The status surface is split.** `/api/status` answers pre-login and now returns only what the installer handoff and the gateway's not-ready page need (`status`, `role`, `version`, `setup_complete`, `setup.mdns_name`, `portal_url`). Everything else — account counts, telemetry, chat, module and data-plane state, the session-store dot — moved to **`/api/system`**, which is session-gated. Household facts such as how many admins exist were readable by any device on the LAN before this. `status_public()` lists keys rather than subtracting them from the full snapshot, so a field added to the full snapshot cannot leak by default. Spawning the background workers is no longer an unauthenticated side effect either; they still start unconditionally at CP startup.
- **Frame-aware login:** the gate can land `/login` inside the shell's iframe, so the page breaks out with `window.top` before rendering. Sign-in answers XHR with `200 {"target": …}` (a fetch cannot read a 302's `Location`) and everything else with the 302. `rd` is taken as everything after `rd=` **unparsed**, because nginx composes `?rd=$request_uri` unencoded and a target legitimately contains `?`/`&`; `auth.safe_redirect_target()` then decodes **before** checking and rejects `//`, `/\` and off-origin forms. Logout tears the OWUI frame down before navigating, so a signed-in chat does not linger after sign-out.
- **One stylesheet root**, `/_lemonseed/lemonseed.css`, served by nginx `try_files` out of the read-only image (not proxied, not gated). Setup, login, the shell, the settings panel and the gateway's not-ready page all link it; no page inlines CSS any more. The not-ready page's rules are scoped under `body.cp-down` in that file, because unscoped its bare `p`/`button` rules would restyle the portal. Nginx error pages and CP pages now cannot disagree, which is also the precondition for user themes (§11).

### 3.4 Data services and module lifecycle

The CP is the source of truth for **which containers should exist and be serving**. Two supervisors tick on a ~10 s loop: `data_services.ensure_data_worker` (PostgreSQL, Valkey) and `modules.ensure_module_worker` (Open WebUI). Each reconciles intent with reality: missing container → `docker pull` (registry-reachability preflight, DNS + TCP 443, cached and throttled) → `docker run` with exact pinned arguments; stopped → start; running but not serving after a grace window → recreate. Docker `--restart` policies are **not** relied on for recovery.

The systemd oneshot units (`lemonseed-postgres`, `lemonseed-valkey`) are a boot accelerator only and **never pull**: they start whatever is already on disk (pinned digest first, tag as fallback), and when nothing is local they hand off to the supervisor and exit — a first boot with no images must reach the interstitial rather than stall on a WAN transfer. Because `--restart unless-stopped` usually has the containers up before the unit runs, each unit logs the image reference it is using on *every* path; "which image is actually running" is the first question after a pin bump. Container state and last error surface on `/api/data` and `/api/modules` so the portal can explain *why* chat is not ready.

**Module updates are blue/green:** start the new container → health-check → switch the reloadable nginx upstream file (`/run/lemonseed/owui-upstream.conf`) → retire the old. A failed cutover keeps the old slot serving.

**Container policy:** module containers join the user-defined Docker network **`lemon`** and reach data services by Docker DNS name (`lemonseed-postgres`). Host-facing publishes are `-p 127.0.0.1:…` only. Images are pinned to an explicit upstream stable tag in-tree (`OWUI_IMAGE_DEFAULT = ghcr.io/open-webui/open-webui:v0.11.0`); never `:latest`/`:main`. `LEMONSEED_OWUI_IMAGE` is a lab override only.

**Minimize bind-mounted application state.** Chat history, OWUI config, embeddings metadata, the CP module registry and user records belong in managed databases on user-data — not opaque host directory trees per container — so blue/green can run old and new against the same data.

### 3.5 Open WebUI integration contract

This is the most delicate boundary in the product: Open WebUI is simultaneously *the* household UX and a third-party app with its own admin UI, database and release cadence. The full design record is `proposals/M6/OWUI-DURABLE-CONFIG-M63B.md`; what follows is the contract as it runs today.

**Authority: the database, after first boot.** OWUI's Postgres `config` table is authoritative. Environment variables are **bootstrap-only** — only keys OWUI reads at import time, before it can query config:

| Env key | Why it stays env |
|---------|------------------|
| `DATABASE_URL` | Needed before a DB connection exists |
| `WEBUI_SECRET_KEY` | Signs session JWTs and API keys |
| `WEBUI_AUTH`, `WEBUI_AUTH_TRUSTED_{EMAIL,NAME,GROUPS,ROLE}_HEADER` | Identity plumbing wired at middleware import time |
| `OFFLINE_MODE` | Suppresses import-time network calls |
| `WEBUI_NAME`, `ENABLE_VERSION_UPDATE_CHECK` | Read at import; absent from the DB `DEFAULT_CONFIG`, so no manifest entry can back them |

Everything else is manifest-managed DB config. Env drift on DB-managed keys must **never** force a container recreate; the CP detects a stale container by exact-matching the bootstrap keys plus its own generation marker (`LEMONSEED_OWUI_ENV_GEN`).

**The manifest is the interface.** `control-plane/owui-policy.json` declares the whole CP↔OWUI contract in data, with six top-level blocks: `version`, `owui` (minimum validated version), `settings`, `entities`, `guards`, `api`. Today: **24 setting entries**, two entity collections, **7 guards**, a **23-entry** API name→route map. `owui_policy.py` validates it strictly at load (`ManifestError` on anything unknown) so the manifest cannot quietly rot.

**Ownership modes** (how much we own):

| Kind | Mode | Semantics |
|------|------|-----------|
| Scalar | `initial_setup` | Delivered as env at first boot / backfilled once if the row is missing; user GUI edits then stick |
| Scalar | `managed` | We own the value; re-asserted on cadence; GUI edits get reverted |
| Collection | `selective_management` | We own *our* entities by ownership key (models: CP-defined id); everything else untouched; entities claiming our reserved name namespace are swept, with a portal log entry |
| Collection | `full_management` | We own the intent **within the collection's declared scope**: create missing, fix variation, delete extras |

**Cadence** bounds re-assertion so the reconciler never stomps user intent on a timer: `at_boot` (every time an OWUI slot goes healthy — security/egress/identity/wiring), `on_upgrade` (manifest version bump), `manual` (explicit portal action). Every entry carries a human-readable `reason`, which is what makes the portal's ownership view and a guard's denial message meaningful.

**Entities as built:**
- `models` — `selective_management`, owned by **CP-defined id** (`lemonseed-ai`, `lemonseed-ai-filtered`), reserved display-name namespace `^Lemonseed.*$` (case-insensitive). v0.11.0 `ModelForm.id` is a client-supplied string stored verbatim, so *we* write the ids and they cannot churn under an upgrade; a wiped DB makes our models *missing*, and the reconciler re-creates them under the same ids. A UI-created model that claims a reserved name is removed by the reconcile namespace sweep — path-only nginx guards cannot see request bodies. Definitions land with the LiteLLM phase.
- `users` — `full_management` with scope **`["membership", "is_admin"]`**: the loop creates, promotes and demotes but **never deletes** accounts. An explicit member-delete in Lemonseed propagates an immediate OWUI delete (account + chats wiped — the delete-button semantics right-to-erasure regimes want). `%SERVICE_ACCOUNT_ID%` is in `never_touch`.

**Guards** (`advertise` = show as Lemonseed-owned in the portal; `enforce` = refuse the request at nginx). The CP renders `/run/lemonseed/owui-guards.conf` from the manifest (atomic; seeded empty, so a boot where the CP never ran blocks nothing while the reconcile loop still enforces intent). Only the OWUI catch-all location carries guards, so "which requests are guarded?" is one line to read. Enforced today:

| Guard | Why |
|-------|-----|
| `POST /api/v1/configs/connections` | writes `direct.enable` (direct model endpoints), which stays off |
| `POST /openai/config/update` | OWUI's connection editor rewrites the **whole** OpenAI connection set, including our Lemonade entry |
| `POST /ollama/config/update` | there is no Ollama on the appliance |
| `POST /api/v1/users/%CP_OWUI_ID%/update`, `DELETE /api/v1/users/%CP_OWUI_ID%` | the service account is not editable or removable from OWUI |

Advisory only (flip with the M7 users & groups work, which supplies the safe replacement): `POST /api/v1/auths/add`, `DELETE /api/v1/users/.*`.

Three non-obvious invariants, all hard-won:
1. A guard's 403 body **must be JSON with a non-empty `detail`** (and the location sets `default_type application/json`). With `text/plain`, OWUI discards the reason and shows a useless “Server connection failed”.
2. `if` + `return` runs in nginx's **rewrite** phase, before the `auth_request` in the gate, so an expired admin session on a guarded path gets “managed by Lemonseed” instead of a sign-in prompt. Accepted for now (T4-A); the anonymous-caller fallback is a backlog item.
3. **Never guard `POST /api/v1/configs/import`** — that is the writer the reconciler itself uses.

**The service account.** The CP signs in to OWUI as a dedicated built-in super admin: username `lemonseed-cp`, display name **“Lemonseed Control Plane”**, email `lemonseed-cp@localhost`, over trusted headers **direct to the slot ports** (`127.0.0.1:18080/18081`), so the portal's own guards never block the reconciler. It also holds an endpoint-restricted `sk-` key whose allowed endpoints are *derived from this manifest's* api map (`derive_allowed_endpoints`), so the key's blast radius equals the contract. OWUI cannot hide a user from its admin list, so protection is an unmistakable name + id-targeted guards + OWUI's primary-admin immunity (the CP account must be OWUI's first user).

**Every portal identity** uses the synthetic domain `auth.OWUI_EMAIL_DOMAIN = "localhost"` — the only no-dot domain OWUI's `validate_email_format` exempts, and the value is read back from the stored row, never re-derived (changing it would duplicate accounts, and the superseded row is OWUI's earliest user). Nothing on the appliance sends mail.

**Reset window (admin-only).** `POST /_lemonseed/api/owui/reset-config` verifies the confirm string, removes the ready flag (so document requests get the interstitial instead of 502s), stops **both** blue/green slots (they share one database, so blue/green buys nothing here), `TRUNCATE`s OWUI's `config` table, drops the CP's API key and remembered identity, starts one slot, re-establishes the service identity, and runs a full reconcile **before** cutting traffic back — plus a last-known-good snapshot used to recover a *broken* reset (a slot is never cut over unreconciled). It exists because a settings reset cannot be done by deleting rows under a running OWUI without errors reaching logged-in users. It touches OWUI's **config** only: user preferences and model settings are preserved; admin-level settings are cleared.

**Reconcile triggers.** Boot (each OWUI slot that goes healthy), the slow audit tick (`POLICY_AUDIT_INTERVAL_SEC`, default 300 s — observe + drift report only, it does not write), and an explicit admin action. A slot is never cut over to if it cannot be reconciled — a user must not meet an unreconciled OWUI. Reconcile failures never perturb the module state machine; they surface through the `owui_policy` snapshot field that gates the iframe.

### 3.6 Model management

- **Catalog** (`control-plane/catalog/models.json`, `catalog_version` 8): 4 families (Gemma, Nemotron, Qwen, LFM2) / 12 models, each with a `checkpoint`, size and capability metadata. The catalog is curated; the planner picks the **best fit** for the inventory.
- **Planning** (`hardware.py`): collect a hardware report (GPU/vendor/VRAM, RAM, disk), then `best_fit_model` selects the model that fits the constraints. Selection is recorded via `POST /_lemonseed/api/models` (admin-only) and persists.
- **Bring-up** (`chat_ready.py`): a daemon worker pulls → loads → pins the selected model through `lemond`'s localhost API, verifies with a short completion, and publishes `chat.state` (`/api/chat`, folded into `/api/status`). The portal gates the OWUI iframe on **both** module `http_ready` and `chat.state=ready`. If lemond reports the weights already on disk (`GET /v1/models/{id}` → `downloaded`), the CP skips the pull and says "…is already on disk — loading it…". `auto_check_model_updates` is pinned **false** so a reboot can never turn into a surprise multi-GB re-download.
- **Disk-space maintenance** (`model_store.py`): 90 % high-water mark, 10 % reserve, LRU eviction of cached models via lemond's API while the pinned model survives, keep-1 fallback, refuse-on-no-room. Comparisons are casefolded (`catalog.dir_key`) because the cache directory name is derived from the catalog `checkpoint`.
- **Hardware tiers** (direction for agentic features): small boxes (< 64 GiB VRAM) run **one** catalog model with good defaults (automations, channels, notes; no heavy sub-agent stack); large boxes run a **fast** daily-chat model plus a **dense** model for deeper research and heavy agent work. All models get system prompts that establish the “Lemonseed” identity and per-user household context (`models.default_params` / `models.default_metadata` in the manifest).
- **Model weights** live on user-data under `/var/lib/lemonseed/models/` and survive image updates.

**llama.cpp provenance.** Built from source at image-build time, **pinned to a tagged upstream release** (never HEAD) — `nixos/modules/lemond.nix` overrides `pkgs.llama-cpp` with its own `fetchFromGitHub` tag and `npmDepsHash`. One build per backend (`llamacpp-cpu` / `-cuda` / `-vulkan`) so lemond can be pointed at the right binary; backend choice is the CP's (`select_backend` from the runtime inventory), not lemond's. CUDA is our own source build; CPU/Vulkan track the `nix-amd-ai` ref. Platform patch sets (e.g. ROCm) become dedicated backend targets when enabled.

### 3.7 Local console

tty4 shows the status splash (IP / URL / boot hints; `docker*` interfaces excluded). tty2 is a root autologin shell for field debug (**empty root password during bring-up only** — see §9). There is no console menu yet; power and update actions today require that shell (§11 backlog).

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
- **In-place updates:** `scripts/lemonseed_update.py` (`status` / `apply` / `cancel`, `--url` / `--dir`, `--reboot`, `--force`) writes the verified SquashFS + ESP kernels into the **inactive** slot, sets GRUB `boot_once`, and leaves `@etc`/`@var` untouched. It refuses to write while a boot attempt is armed. On the next reboot GRUB tries the new slot once; `lemonseed-ab-lock-in.service` promotes `boot_default` only after platform services (network, nginx, control plane, Avahi, Docker) are healthy. A failed boot falls back to the previous `boot_default`.
- Root partitions are **raw SquashFS images**, bootable without unpacking to ext4.
- **Installer** creates the GPT layout and LVM PV; it does **not** format btrfs.
- **First-boot provisioner** (`appliance-storage-provision.service`, shell oneshot every boot): if the LV is missing, create VG `appliance` / LV `user-data`, `mkfs.btrfs`, subvols `@etc` / `@var`. If the LV exists, no-op aside from ensuring subvols.
- Later: additional disks → extra PVs → `vgextend` / `lvextend` / btrfs resize (control plane).
- Exact sizes and type UUIDs live in **`partitions.yaml`** (§5), not in Makefiles.

### Bootloader
- **GRUB** on EFI (`grubx64.efi` on ESP), config from `boot/grub.cfg`.
- A/B selection via the **GRUB environment block** (`grub-editenv`), reusing stock GRUB mechanisms.

### A/B boot protocol

State lives in `grubenv`, manipulated by `scripts/lemonseed-ab.sh`:

| Variable | Meaning |
|----------|---------|
| `boot_default` | Permanent slot: `a` or `b` |
| `boot_once` | One-shot slot for the next boot |
| `boot_attempted` | Slot being tried (written by GRUB when it consumes `boot_once`) |

Flow:
1. Updater writes new SquashFS + matching `vmlinuz` into the inactive slot / ESP.
2. Updater sets `boot_once=<new>` (and clears prior attempt state as needed).
3. GRUB: if `boot_once` is set → boot that slot once, set `boot_attempted`, clear `boot_once`.
4. On **successful startup**, `lemonseed-ab-lock-in.service` sets `boot_default` to the running slot and clears `boot_attempted`.
5. On **failure** (crash/hang before lock-in), power cycle → GRUB uses the last `boot_default`. Rollback is passive; there is no watchdog timer.

Lock-in criteria are OS + control plane (+ Docker); **not** gated on model load. **Open issue** (§12): because only GRUB writes `boot_attempted`, a *planned* reboot taken while `boot_once` is armed silently **upgrades** to the new slot — the power-control work must resolve this.

---

## 5. Partition configuration format

Source of truth in-repo is the human-editable **`partitions.yaml`** (roles, sizes, FS, labels, mount policy). The apply engine is **`systemd-repart`**: `scripts/gen-repart.py` translates the YAML into drop-ins under `out/repart.d` (`make repart-generate`), and `scripts/disk-repart.sh` applies them (`make disk-repart DISK=/dev/sdX`, `DRY_RUN=1` supported). `scripts/repart-smoke-test.sh` / `make repart-test` validate the generated layout on a loop device.

Why repart over the alternatives:

| Tool | Format | Strengths | Fit for us |
|------|--------|-----------|------------|
| **systemd-repart** | INI `repart.d/*.conf` | GPT-native; grows partitions to fill arbitrary disk sizes; `CopyBlocks=` for raw image install; btrfs `Subvolumes=`; already on a systemd appliance | **Chosen** — runtime + install engine |
| **disko** | Nix | Excellent for NixOS installs; reproducible | Strong if the installer is Nix-based; less ideal as a portable text contract for non-Nix helpers |
| **genimage** | libconfuse `.cfg` | Great for **building** fixed disk images offline | Weak for installing onto **arbitrary disk sizes** in the field |
| **Yocto WIC (`.wks`)** | Kickstart-like | Familiar A/B patterns in embedded | Yocto-centric; pulls us off Nix |
| **sfdisk** | dump script | Simple, scriptable | Low-level; no FS/subvol/copy semantics |

Mount flags principle: **`noexec` / `nodev` / `nosuid` wherever practical**; `/var` flags validated empirically under Docker + Lemonade.

### Storage ownership
| Layer | Responsibility |
|-------|----------------|
| Installer (M4) | GPT + LVM PV for user-data; preserve known user-data on reinstall |
| `appliance-storage-provision` | Every boot; create VG/LV/btrfs/subvols if missing |
| Control plane | Multi-disk expansion; Docker/module orchestration |

---

## 6. Root filesystem model

### Read-only SquashFS
- NixOS build produces a SquashFS root, installed onto the A/B partitions with **`dd` / block copy** so the kernel can use the partition as root.
- **No overlayfs / tmpfs overlay** on `/`.

### Mutable paths
- btrfs subvolumes on the **user-data LV** mounted over paths that need RW (`subvol=@etc`, `subvol=@var`). Initial set: **`/etc`**, **`/var`** only.
- **`/tmp`:** size-capped **tmpfs** (`boot.tmp.useTmpfs`, 128M) — not a btrfs subvol and not a symlink to `/var/tmp`. Needed early (before user-data) and must not compete with GPU/model RAM. Persistent scratch belongs on `/var`.
- Docker volume data lives under user-data (typically `/var/lib/docker` once `/var` is mutable); models under `/var/lib/lemonseed/`; Postgres/Valkey data under `/var/lib/lemonseed/data/` (preserve-on-reinstall).
- Device discovery for the PV: `/dev/disk/by-partlabel/lemonseed-user-data`, else kernel cmdline `appliance.storage=` (QEMU).
- **`/etc` population:** an empty `@etc` is fine; NixOS `setup-etc.pl` activation recreates managed `/etc` from the store each boot. We do **not** copy SquashFS `/etc` into `@etc`.
- **Metadata convention:** `/run/appliance/version` (image version from the booted closure, tmpfiles symlink) and `/etc/appliance-*` mutable markers on `@etc` (e.g. `/etc/appliance-first-boot`, `/etc/appliance-setup.json`, `/etc/appliance-setup-complete`).
- **`/var`:** an empty `@var` is fine; systemd-tmpfiles and services create what they need; state persists across image updates.

### Initrd
- Soft goal: **no initrd**. We still ship one in practice (copied to `initrd.img` by the artifacts build) until a no-initrd spike lands; drivers needed for storage + btrfs come from the kernel config.
- If kept, it stays **minimal** (storage + filesystem modules, mount logic) and is versioned/signed like other boot artifacts.

---

## 7. Image contents

### In the SquashFS base
- systemd
- Docker — for **modules**, not for `lemond`
- **Headless Lemonade (`lemond` + CLI)** built from source; no Lemonade web/desktop UI
- GPU driver stacks in-base for discovery at boot (**NVIDIA open kernel modules**; AMD via Mesa RADV/Vulkan)
- Inference backends as Nix packages (`llamacpp-cpu` / `-cuda` / `-vulkan`); selected at runtime (§3.6)
- **Control plane** (systemd-supervised) + nginx gateway
- Tools needed for updates: mount ESP on demand, `grub-editenv`, verify signatures, write the inactive slot

### Not in the SquashFS base
- Open WebUI, ComfyUI, PostgreSQL, Valkey and other **module/data** images — pulled at runtime by the CP
- Model weights and Lemonade mutable state — `/var/lib/lemonseed/`
- PostgreSQL/Valkey data dirs — `/var/lib/lemonseed/data/`
- User data

### Networking
- All Lemonseed containers join the user-defined Docker bridge network **`lemon`** (`lemonseed-docker-network.service`, created before any container). Inter-container access uses Docker DNS names (`lemonseed-postgres`, `lemonseed-valkey`); host-facing publishes are **`-p 127.0.0.1:…` only** — a startup check rejects any port spec without `"HostIp":"127.0.0.1"`.
- `lemond`, PostgreSQL, Valkey and the CP reach the host on **loopback only**; nginx :80 is the only LAN entry. `host.docker.internal` is reserved for a container reaching **host** processes (e.g. `lemond`).

### Control plane
- **Language:** Python 3, stdlib-first, served by `python3.withPackages` (psycopg2, bcrypt, requests, lemonade Python SDK) — packaging in `nixos/modules/control-plane.nix`. Revisit Go only if footprint hurts.
- One process today: privileged API + portal UI, bound to **127.0.0.1:8080**. Splitting an unprivileged UI from the privileged daemon remains an option, not a need.
- Background work uses non-blocking locks + daemon threads (`chat_ready`, `data_services`, `modules`, the OWUI reconciler) so no HTTP handler blocks on provisioning.

### HTTP gateway
- **Public entry is always nginx on :80**; the CP stays loopback-only. Full routing table in §3.2.
- **First boot / unset appliance:** nginx is a straight reverse proxy to the CP; the CP serves a **no-login** readiness + setup wizard (`/`, `/setup`, `/api/boot`). Completing setup writes `/etc/appliance-setup-complete` (+ JSON state).
- **Post-setup:** document `/` → Lemonseed shell; iframe (`/` with `Sec-Fetch-Dest: iframe` or `?embed=1`) → Open WebUI at path `/` (root SPA — it must not be mounted under `/chat/` or the client router 404s); catch-all → Open WebUI; reserved paths → CP. A **reloadable upstream file** enables blue/green. Modules stay **auth-naive**: authentication terminates at the gate.
- **mDNS:** Avahi publishes the slug of the appliance **Name** from setup (`Matt Lemonseed 1` → `matt-lemonseed-1.local`). Before setup the name defaults to `Lemonseed`, so a fresh appliance is reachable at `lemonseed.local` for the installer hand-off. After a preserve reinstall of a **renamed** appliance, `lemonseed-avahi-alias` briefly publishes `lemonseed.local` as a CNAME so the installer can discover the box; the installer then redirects to `<slug>.local` and the alias is released (`/run/lemonseed/mdns-handoff-done`). Fully confirmed appliances never claim `lemonseed.local`, so multiple units can share a LAN.
- **Appliance identity (one name, not two):** setup collects a single user-facing **Name**; its slug is the system hostname, the mDNS name, and the hostname dhcpcd advertises. `networking.hostName` is therefore **empty** in the NixOS config — a non-empty value makes `/etc/hostname` a read-only store symlink and pins `host-name=` in `avahi-daemon.conf`, both of which would defeat the chosen name. `lemonseed-identity.service` seeds `/etc/hostname` on `@etc` before dhcpcd/Avahi start.
- **Name collisions:** setup resolves `<slug>.local` over mDNS before offering a default; if another host owns it, the suggested name becomes `Lemonseed 2` (editable), and submitting a name owned by a different host is rejected. Avahi's effective hostname (queried over D-Bus, including any collision suffix) identifies our own announcement; IP comparison is only a fallback because DHCP leases change.

---

## 8. Build system

### Nix
- **Flakes** + pinned `nixpkgs` for reproducibility; NixOS modules under `nixos/modules/`, wired from `nixos/configuration.nix` and profile overlays (`nixos/modules/qemu-test.nix`).
- No absolute paths in Makefiles or scripts — everything relative to repo root / `CURDIR` / Nix store outputs.
- Production and QEMU are **separate `nixosSystem` builds** from one `mkAppliance`; they share package derivations but not a repackage of each other.
- The flake exposes the appliance and installer outputs; `nix flake update` for nixpkgs bumps, and a bump triggers a full squashfs recompute.

### Makefile
| Target | Behavior |
|--------|----------|
| *(default)* / `help` | Print help; no build |
| `build-production` | Production SquashFS + `vmlinuz` + `initrd.img` → `out/latest` |
| `build-qemu` | QEMU smoke profile (different root device / tmpfs mounts) → `out/latest-qemu` |
| `build-installer` | USB installer image |
| `run-qemu` | `build-qemu` if artifacts are missing, then boot the serial console |
| `qemu-clean` | Remove QEMU state |
| `repart-generate` / `disk-repart` | `partitions.yaml` → `out/repart.d`; apply to `DISK=` |
| `grub-cfg` | Stage `boot/grub.cfg` → `out/grub/` |
| `test` | Tier A unit tests (`scripts/run-unit-tests.sh`) — no root, no hypervisor |
| `lint` | Ruff (`nix run nixpkgs#ruff`) over `control-plane/`, `scripts/`, `installer/` using `ruff.toml` |
| `test-qemu` | QEMU HTTP smoke (`scripts/qemu-api-smoke-test.sh`) |
| `test-update-unit` / `repart-test` / `update-test` | Narrow update tests / partition + updater smoke (the latter two need root) |
| `check-untracked` | Flake gate: fail if non-ignored untracked files exist |
| `nix-gc` | Store GC before a build |
| `keys` | Generate local ed25519 test keys |
| `clean` | Remove `out/` |

`make test` and `make build-*` run `scripts/check-untracked.sh` first — the flake only sees tracked/staged files, so new paths must be `git add`’d.

`make lint` is deliberately **not** part of `make test`: `make test` must stay offline and hypervisor-free, and ruff comes from nixpkgs rather than from the appliance. Rule selection (and the documented exclusions) live in `ruff.toml`; adding a rule means cleaning its existing findings in the same change.

### Versioning
- Default: `YYYYMMDD.NN`, where `NN` is a **local build counter** (`.build-counter`, gitignored), computed by `scripts/version.sh` and passed as `APPLIANCE_VERSION` into `nixos/modules/version.nix`.
- Override: `VERSION=20260810.01 make build-production` for official releases.
- Because the version lands in the toplevel (`metadata.nix` → `systemd.tmpfiles.rules` for `/run/appliance/version`) **and** in derivation names, every build produces fresh artifacts — including a full `mksquashfs` at `zstd -Xcompression-level 10` over the whole root closure. That recompression is the irreducible per-build cost; the GPU stack is not (see below).

### Build cache & cycle time

Measured 2026-10-09 (full numbers in [`proposals/M6/BUILD-CYCLE-TIME-P7.md`](./proposals/M6/BUILD-CYCLE-TIME-P7.md)); the load-bearing facts:

- **The version string does not invalidate the GPU stack.** `llamaCppCuda` (`nixos/modules/lemond.nix`) is version-pure: the CUDA and npm-deps `.drv` paths are byte-identical at `APPLIANCE_VERSION=0.0.0-dev` and `20261009.07`, and the version string appears zero times in the CUDA derivation.
- **`appliance-artifacts` must pin the system closure explicitly.** `mksquashfs` output is opaque to nix's reference scanner, so `root-<version>.squashfs` advertises 113 references and *none* of them the toplevel. Before P7 the artifacts output referenced only the three image files, so `scripts/nix-gc.sh` — which `scripts/package.sh` runs at the end of every build — collected the just-built toplevel together with the locally compiled CUDA llama.cpp, forcing a from-source GPU recompile on the next build. `nixos/modules/artifacts.nix` now writes the toplevel path into `$out/closure-root`; nix detects a plain-text store path, so the build root keeps the closure warm. (A plain-text store path is detected; a compressed one is not — that asymmetry *is* the whole mechanism.)
- **`--impure` makes any tracked edit dirty the build.** `scripts/package.sh` builds `.#artifacts` with `--impure`, so `self` is the dirty git tree: editing a `proposals/*.md` changes the toplevel and the squashfs. Benchmarks taken on a dirty tree are not repeatable.
- **Only `cache.nixos.org` is configured** (`nix show-config`: `substituters = https://cache.nixos.org/`, `max-jobs = 1`, so a ~109-derivation plan is serialized). `llamaCppCuda` is defined in-tree, so no binary cache will ever cover it — pinning it locally (above) is the only protection.

### Outputs (HTTP-ready layout)

```
out/appliance-<version>/
  vmlinuz
  root.squashfs
  manifest.json          # names, versions, hashes, slot metadata
  vmlinuz.sig            # optional until signing is enabled
  root.squashfs.sig
  manifest.json.sig
```

Distribution may later wrap this as a tarball or serve discrete objects; the layout stays the same either way. Current release channel: `https://lemonseed.ai/releases/stable/` (`IMAGE_BASE_DEFAULT` in `scripts/lemonseed_artifacts.py`). The channel moves to **GitHub releases** in M7 (P5): whatever the portal/updater end up doing must work against GitHub release asset downloads. Note that this layout does **not** map one-to-one onto release assets — GitHub caps each asset under 2 GiB, so `root.squashfs` has to ship as ordered parts. See the GitHub-releases backlog entry in §11 for the manifest-and-chunks design.

### Signing
- **Detached signatures** over artifacts (and/or a manifest listing hashes); `scripts/sign.sh`, keys from `scripts/gen-keys.sh`.
- Signing is **disabled by default** with local test keys (gitignored); release keys come later. The verifier runs at download/install time. Secure Boot / TPM are explicit follow-ons.

### QEMU validation
- `make run-qemu` boots kernel + SquashFS + serial console.
- `make test-qemu` boots the appliance-qemu profile and probes `/healthz`, `/api/status`, `/api/system`, `/api/boot`, `/api/data`, `/api/modules`, the shared theme stylesheet, the session gate (including that an unauthenticated **XHR** gets a real 401 rather than a 302) and the OWUI guards. The profile pre-pulls the pinned Postgres/Valkey images and serves Open WebUI from a host virtio-fs export (`nixos/modules/qemu-docker-cache.nix`) so smoke tests do not depend on registry pulls inside the guest. Opt-in e2e: `QEMU_E2E_CHAT=1` (pull + load + assert chat ready), `QEMU_E2E_GC=1` (disk-space eviction).
- Full A/B disk and GPU validation happens on **real hardware**; QEMU has no GPU.

---

## 9. Security posture (as built)

| Layer | Approach |
|-------|----------|
| Transport / update authenticity | HTTP + **detached signatures** on artifacts (signing optional until release keys exist); TLS deliberately absent on the LAN |
| Root filesystem | Read-only SquashFS; no overlay |
| Mutable state | Minimal btrfs binds; restrictive mount flags |
| Edge auth | **CP-native login** at the nginx boundary: signed stateless session cookie, `auth_request` → CP, identity via `X-Forwarded-*` set **only** from the verify response. Modules stay auth-naive; OWUI's own login is disabled |
| OWUI write surface | Manifest-declared guards rendered to nginx; enforced sections return JSON 403 with a human `detail` |
| Secrets | CP-provisioned per-module DB credentials; OWUI API key restricted to the manifest's endpoint set; no secrets in the world-writable tree |
| Boot trust / TPM / Secure Boot | Deferred |
| Driver redistribution | All-in-image for simplicity; revisit if licensing blocks redistribution |

**Known bring-up exposure (intentional, must be closed before general availability):** root has an **empty password** and tty2 auto-logs in a root shell. Anyone with physical access owns the box. This is the reason lockout recovery is a preserve-reinstall marker rather than a password reset, and it is the pairing target for the encryption/TPM and GRUB-password work in §11.

---

## 10. Repo layout

```
.
├── DESIGN.md                 # this file — as-built contract + backlog
├── AGENTS.md                 # engineering process + test lifecycle
├── README.md, Concept.md, Requirements.md
├── Makefile, flake.nix (defines `mkAppliance` + the appliance / appliance-qemu
│   / installer outputs), flake.lock
├── partitions.yaml           # layout source of truth (§5)
├── nix-install.sh            # installer bootstrap entry
├── nixos/
│   ├── configuration.nix     # production profile composition
│   ├── installer/            # installer profile (configuration.nix + modules)
│   ├── packages/             # Nix-side package glue (lemonseed-ab)
│   └── modules/              # one concern per module:
│       ├── artifacts.nix, build-root-squashfs.nix, metadata.nix, version.nix
│       ├── storage.nix, ab-boot.nix, ab-root.nix, update.nix
│       ├── gateway.nix, control-plane.nix, data-services.nix
│       ├── lemond.nix, nvidia.nix, amd-gpu.nix, fhs-compat.nix
│       ├── console.nix
│       └── qemu-test.nix, qemu-docker-cache.nix
├── boot/grub.cfg             # A/B GRUB config
├── pkgs/                     # local package derivations (lemonade Python SDK)
├── control-plane/            # the CP: stdlib HTTP server + modules
│   ├── stub.py               # HTTP surface + portal HTML
│   ├── auth.py               # sessions, groups, gate snippets, login
│   ├── db.py                 # CP Postgres schema + access
│   ├── owui-policy.json      # the CP↔OWUI contract (§3.5)
│   ├── owui_policy.py        # manifest engine (validate/reconcile/render guards)
│   ├── owui_client.py        # thin stdlib OWUI admin-API client
│   ├── owui_portal.py        # ownership view for the settings panel
│   ├── modules.py            # module supervisor (blue/green + reconcile wiring)
│   ├── data_services.py      # Postgres/Valkey supervisor + provisioning
│   ├── chat_ready.py         # model pull/load/pin + chat readiness
│   ├── hardware.py, catalog.py, catalog/models.json, model_store.py
│   ├── lemond_client.py      # thin lemond localhost-API client
│   └── test_*.py             # Tier A unit tests (run from control-plane/)
├── installer/                # USB installer (install.py, disks.py, preflight.py, server.py) + tests
├── scripts/                  # version.sh, package.sh, gen-repart.py, disk-repart.sh,
│                             # lemonseed-ab.sh, lemonseed_update.py, qemu-*.sh,
│                             # run-unit-tests.sh, check-untracked.sh, nix-gc.sh,
│                             # sign.sh, gen-keys.sh, and their test_*.py
├── assets/brand/             # brand marks / favicon
├── artifacts/                # captured hardware references (lspci, vulkaninfo)
├── proposals/                # milestone design records (§2) + forward-looking ideas
├── keys/                     # optional local test keys (gitignored contents)
└── out/                      # build outputs (gitignored)
```

---

## 11. Backlog (consolidated review)

One home for deferred ideas, grouped for periodic review; promoting an item means assigning it a milestone. Origin in brackets; superseded/done work lives in the decision log, not here.

**Models & chat UX**
- **LiteLLM friendly model names:** stable, friendly models (“Lemonseed AI”, “Lemonseed AI with Content Filters”) fronting lemond, so model name/path changes never churn OWUI model config. The friendly models become the manifest-owned `models` entities (`lemonseed-ai`, `lemonseed-ai-filtered` are already reserved in `owui-policy.json`), replacing the env-era `DEFAULT_MODEL_METADATA`/`DEFAULT_MODEL_PARAMS` approach. Also the cheap path to the OpenAI-compatible endpoint below, and the natural home for group-driven content filtering. **Pulled into M7.** [was M6.3b]
- **Custom model catalog:** friendly display names (e.g. flagged labels), Lemonade `user_models.json` / registry overrides so Open WebUI and Lemonade agree — not alias hacks at load time. [was M6]
- **Users & groups management:** portal CRUD for household accounts (create / rename / reset password / disable / delete), roles as group membership (Admin / User / Disabled), group boolean attributes (below), and the OWUI mirror for membership + admin. This work also owns flipping the two account guards from `advertise` to `enforce`. Day one is immediate OWUI delete on delete. **Pulled into M7** (was scheduled as M6.2b).
- **Group boolean attributes (POSIX-like):** users in multiple groups (e.g. `Family` + `Kids`), attributes inherited from groups, combined with logical **OR** for booleans (and append for lists, if lists ever appear). Example attribute: “enable content filtering”. Absorbs the older “account types (child/adult)” idea — there is **no** `users.account_type` column today, and there should not be one; a group attribute is the composable version. Feeds the LiteLLM filtered-model name above. [new 2026-10]
- **Last-enabled-admin guardrail:** §3.3 derives roles from groups (correct), but nothing prevents demoting or disabling the final admin, and nothing forces a re-authentication when membership changes. Add the guardrail in the M7 users & groups work (session revocation itself arrives with M6.6's generation counter). [new 2026-10]
- **MoE vs dense preference by platform:** prefer **MoE** models on low-memory-bandwidth platforms (iGPUs) and dense on high-bandwidth dGPUs when a family has similarly-sized MoE and dense options. Needs a `moe: true` catalog flag + a bandwidth heuristic in `best_fit_model`. [was M6.5]
- **GeoIP model preference:** lightweight GeoIP lookup; prefer "local" model families for the user's country (Qwen for China, Gemma for the US) when ranking the default pick. [was M6.5]
- **Advanced model tab:** an "advanced" picker tab exposing the full `lemonade list` catalog so power users can pick off-menu models. [was M6.5]
- **Catalog models not in the Lemonade catalog:** support models in our catalog that the main Lemonade catalog does not yet offer. Requires a direct HF checkpoint path + `lemonade load` with an explicit checkpoint, plus the llama.cpp arch-support caveat (our pinned llama.cpp must support the architecture). Add a **build-time check** that every catalog `checkpoint` exists in the pinned `server_models.json` — our catalog once disagreed with the registry on a checkpoint's case, which broke the disk-space guard's directory matching. [was M6.5]
- **Local Wikipedia cache:** experimental indexed Wikipedia snapshot on spacious, well-connected appliances as an optional knowledge source for OWUI (when native KB tools are re-enabled). [was M6]
- **More complete user profiles:** full name, email, etc. [was M6.5]
- **Model weights survive an upgrade (landed; needs one more bare-metal confirmation):** the “re-downloaded after upgrade” report turned out to be lemond re-verifying a cached 7.3 GB file (~96 s, no bytes moved). The CP now reads lemond's `downloaded` flag before deciding, skips the pull and the disk guard when cached, and says "…is already on disk — loading it…". Confirm the wait is gone on the next bare-metal reboot. [2026-10]
- **No silent model re-fetch (landed, confirmed):** lemond's `auto_check_model_updates` is pinned false, so a reboot goes straight to *Loading … onto the GPU*. [2026-10, confirmed 2026-10-09]
- **Download rate / ETA — needs a lemond API, not a probe.** The old speed-probe is deleted (its URL 404'd, and the ETA never populated). Re-verified against the pinned lemond 11.5.2: model detail carries only `downloaded` / `size` / `checkpoint`, and every candidate progress route 404s, so **no rate or ETA is computable from our side**. Revisit *with* a progress API from upstream. The toolbar **NET** meter below is the mitigation we can build today. [2026-10-08]
- **SearXNG web search:** self-hosted SearXNG as a module so OWUI KB/web-search tools work without a third-party API key. [deferred past M6, moved to backlog 2026-10]

**Open WebUI integration**
- **Guard surface expansion:** promote more sections to enforce as portal usage shows what admins actually try to change. Day one: five mutating paths enforce; the two account guards flip with the M7 users & groups work.
- **Selective connection management:** let OWUI admins create their own model connections alongside ours (Lemonseed owns its entry, others untouched). Needs per-entry merge semantics on `openai.api_configs`; day one writes the whole set via raw import and enforces exactly one connection. [was M6.3b]
- **Body-aware request-time guards:** block OWUI-UI writes by request *content* — e.g. reject `POST /api/v1/models/create` when the body's `name` matches `^Lemonseed.*$` — instead of sweeping collisions after the fact at reconcile. Path-only nginx guards cannot see bodies; needs an nginx lua module or a small CP-side validation shim. [2026-10]
- **Group namespace / destructive header sync (gates group management):** trusted-header sign-in re-syncs OWUI group membership on **every** sign-in — `Groups.sync_groups_by_group_names` deletes memberships missing from `X-Forwarded-Groups`, so a group an admin creates in OWUI silently loses our users at their next sign-in. Upstream skips the sync when the header is empty, so the options are: stop sending the header (OWUI owns groups), or send a closed, CP-provisioned namespace so the sync stays additive. Must be settled before we build groups. [2026-10]
- **Six `groups.*` API-map entries are declared but unused** (`owui-policy.json`): they exist for the group work and are not exercised by any code path. Either use them or drop them so the manifest keeps meaning “what we do”. [2026-10]
- **SCIM evaluation:** revisit if trusted-header provisioning shows friction. [was M6.3b]
- **LKG restore as an admin action:** the last-known-good config snapshot today only recovers a *broken* reset. A standalone “restore my settings” endpoint is not built because a reconcile rebuilds everything Lemonseed owns. [2026-10]
- **Config-table snapshot for a failed reset:** before `TRUNCATE TABLE config`, copy the table so a reset that cannot rebuild rolls back byte-for-byte instead of relying on the version-gated LKG snapshot. Add only if reset proves buggy. [2026-10]
- **Manual reconcile button may be unnecessary:** the “Apply Lemonseed settings now” endpoint is the escape hatch for “an admin broke something and wants it back now”. If we never use it, delete it. [2026-10]
- **OWUI auth-provider config is admin-editable:** an admin can add LDAP/OAuth/SCIM config in OWUI's own admin UI (guards do not cover it), and OWUI's OAuth is already half-configured by our trusted-header setup. It cannot actually be used — every OWUI call traverses the Lemonseed session gate — and a settings reset clears it, so day one leaves it alone. Decide whether to guard those paths or keep relying on reset. [2026-10]
- **Directory authentication (choose the layer before building):** LDAP/OAuth/enterprise-directory sign-in for household accounts. Unresolved: OWUI's built-in providers (admin-editable, unguarded, bypasses our portal session + TOTP story), a CP-native directory integration, or host-level PAM/sssd with the CP as the only consumer. Must not weaken the invariant that the nginx `auth_request` gate is the sole login path. [2026-10]

**Accounts & data lifecycle**
- **Optional real email addresses + basic SMTP:** synthetic `@localhost` identities are an internal join key today. Let users optionally attach a real address, and give the appliance minimal outbound SMTP (password reset, notification mail) — which needs per-account addresses rather than derived ones and probably a CP-managed relay credential. The service account never needs this. [2026-10]
- **Tombstoned user deletion (undelete window):** mark a deleted account unusable immediately, then an async worker wipes it across CP + OWUI + pending backups after a ~7-day undo window. Must be evaluated against jurisdictional wipe-within-N-days rules (the tombstone revokes access instantly; data persists during the window). Day one is immediate hard delete. [2026-10]

**Portal & platform UX**
- **Ownership report as a build artifact, not admin UI:** the config-ownership classification (`owui_portal.build_view`) is developer/agent detail — the portal stopped rendering it after bare-metal review, but the payload is still assembled at request time. Generate it at build time instead so the runtime payload shrinks and the detail cannot creep back into the UI. [2026-10]
- **OpenAI-compatible endpoint toggle:** let an admin expose the appliance as an OpenAI-compatible endpoint for other LAN apps, with simple key management. Two candidate engines: OWUI's own OpenAI-compatible surface, or a CP/LiteLLM-side endpoint in front of lemond (the LiteLLM item makes the latter cheaper). Constraint already decided: `auth.enable_api_keys` is **Lemonseed-managed** (the CP's own key depends on it) while per-user keys stay user-owned, so the toggle must not fight that. The portal's `API` panel is where it belongs; consider a high port that does not traverse the auth layer. [was M6, consolidated 2026-10]
- **Network throughput meter:** a **NET** section in the app toolbar (alongside LOAD + STORAGE) showing Tx/Rx bytes/s over a trailing ~10 s average, with a bar where 100 % = link speed. Read the default-route interface (`ip route get 8.8.8.8`), then sample `/proc/net/dev`. Ethernet first; keep it modular so WiFi can be tuned separately. Expose via `/api/status` telemetry. This is also the substitute for the impossible model-download ETA. [was M6.5]
- **Internationalization & accessibility:** multiple languages and dialects, number units, and assistive-technology support (alt text, screen readers). [was M6]
- **Conduit mobile client:** evaluate [Conduit](https://conduit.mobile/), an open-source Open WebUI client, once the LAN UX is stable (§0). [deferred past M6, moved to backlog 2026-10]
- **Shared channels & calendar:** household-shared chat channels and a shared calendar as first-party appliance data, not per-user OWUI state. Needs its own data-plane ownership decision (Postgres schema vs module). [deferred past M6, moved to backlog 2026-10]
- **UX theme support:** multiple preset themes (light, dark, professional, playful). [was M6]
- **Settings page restructure:** one main "Lemonseed" section for system-wide entities, then one settings box (or tab) per module — Open WebUI as the first module — so a module can contribute parameters and status when installed. [2026-10]

**Modules & infrastructure**
- **Module catalog specs:** define a Lemonseed **module configuration file** (one file per module in a catalog directory in-tree): execution params (docker-compose fragment / pinned image), UX (logo, name, description, help), and dependencies (Postgres, Valkey, future object store). Catalog entries are what users see and what the CP downloads/sets up. The catalog needs its own **test/release cycle**, and should be hostable on lemonseed.ai/github so new offerings and versions can arrive without a full appliance upgrade. [was M6]
- **Module image pins (strategy):** every Docker module tracks the **newest upstream stable** tag we have validated, pinned explicitly in code so field appliances never float on `:latest`. The build/rebase pipeline bumps the pin, runs unit + QEMU (and hardware when the module is user-facing), then lands it. [was M6]
- **ComfyUI image generation module:** ComfyUI as a module driven through Open WebUI workflows, VRAM-gated so it never competes with a loaded chat model (see the image-gen row in §0). [deferred past M6, moved to backlog 2026-10]
- **Live API health:** CP status fields should report **current** health via cheap live checks (`pg_isready` / `SELECT 1`, Valkey PING, HTTP ready) rather than a cached “daemon started once”. If Postgres stops accepting connections after boot, the API must flip to unhealthy. [was M6]
- **Backup / restore:** transactionally clean, space/time-efficient backup including Postgres dump, Valkey dump, and per-module **durable** volumes declared in the module spec. Targets: USB (preflight free-space check; optional encryption) and local SMB/NAS shares. [was M6]
- **Encrypted user data** at the block/filesystem level, using TPM for keys when a working TPM is detected, paired with a GRUB password and locked-down local tty/ttyS login so a stolen box does not hand over household data. Pairs with closing the empty-root-password bring-up hole (§9). [2026-10]
- **TOTP / MFA:** see [`proposals/AUTH-TOTP.md`](./proposals/AUTH-TOTP.md); planned for the `admin` group. [was M6.3]
- **Build cycle time, residual:** the root cause of the CUDA recompile is fixed (`nixos/modules/artifacts.nix` pins the system closure; mechanism in §8). Still open, in rough value order: (a) stop embedding the version in the hashed toplevel (`metadata.nix` versionFile → tmpfiles `/run/appliance/version`) so a build-counter bump does not force a full `zstd -Xcompression-level 10` re-`mksquashfs` of a ~3.2 GB image — requires deciding how `/run/appliance/version` is delivered out-of-band, so it needs its own design; (b) prune `out/`, which holds 177 GB across 84 `appliance-*` dirs plus a 32 GiB `qemu-userdata.img`, on the **same** filesystem as `/nix`; (c) `scripts/nix-gc.sh --if-needed` is dead at 49 % store usage — separate "always prune stale `out/.build/*` roots" from "collect the store only under space pressure"; (d) `max-jobs = 1` serializes the ~109-derivation plan (a machine-level `nix.conf` choice, not a flake setting); (e) whether `nix-amd-ai` publishes a usable binary cache is **unverified** — and it can never cover `llamaCppCuda`, which is defined in-tree. [measured 2026-10-09, P7]
- **GitHub releases as the distribution channel:** replace `https://lemonseed.ai/releases/stable/` with one GitHub release per version, carrying the USB installer image and the payload blobs, published by a single `make release` target (build → sign → create release → upload assets) that works without secrets in the tree. Supersedes the earlier `make release`-only stub. **M7 P5 assumes this channel**; details below are grounded in docs.github.com as read 2026-10-12.
  - **Hard limits that shape the design:** a release may have up to 1000 assets, and *each file must be under 2 GiB* (GiB, not GB), with no limit on total release size or bandwidth. `root.squashfs` is ~3 GB today, so **sharding is mandatory**, not an optimization.
  - **Mechanics to respect:** releases hang off Git tags. Asset filenames containing special/non-alphanumeric characters or leading/trailing periods get renamed server-side, so part names must be conservative ASCII (`payload-000.part`). Downloads via the API need `Accept: application/octet-stream` and clients must handle `302`. Uploads go to the release's `upload_url` (`uploads.github.com`) as a raw body; a mid-upload `502` leaves an empty asset in `state: starter` (safe to delete), and re-uploading a duplicate filename returns `422`. Unauthenticated API access is **60 requests/hour per IP**, which is fine for an occasional update check but rules out polling.
  - **Chunking sketch (user's proposal, elaborated):** `manifest.json` stays **outside** the tar and is both the assembly plan and the root of trust — it describes the reassembled files (outputs: name, length, SHA-256) *and* the chunks (inputs: name, order, length, SHA-256), so reassembly is verifiable before anything is untarred. Build the tar canonically (`tar --format=pax --sort=name --mtime=@0 --owner=0 --group=0 --numeric-owner`, fixed file order) so identical inputs produce a byte-identical stream, then cut it into fixed **1 GiB** parts with names that are *stable across releases* — that keeps part boundaries comparable, so an updater can keep parts it already has and refetch only the ones whose SHA changed. Install order: verify each part → concatenate → verify the tar → untar to staging → re-verify every extracted file against the manifest → only then flip the A/B slot.
  - **Resume:** GitHub documents no `Range` support for release assets, so treat download resume as *checkpoint after each verified part*, not server-side resume.
  - **Trust model:** the root of trust stays **our detached signature over `manifest.json`** (§8 Signing) — verifiable on an air-gapped installer with no network and no `gh` on the box. GitHub's asset `digest` field is *transfer integrity only*, not authenticity (metadata and payload come from the same origin). GitHub **artifact attestations** (`actions/attest@v4` with `id-token: write`/`contents: read`/`attestations: write`, `subject-path` for binaries; `gh attestation verify <file> -R owner/repo`) are attractive as a *CI-side* provenance record, but are plan-gated (Free/Pro/Team: **public repos only**; private/internal needs Enterprise Cloud) and need `gh`/Sigstore plus network on the verifier, so they must never be the appliance's check. The attestations doc points onward to *release attestations* / "Immutable releases" for release-level provenance — that page would not load when researched, so read it before designing the workflow.
  - **Same theme as the image-pin decision** (M6.6 T5): build from upstream `:latest`-equivalents, but *expand to digest/tag at build time* so a field appliance never floats a Docker image version independently of the release it installed.
  - **Open questions before this can be scheduled:** is the USB installer image itself under 2 GiB (if not, it needs the same part treatment)? Does the updater resolve `…/releases/latest/download/manifest.json` or a version-named asset — and can `IMAGE_BASE_DEFAULT` move to a GitHub base without bricking boxes already in the field? Public or private repo (this decides the attestation plan)? How many releases do we retain, and does `make release` run from a dev machine or CI?
- **Cloud service / DynDNS:** remote access + Let's Encrypt; see [`proposals/CLOUD-SERVICE.md`](./proposals/CLOUD-SERVICE.md). [was post-M6]
- **Installer stream-to-disk:** partition first, stream SquashFS to `root-a`, avoid multi-GB RAM staging on the live ISO. [was M6]
- **Registry reachability preflight (hardening):** the preflight exists; keep improving the surfaced message and the retry/backsoff behavior so a dead WAN link never looks like a broken appliance. [was M6]

**Deferred platforms / hardware**
- AMD ROCm/HRX backend, AMD NPU (XDNA), heterogeneous GPU planner, omni (`collection.omni`) models (blocked on the lemond load path), ARM builds, Secure Boot/TPM root of trust, offline install payload.

---

## 12. Open questions / deferred decisions

1. **Initrd:** soft goal of none; still shipping one.
2. **Exact GPT type UUIDs** for A/B SquashFS — linux-generic in use for bring-up.
3. **`/var` mount flags** — validate empirically under Docker/Lemonade.
4. **Planned reboot vs `boot_once`:** **answered.** Only GRUB writes `boot_attempted`, so an ordinary reboot silently completes an armed one-shot upgrade. The portal therefore never issues a bare reboot while an update is staged: it offers **"Reboot into the new version X"** (default/highlighted) versus **"Reboot without updating"**, so the two intents stay distinct (M7 P5).
5. **Graceful data-plane shutdown:** **resolved empirically** (2026-10-11): `shutdown -h`, `shutdown -r` and the ACPI power button all shut the current image down and bring Postgres back clean. An explicit ordered-stop assertion in QEMU is still worth adding alongside the M7 P5 power controls, but it is no longer an open correctness question.
6. **Session revocation mechanism:** **answered** — a per-user **generation counter in Postgres** that `session-verify` compares against the token, not a per-box secret rotation. Cheap per event, no global logout on a single revoke, and it answers "who is still signed in" as a side effect (M6.6).
7. **Portal XHR error contract:** **answered** — one rule for every portal request: **401** redirects `window.top` to `/login?rd=…`, **403** is surfaced as "signed in, but not permitted" and never redirected, **5xx** is retried with backoff. See §3.3 and [`proposals/M6/SESSION-AND-ERROR-CONTRACT-M66.md`](./proposals/M6/SESSION-AND-ERROR-CONTRACT-M66.md).
8. **`lemond` packaging:** from-source Nix today; whether to flake-input `nix-amd-ai` later for AMD only, or vendor a slim copy.
9. **Heterogeneous GPU planner:** inventory-ready schema now; scheduling later.
10. **Per-key OWUI policy values:** the manifest framework is locked; mode / cadence / value for each dotted key is decided per method as each config section is implemented.
11. **OWUI egress guardrails:** durable config lets an OWUI admin add external model connections (family-data egress), and the writers are now guarded. Open product call: keep exactly one connection, or move to per-connection selective management and grant power users their own?
12. **Removed-user OWUI history:** day one is an immediate OWUI delete on delete. Open: adopt the tombstoned 7-day-undelete flow, and how it squares with jurisdictional right-to-erasure timelines.
13. **Encryption:** deferred; document a LUKS/migration plan before friends-and-family data. See `proposals/AUTH-TOTP.md`.

---

## 13. Decision log (summary)

| Topic | Decision |
|-------|----------|
| Product | Local multi-user home AI hub; Concept.md is product source |
| Arch | x86_64; ARM later |
| Firmware | EFI-only for now |
| Bootloader | GRUB EFI |
| A/B | GRUB `grubenv` one-shot + lock-in |
| Root | SquashFS via `dd`; no overlay |
| User-data | Installer creates the LVM PV only; the first-boot provisioner runs idempotent VG/LV/btrfs provisioning |
| Mutable state | btrfs subvols `@etc`/`@var` mounted on `/etc`/`/var`; `/tmp` size-capped tmpfs (128M) |
| ESP | On-demand mount only |
| Apps / modules | Not baked into image; Docker at runtime; **state in Postgres/Valkey**, not bind-mounted SQLite |
| Data services | Shared PostgreSQL (+ pgvector), Valkey; CP-provisioned credentials |
| Inference runtime | **`lemond` embedded in SquashFS** (headless); Docker is not the Lemonade path |
| Module updates | Blue/green: start new → health-check → nginx cutover → stop old; CP supervisor owns lifecycle, not Docker restart policies |
| Container image pins | One pins file (`control-plane/container-images.json`) read by Nix at eval **and** by the CP at runtime; run by **digest**, falling back to the tag only when the image carries no `RepoDigests` (our QEMU tarballs). `make refresh-image-pins` / `check-image-pins` bump and audit them. **No `docker pull` on the boot path** — the CP supervisor pulls off-critical-path and reports through `/api/data`; the boot path runs only what is local and logs the reference it used |
| GPU | Drivers in base; NVIDIA CUDA + AMD Vulkan (Mesa RADV); single image covers both; no VRAM pooling; ROCm/NPU deferred. NVIDIA validated on hardware; the AMD path has **never run on AMD silicon** (no hardware, and QEMU is CPU-only) |
| Inference UX | Open WebUI primary; catalog-selected LLM via `lemond` |
| Models | Stored under `/var/lib/lemonseed/`; versioned catalog fit; LRU disk-space maintenance; `auto_check_model_updates` off |
| llama.cpp release | **Build from source at image-build time, pinned to a tagged upstream release** (never HEAD). One build per backend (`llamacpp-cpu`/`-cuda`/`-vulkan`) that lemond is pointed at; backend choice is the CP's (`select_backend`), not lemond's. CUDA is our own source build; CPU/Vulkan track `nix-amd-ai`. |
| Lemonade packaging | From-source Nix; wire Nix backends; patterns from nix-amd-ai |
| Control plane | systemd root; Python stdlib; privileged API + web UI in one process; **CP state in Postgres** |
| CP API namespacing | CP APIs live under `/_lemonseed/api/*`, not `/api/*`. Open WebUI's frontend calls `GET /api/models` for its OpenAI-style list, and an exact-match CP location was silently winning and returning the picker payload to OWUI. `/_lemonseed/` is the reserved internal namespace. |
| Builds | Local; flakes; `build-production` / `build-qemu` / `run-qemu` / `qemu-clean` |
| Version | Date + local counter; `VERSION` override |
| Partition config | YAML source (`partitions.yaml`) → `gen-repart.py` → systemd-repart as apply engine |
| Signing | Layout ready; detached sigs; optional/local keys for now |
| Distribution | HTTP; versioned artifact directory |
| Installer | M4; partitions + optional admin lockout reset marker; no auth work for fresh installs; reinstall releases LVM/DM |
| Encryption | Deferred; TPM + GRUB password planned together with closing the bring-up root login |
| Open WebUI / auth | **M6.1:** OWUI auth off. **M6.2:** edge auth only at the nginx boundary — modules stay auth-naive (no per-container OAuth) |
| Edge auth (M6.2) | **CP-native login** (signed HMAC-SHA256 session cookie, branded page). Replaces Authelia (whose hardcoded `https` requirements were unfixable on an HTTP-only LAN). nginx `auth_request` → CP `/_lemonseed/session-verify`; identity headers set **only** from that response. Cookie: `Path=/; HttpOnly; SameSite=Lax`, no `Secure` by design. State in Valkey: **sliding** 1 h idle window under a 10 h absolute ceiling, keyed by boot id; `users.token_generation` gives real revocation; the gate **fails closed** if the store is down. |
| Admin gating | Enforced in the **CP** from `X-Forwarded-Groups`; the nginx admin include is intentionally empty because nginx `if` runs before `auth_request` sets its variables |
| CP authority | **CP + Postgres is the source of truth** (accounts = standard **users + groups**, many:many, Linux-style; elevation = `admin` group, no singular owner); config files / env / secrets are derived and regenerated |
| Admin bootstrap | **Single `/setup` path** (synchronous; name + timezone + admin creds) — first admin only while the `admin` group is empty; lockout = preserve-reinstall + “Reset admin credentials” marker (physical access) clears the `admin` group so `/setup` reopens |
| OWUI config authority (M6.3b) | **DB after first boot.** OWUI's Postgres `config` table is authoritative; env = bootstrap-only (the import-time set in §3.5 plus a CP env-generation marker). The CP reconciles via OWUI's admin API from a versioned manifest (`control-plane/owui-policy.json`) declaring per-key mode + cadence + `reason`, so behavior stays legible outside Python. |
| OWUI interaction modes | Two orthogonal axes: **mode** per entity kind (`initial_setup`, `managed`, and for collections `selective_management` / `full_management`) and **cadence** (`at_boot` / `on_upgrade` / `manual`) bounding re-assertion. `initial_setup` must mean "delivered as env at first boot", because v0.11.0 seeds *every* known key into the table at first boot. |
| OWUI entity ownership | By **CP-defined id**, namespaced by a reserved display name (`^Lemonseed.*$`), collision-swept at reconcile. Chosen over UUID-based ownership because v0.11.0 `ModelForm.id` is a client-supplied string we write, so ids cannot churn across upgrades/reinstalls; a wiped DB makes our entities *missing* and the reconciler re-creates them under the same ids. |
| OWUI users collection | Managed as **membership + is_admin only**: the loop creates, promotes and demotes but **never deletes** accounts; an explicit CP member-delete propagates an immediate OWUI delete (account + chats wiped). Deleting users as routine reconciliation is too heavyweight — users own their own settings, including per-user API keys. |
| OWUI service account | The CP authenticates to OWUI as a built-in super admin (`lemonseed-cp`, display name **“Lemonseed Control Plane”**, `@localhost` email) over trusted headers **direct to the slot ports**, plus an endpoint-restricted `sk-` key derived from the manifest's api map. OWUI cannot hide users from the admin list, so protection = unmistakable name + id-targeted guards + OWUI primary-admin immunity (the CP account must be OWUI's first user). |
| Synthetic identity emails | One domain, `@localhost` — the only no-dot domain OWUI's `validate_email_format` exempts, so its admin UI can manage such rows. Never re-derived: OWUI keys identity on email, so a change duplicates the account rather than renaming it. |
| Guard denial shape | A guard's 403 body is **JSON with a non-empty `detail`** and the location sets `default_type application/json`; with `text/plain` Open WebUI discards the reason and shows its own “Server connection failed”. |
| Guard vs login redirect | Accepted: guarded paths answer **403 before** the auth gate can redirect (nginx rewrite phase beats access phase), so an expired admin session on a guarded path sees “managed by Lemonseed” rather than a sign-in prompt. Revisited in M6.6, where the XHR contract gives the client a way to recover; the mechanism is an open decision there. |
| Settings reset | **Admin-only**, explicit confirm, and done as a window: stop OWUI → `TRUNCATE TABLE config` → start → full reconcile → cut back over. Never guard `POST /api/v1/configs/import` — that is the reconciler's own writer. Reset clears OWUI **config** (admin-level settings); user preferences and model settings survive. |
| Portal content rule | The settings page shows **only what a home admin can act on**. Ownership/guard breakdowns, API method tables and other sausage-making moved out of the UI; developer-facing detail should be a build artifact. |
| OWUI guards day one | Five mutating OWUI paths enforce; `POST /api/v1/auths/add` and `DELETE /api/v1/users/.*` stay `advertise` until the users & groups work (M7, formerly M6.2b) supplies user-list mirroring (enforcing them earlier would leave no way to create a user). The manifest strings that say so are pinned by tests as a tripwire. |
| Session storage | **Sliding sessions in Valkey** (already running), keyed by boot id so a reboot logs everyone out; state is disposable by design, which is what makes it cheap. Revocation via a **per-user generation counter in Postgres**, compared on every verify. Rejected: a per-box secret rotation (revokes everyone to drop one), and a purely stateless absolute-expiry token (cannot revoke, cannot slide). |
| Gate availability | The gate **fails closed** when Valkey is down, even though that means a login outage: the CP starts Valkey itself, so a stateless-token fallback would silently downgrade authentication. A broken-but-authenticated appliance is acceptable; an unauthenticated one is not. |
| Portal XHR errors | **401** → `window.top` to `/login?rd=…`; **403** → surfaced as “signed in, but not permitted”, never a redirect; **5xx** → retry with backoff. Rejected: swallowing 403 silently (dead UI, see the `refreshStatus` string-pinned test that asserted doubly-dead code), and redirecting on 403 (loses the distinction between “not you” and “not yours”). |
| `/api/status` exposure | Split, and shipped: `/api/status` answers pre-login with an explicit **public subset** (installer handoff only) and the full snapshot moved to the session-gated **`/api/system`**. Household facts (e.g. admin user count) had been readable pre-login, and an unauthenticated poll could make the appliance run seven `systemctl` probes plus Docker and lemond queries. |
| One portal fetch helper | **One** `portalFetch()` for every CP call the portal makes, rather than per-caller handling: fifteen hand-written `fetch()` calls each invented their own answer to a dead session, and the one that looked correct was unreachable. Twelve were control-plane calls and now go through the helper; three stay outside it for stated reasons. Retries are capped and **GET-only**. Rejected: swallowing 403 silently (dead UI), redirecting on 403 (loses "not you" vs "not yours"), and retrying POSTs (a lost response would run a reset twice). |
| Gateway error pages | Lemonseed-branded **static** page served by nginx for 500/502/503/504 (`error_page … =503 /_lemonseed/cp-down`, an `internal` location reading the CP's store-sourced static dir). Replaces the `@cp_interstitial` proxy, which could never render: nginx does not run `error_page` inside a location it reached *through* `error_page`, and the fallback proxied to the very process that was down. Self-reloads once `/api/status` answers |
| My Profile vs Settings | Non-admins get a self-service **My Profile** (name, password, email, future TOTP; shows role and last login) and **never** the Settings/System Control Panel. Logout lives in My Profile, since every user needs it and only admins need Settings. |
| Update policy | **Never auto-download and never auto-install.** A weekly check (heavily fuzzed, `sleep $(( $RANDOM * 86400 / 32768 ))`) and a manual “check now” run the same workflow; on success they light a red dot on the settings icon for admins and arm the update button. Rejected: silent auto-update on a household box that may be mid-pull. |
| Shutdown timing | **systemd owns the timeout** for reboot/shutdown; the CP asks and gets out of the way rather than wrapping systemd in another timer script. |
| Destructive-action confirms | Typed "type the word to continue" interstitials are reserved for **data-destroying** actions (settings reset, user delete). Reboot, shutdown and update are data-safe, so they get an explainer plus a confirm — no typing. |
| Release channel | Updates must be compatible with **GitHub release** asset downloads; the current `https://lemonseed.ai/releases/stable/` path is being replaced (M7 P5). Distribution mechanics, the 2 GiB asset cap and the chunking design live in the §11 backlog entry, not here. |
| Lint gate | `make lint` (ruff from nixpkgs, config in `ruff.toml`) over `control-plane/ scripts/ installer/`, kept **out of** `make test` so unit tests stay offline. Curated rule selection; permanent documented exclusions are `UP045` (conflicts with AGENTS.md's `Optional` guidance), `TRY004` (those `ValueError`s are deliberately caught by the enclosing handler — `TypeError` would turn 400s into 500s), and `external = [BLE001, N802, PLC0415]` so the call-site `noqa` documentation stays without enabling the rules. |
| Test determinism | The suspected control-plane suite flake is **disproven**: 36 perturbed runs (shuffled, reversed, `PYTHONHASHSEED`, per-module isolation, leak probes) were all green. The real hygiene defect was process-global state (`owui_policy._last_good_manifest`, `_status`) leaking between test classes; the reconcile class now snapshots and restores it via `addCleanup`. |
| Build closure pin | `appliance-artifacts` writes the toplevel path into `$out/closure-root`. Necessary because `mksquashfs` output is opaque to nix's reference scanner, so the image's closure had **no** GC root and the post-build `nix-gc.sh` collected the locally compiled CUDA llama.cpp every single build. |

---

*Historical review checklists for M1/M5/M6 live in [`proposals/M6/REVIEW-CHECKLISTS.md`](./proposals/M6/REVIEW-CHECKLISTS.md).*
