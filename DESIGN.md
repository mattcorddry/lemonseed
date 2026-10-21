# Lemonseed Appliance — Design

Status: **living design, as built** — M1–M5 shipped; M6 landed through **M6.6** (Open WebUI durable config + control-plane reconciliation, AMD Vulkan support, model picker, session & error contract; 2026-10). M7 has begun: **P2 (SRP-6a sign-in), P3 (household accounts + audit) and P4 (groups + OWUI guards) are built** — as APIs; the portal screens over them are not. LiteLLM friendly model names and the portal power controls remain in **M7**.

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
- Offline USB image payload (installer downloads from GitHub releases; offline option later)
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
| **M6.4 — AMD GPU support** | Vulkan backend (Mesa RADV) for dGPU + Strix iGPU; runtime backend dispatch. **Validated on real Strix Halo hardware** (Ryzen AI Max+ 395 APU, shared-memory iGPU); the `ttm.pages_limit` shared-memory split and GTT-sized reporting both work end to end. **Not yet validated: AMD dGPU (PCIe card)** — implemented and unit-covered, but no discrete AMD card has been booted. QEMU is CPU-only, so no automated test loads a model through Vulkan | [`proposals/M6/AMD-VULKAN-M64.md`](./proposals/M6/AMD-VULKAN-M64.md) |
| **M6.5 — Model picker** | Portal model picker (families + best-fit), model disk-space maintenance | [`proposals/M6/MODEL-PICKER-M65.md`](./proposals/M6/MODEL-PICKER-M65.md) |
| **M6.6 — Session & error contract** | Sliding sessions in Valkey + Postgres generation counter, one XHR status contract (`portalFetch`: 401 → `/login` in the top frame, 403 surfaced, 5xx capped backoff), `/api/status` public subset + gated `/api/system`, branded gateway error pages + self-reload, one shared stylesheet root, container-image pins | [`proposals/M6/SESSION-AND-ERROR-CONTRACT-M66.md`](./proposals/M6/SESSION-AND-ERROR-CONTRACT-M66.md) |
| **M7 P2 — SRP-6a sign-in** | Password never crosses the wire: SRP challenge/verify CP routes + client-side enrollment, browser-side SHA-256 with KAT self-check, bcrypt login path **deleted** (`POST /login` → 405), Valkey-backed sign-in rate limit, schema v5 (since folded into the baseline, §3.3) | [`proposals/M7/PLAN.md`](./proposals/M7/PLAN.md) |
| **M7 P3 — Household accounts** | `usermgmt.py` as the only writer of `users`; account CRUD + self-service profile routes; OWUI mirror keyed on `users.owui_id`; append-only `audit_log` + journald copy with a 365-day sweep; schema **v6** over a v5 baseline (pre-baseline databases now **require a reinstall**). Portal: a **People** panel in Settings (accounts, groups, audit log) and a self-service **My Profile** page at `/profile/` | [`proposals/M7/USERS-AND-GROUPS.md`](./proposals/M7/USERS-AND-GROUPS.md) |
| **M7 P4 — Groups** | Group CRUD routes with `groups.system` protecting `admin`/`users`, `group_attributes` with the key set declared in code, membership mirrored to OWUI through `X-Forwarded-Groups`, and **every** OWUI account guard on `enforce` — create, edit and delete all refused at the edge, so the Open WebUI user panel is read-only end to end (D-17b) | same record |
| **M7 P5 — Power controls & update affordances** | SYSTEM panel power buttons (interlocked, going-away marker), **Software update** box: check job + download/write/reboot with the updater's stdout percentages, a 100% finish line plus a short reboot grace so the dialog can say "Update finished" before the box goes down, **Retry/Cancel** on failure, long hashes wrapped; per-request access trail in its own journald channel (`-t appliance-control-plane-access`, real client + user); nginx proxy-headers hash sized for the gate; GPU kernel modules load only on hardware presence (udev modalias rules, vendor 1002/10de). **M7 closed 2026-10-19** — "Beyond P1–P5" (LiteLLM friendly model names; modules definition + PoC) moved to future milestones | [`proposals/M7/PLAN.md`](./proposals/M7/PLAN.md) |

M6.1–M6.5 are all landed. **M6.4's coverage differs by AMD platform.** The **Strix Halo APU**
path (Ryzen AI Max+ 395 class — one chip, CPU + GPU, memory shared between them) is
**validated on real hardware**, including the `amdgpu-kernel-params.sh` shared-memory
carve-out and GTT-sized VRAM reporting. What remains unvalidated is the **AMD dGPU** path: a
PCIe Radeon card. That code exists and is unit-covered, but no discrete AMD card has been
booted on the appliance. The distinction matters because the two paths have different memory
semantics — the APU's model budget comes from a GRUB `ttm.pages_limit` carve-out of system
RAM, a dGPU's from its own VRAM. Separately, no *automated* test exercises Vulkan at all: the
QEMU profile pins `llamacppBackend = "cpu"`. See
[`proposals/M6/AMD-VULKAN-M64.md`](./proposals/M6/AMD-VULKAN-M64.md).

M6 close-out also carries **P0** (lint gate + test determinism; `make lint` is now the gate, and the suspected suite flake was disproven) and **P7** (build cycle time; [`proposals/M6/BUILD-CYCLE-TIME-P7.md`](./proposals/M6/BUILD-CYCLE-TIME-P7.md)).

**Moved out of M6 into M7:** **users & groups management** (formerly M6.2b — which also owns flipping the two account guards from `advertise` to `enforce`; **all three account guards, create included, are now `enforce`**, §3.5), **LiteLLM friendly model names**, and **portal power controls** (reboot / shutdown / update-check / logout / the merged SYSTEM panel). Sprint pulls are listed in [`proposals/M7/PLAN.md`](./proposals/M7/PLAN.md), which holds M7 feature work only.

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
| `/profile/` | CP self-service **My Profile** — own name, own address, own password | session |
| `= /profile` | → `302 /profile/` | — |
| `= /login` | CP login page | **open** (it *is* the sign-in form) |
| `= /logout` | CP | session |
| `= /setup` | CP first-boot setup | **open** (pre-admin trust window) |
| `= /_lemonseed/api/srp/{challenge,verify,rotate}` | CP SRP-6a handshake + self-service rotation | **open** (M7 — these *are* sign-in, so gating them is circular; `rotate` demands proof of the current password, and a forced rotation happens before a cookie exists) |
| `= /healthz`, `= /health` | CP | **open** |
| `= /_lemonseed/session-verify` | CP | nginx `internal` (auth_request target only) |
| `= /api/boot` | CP | **open** (installer handoff) |
| `= /api/status` | CP | **open** — public subset only: `status`, `role`, `version`, `setup_complete`, `setup.mdns_name`, `portal_url` |
| `= /api/system` | CP full snapshot (telemetry, chat, modules, data, auth, session store) | session |
| `= /api/hardware`, `/api/plan`, `/api/data`, `/api/modules`, `/api/chat` | CP | session |
| `= /_lemonseed/api/models` | CP model picker | session + admin |
| `= /_lemonseed/api/owui/{config,reconcile,reset-config}` | CP OWUI surfaces | session + admin |
| `= /_lemonseed/api/profile` | CP self-service profile (own account only) | session |
| `= /_lemonseed/api/users`, `/_lemonseed/api/users/<action>` | CP account management (M7 P3) | session + admin |
| `/_lemonseed/api/groups/<action>` | CP group management (M7 P4) | session + admin |
| `= /_lemonseed/api/audit` | CP audit trail (M7 P3) | session + admin |
| `= /_lemonseed/api/wordlist` | CP sign-in-code vocabulary (`words-en_US.txt`, M7 P3) | session + admin (the words are public; only the draw is secret) |
| `= /_lemonseed/api/power` | CP reboot / shut down (M7 P5, first increment) | session + admin |
| `= /_lemonseed/api/gc-test/*` | CP disk-GC test hooks | session + admin (QEMU e2e only) |
| `/_lemonseed/brand/`, `= /favicon.ico` | static brand assets (7 d cache) | open |
| `= /_lemonseed/lemonseed.css` | the one stylesheet root, `try_files` out of the image | open, revalidated |
| `/_lemonseed/lemond/` | `127.0.0.1:13305` | `allow 127.0.0.0/8; allow 172.16.0.0/12; deny all` — for modules on `lemon`, never LAN |
| `= /chat`, `= /chat/` | → `302 /?embed=1` | — |
| `/ws/` | Open WebUI (Socket.IO) | session, **no OWUI guards** |
| `/` (catch-all) | Open WebUI: SPA assets, `/api/v1`, `/openai`, `/ollama`, `/c/…` | session + **OWUI guards** |

Failure states are carried by two named locations. `@auth_unauthorized` (navigations get `302 /login?rd=$request_uri`, unencoded — see §3.3; anything labelled `X-Requested-With: fetch` gets `401` + JSON instead, because `fetch()` follows a redirect and would hand the portal login HTML at HTTP 200) and `@owui_loading`, which is genuinely branded because OWUI and the CP are independent processes, so a 502 from `/` while OWUI starts is normal. Every other upstream failure is answered by a **static** page: each gated CP location ends with `error_page 500 502 503 504 =503 /_lemonseed/cp-down`, an `internal` location that `try_files` `cp-down.html` out of the CP's store-sourced static directory, so it renders even when the CP is the thing that is down. `location = /` chooses its target from the map `$lemonseed_error_target` (document → the static page; `?embed=1` or an iframe `Sec-Fetch-Dest` → `/_lemonseed/interstitial`, which proxies the CP shell so an embedded frame keeps its path). The page polls `/api/status` and reloads `_top` when the CP answers. Two nginx rules shaped this and both are asserted against a live nginx by `make test-gateway`: `alias` is rejected inside a named location, and **`error_page` does not run inside a location reached through `error_page`** — which is why the old `error_page 502 → @cp_interstitial → proxy_pass :8080` chain could never render during the startup window it existed for. `proxy_intercept_errors` is off, so a 5xx the CP itself produced passes through untouched: a real bug stays visible. **500 belongs in those lists** for a reason that is easy to delete by mistake — an `auth_request` subrequest that *answers* with anything other than 401/403 fails the main request as **500** (Valkey down ⇒ verify 503 ⇒ main 500), while one that **cannot connect at all** surfaces as **502** (both measured on bare metal during the startup window).

`/_lemonseed/` is the reserved internal namespace for CP surfaces; the model picker lives there rather than on `/api/models` because Open WebUI's own frontend needs that path (see decision log).

**Control-plane HTTP surface (`control-plane/stub.py`)** — a single stdlib `socketserver`-based process on `127.0.0.1:8080`, no WSGI framework. GET: `/healthz`, `/api/boot`, `/api/status`, `/api/system`, `/api/hardware`, `/api/plan`, `/api/chat`, `/api/data`, `/api/modules`, `/settings/`, `/settings/content/`, `/profile/`, `/login`, `/_lemonseed/session-verify`, `/_lemonseed/api/models`, `/_lemonseed/api/owui/config`, `/_lemonseed/api/users`, `/_lemonseed/api/profile`, `/_lemonseed/api/audit` (`?target=` and `?limit=`), `/_lemonseed/api/wordlist`, the brand/favicon files. POST: `/login` (dispatched but answered **405** — see §3.3), `/logout`, `/setup`, `/_lemonseed/api/models`, `/_lemonseed/api/owui/reconcile`, `/_lemonseed/api/owui/reset-config`, `/_lemonseed/api/srp/{challenge,verify,rotate}`, `/_lemonseed/api/profile`, `/_lemonseed/api/users/<action>`, `/_lemonseed/api/groups/<action>`, `/_lemonseed/api/power`. The two action sets are fixed `frozenset`s (`stub.USER_ACTIONS`: `create`, `update`, `groups`, `disabled`, `reset-password`, `delete`; `stub.GROUP_ACTIONS`: `create`, `delete`, `member`, `attribute`, `attribute-remove`) rather than dispatched by reflection, so an unknown action **404**s from a closed list instead of reaching a function someone later added and never meant to expose.

**The settings control panel (`/settings/content/`)** is one document with a rail of five tabs down its left — General, AI Models, Users &amp; Groups, Open WebUI, Modules — and exactly one pane visible. The rail lives **inside** that document rather than in the portal shell on purpose: the chat view is an Open WebUI iframe that already owns a left rail and both of them auto-hide, so a second rail beside it is unusable. The shell therefore keeps its top bar, and only the settings document grew a rail. Every pane is in the served HTML with `hidden` on the four that are not selected, so the page reads before JavaScript runs (and QEMU asserts on the served markup, not on a browser's render). Every question the panel asks — edit a name, confirm a delete, add a group — goes through **one dialog component** (`stub.DIALOG_JS`) instead of the browser's `prompt`/`confirm`/`alert`, which paint outside this theme, carry one line of copy, cannot disable their own OK button, and block the event loop of the frame they open in. A typed confirmation keeps its OK **disabled** until the word matches, and Escape or a click outside means *no* (the dismiss value), never the default. Row actions the appliance would refuse are rendered disabled with the reason in their title: a button that cannot work should not look like a bug.

**Power (`POST /_lemonseed/api/power {action: reboot|shutdown}`, admin at the edge *and* in the CP)** is refused with **409** naming the work in flight — a model pull, a policy reconcile, an Open WebUI window — because a reboot in the middle of those leaves a truncated weights file or half-applied managed settings. `chat_ready.in_flight_work()` and `modules.in_flight_work()` read `Lock.locked()` / `Popen.poll()`, so asking never blocks behind the work it is asking about. The prepare pass publishes **what it is doing** (pull / load) and the interlock refuses only for those phases: the readiness verify holds the same lock but is a read, and refusing through it made both buttons fail right after boot while the model was already serving (bare-metal review). The **200 goes out first**, then a daemon thread sleeps ~2 s and execs `systemctl`; answering first is what lets the portal tell "refused" apart from "gone". A reboot writes the same `reboot-pending.json` marker the AMD GPU kernel-params service writes, so `/api/chat` answers `rebooting` and the shell says so instead of letting a progress bar die quietly (the CP deletes the marker at startup). A shutdown writes no marker: the appliance is not coming back by itself, and the confirm copy says so before anyone presses it.

**Updates (`GET /_lemonseed/api/update`, `POST …/update/check`, `POST …/update {retry}`, admin at the edge *and* in the CP)** are the SYSTEM panel's Version section: the running version renders server-side from `/run/appliance/version`, and *Check now* / *Download and install* are **jobs** — `202` plus a pollable status (downloading / writing / rebooting / failed) read off the updater's own log lines, never a synchronous `apply`. A second start while one runs, or a power press while an install does, is `409` naming it. Install runs `lemonseed-update apply --reboot` after a **preflight** that refuses an unknowable slot pointer, a pending `boot_once`/`boot_attempted`, a leftover arm-time record, or a channel version equal to the running one — each a clear message, with `--force` as the CLI escape. The CP's access trail likewise stays out of the service log: **one line per request on its own journald channel** (`SYSLOG_IDENTIFIER=appliance-control-plane-access`; `journalctl -t appliance-control-plane-access`), carrying the client IP from `X-Forwarded-For` (otherwise every request reads as 127.0.0.1), the forwarded username, the request, and the status as searchable fields. Journald's rotation and retention apply, and the service's own log keeps diagnostics and errors. The panel holds **one dialog** across the whole job, with a progress bar fed by the updater's own stdout percentages, and when the status API answers again after the reboot the browser is sent to `/login`; retry stays **cancel-then-apply** through the API and the CLI — there is deliberately no Try-again button. The update's own reboot rides the `reboot-pending.json` marker so the shell says "rebooting". The **failed-update banner** rests on a record the updater writes when it arms GRUB `boot_once` (`/var/lib/lemonseed/update-attempt.json`): booting any slot other than the target while running anything other than the intended version is the failed-or-stuck signature, and booting the target — or running the intended version — clears it. A weekly `lemonseed-update-check` timer records the channel's latest version, so the panel can say an update is available before anyone asks.

Portal settings render as **two URLs**: `/settings/` is the shell (nav + iframe) and `/settings/content/` is the inner document. Panels, in order: **Lemonade AI Engine** (chat status + active model), **Large Language Model** (the picker), **Status** (version / setup / timezone / GPU), **Chat** (a *Status* subsection — Open WebUI version + container state, and the Lemonseed configuration row — then a *Controls* subsection with *Apply Lemonseed settings now* / *Reset chat settings* / *Refresh status*), **API** (collapsed "System API Data"; the intended home for the OpenAI-compatible endpoint toggle in §11). The rule for anything added here: **show only what a home admin can act on** — developer-facing detail belongs in build artifacts (§11). Last panel: **People** (M7 P3/P4) — one row per account (display name, username, `disabled` / `must choose a new password`, address) with *Name*, *Email*, *Enable/Disable*, *Reset password* and a typed-confirmation *Delete*, plus a chip per **custom** group that writes membership on click (the two system groups are deliberately not chip-assignable, so `admin` is granted by the add-account form's checkbox and there is **no UI to demote an existing admin** — the API path exists, and the last-admin guard is what protects it, not a hidden button); one row per group with its member count and a typed-confirmation *Delete* (system groups are labelled and have none); an add-account and an add-group form; and a *Load log* button that fetches the audit trail on demand. Assigning `admin` happens through the chip row, so the last-admin guard is reached through the same write path as everything else. There is **no** control that writes a group attribute — the routes exist, the widget does not (backlog). It speaks only the routes above (`GET /api/users`, `POST /api/users/<action>`, `POST /api/groups/<action>`, `GET /api/audit`), so the panel and the API cannot disagree about what is possible. Non-admins never see this document at all: `/settings/` is admin-gated, and their own surface is `/profile/`, reachable from the account menu on every signed-in page.

### 3.3 Identity, sessions and access control

**As built:**

- **Sign-in is SRP-6a — the password never crosses the wire** (M7 P2; decision `D1` and
  `P2` in [`proposals/M7/PLAN.md`](./proposals/M7/PLAN.md)). Two CP routes, both open at
  the gateway: `POST /_lemonseed/api/srp/challenge {username} → {cid, salt, B}` and
  `POST /_lemonseed/api/srp/verify {cid, A, M1} → {M2, target}` plus the session cookie.
  `control-plane/srp.py` is pure math (`pow`/`hmac`/`hashlib`, stdlib only) over the
  **fixed RFC 5054 2048-bit group** — never generated, always the published constant —
  with SHA-256: `x = H(s | H(I | ":" | P))`, `u = H(A | B)`, `M1 = H(A | B | S)`,
  `M2 = H(A | M1 | K)` (RFC 2945 §2.4). The handshake rejects `B % N == 0` and `u == 0`
  before deriving anything, because both are small-subgroup attacks.
- **Pending handshakes are in-process, single-use, and expire in 120 s**
  (`srp.PendingStore`). Single-use is what makes a replayed `M1` fail, and `b` is never
  persisted. `verify` takes its identity from the *pending record*, never from the request
  body, and re-reads the row: if `srp_verifier` changed between the two moves the handshake
  is refused. `M2` is mutual authentication and is asserted on the wire in QEMU — if the
  server cannot produce the `M2` the client derived, it proved nothing.
- **An unknown username is not an enumeration oracle** (RFC 5054 §2.5.1.3). The challenge
  is built on a **simulated** path whose salt is `srp.fold_salt(fold_secret(), username)`
  — an HMAC under the session-signing secret as a labelled subkey — so the response is
  indistinguishable in shape and cost, and `verify` still performs the full modular
  exponentiation before rejecting. Neither presence nor absence is a tell.
- **Enrollment is client-side.** The browser derives `salt` and `v = g^x mod N` and posts
  only those (`stub.SRP_ENROLL_JS`; the same math in `complete_setup`). The server
  therefore **never sees a password** and cannot enforce a password policy — the minimum-8
  rule is `SRP_ENROLL_JS`'s, which is honest but weaker than a server rule. This also means
  **bcrypt is gone, not narrowed**: `auth.verify_credentials` and `auth.hash_password` are
  deleted, `POST /login` answers **405** (`Allow: GET`, "Signing in requires JavaScript."),
  and a pre-M7 row has no verifier and cannot be given one (bcrypt is one-way — you cannot
  derive an SRP verifier from a hash). **Every existing test machine needs a fresh
  reinstall.** M7 P3 then stopped pretending that column might earn its keep:
  **`users.password_hash` no longer exists** — there is no password column in the CP schema
  at all, because the server is not permitted to hold a value it must never see. The
  reasoning and the price are the Accounts bullet below.
- **The client JS carries its own SHA-256** (`stub.SRP_CLIENT_JS`, a module-level constant
  beside `PORTAL_FETCH_JS` / `OWUI_CONFIG_JS` — no new static file, no new nginx location).
  `window.crypto.subtle` is `undefined` on the portal, so there is **one** code path rather
  than a fallback ladder, and the file self-checks against hardcoded known-answer vectors
  at load and **refuses to submit** if any check fails. Failure direction is closed: a bad
  digest cannot silently produce a wrong-but-submittable proof.
- **What SRP does *not* buy** (do not oversell it): it stops passive sniffing and password
  reuse across sites, but the session cookie is still a bearer token on plaintext HTTP, and
  an **active** on-path attacker can rewrite the login page and read the password out of the
  form. Beating that requires TLS or pinning the page, both of which are out of scope; the
  LAN threat model is documented in §9.
- **Sign-in rate limiting lives in Valkey, not in the process** (`auth.py`). Failure and
  lockout counts are TTL keys (`lemonseed:rl:f:<ip>`, `lemonseed:rl:l:<ip>`, `INCR` +
  `EXPIRE` in one pipeline) so a CP restart no longer clears a lockout — which is the whole
  point, since the old process dicts were wiped by every deploy. They are deliberately
  **not** boot-scoped the way session keys are. Plain dicts mirror the same state as a
  fallback, and the **failure direction is allow**: if Valkey is unreachable the gate opens
  rather than locking a household out of their own box. That is the inverse of the session
  store's fail-closed rule, and it is intentional — a store outage must not become a
  lockout, and the accepted cost is that online password guessing is unthrottled while the
  data plane is down. That is a real residual risk, not a formality; it is bounded only by
  SRP making each guess a full handshake rather than a cheap POST.
- **One login path:** the nginx `auth_request` gate (`auth.GATE_SNIPPET`, CP-written to `/run/lemonseed/auth-gate.conf` after the first admin exists). Session cookie `lemonseed_session` (`Path=/; HttpOnly; SameSite=Lax`, deliberately **no** `Secure` — the appliance is HTTP-only on the LAN and self-signed TLS was worse UX than none) carries an HMAC-SHA256 token that also embeds a session id (`sid`) and the account's session generation (`gen`). The token alone is not authority. **The snippet survives the reboot:** `/run` is tmpfs and the boot seeder used to re-create it empty, so the edge was briefly open — and an admin's dead-cookie request reached the CP with no forwarded groups and got a bare `needs-admin` JSON. The CP now also writes a persistent copy under `/var/lib/lemonseed/` that the seeder restores before nginx starts (D-47).
- **Session state lives in Valkey** (`control-plane/sessions.py`), keyed by boot id and `sid`. **Sliding** idle window `SESSION_INACTIVITY_SECONDS = 3600`, refreshed on every verify, clipped by the absolute ceiling `SESSION_ABSOLUTE_SECONDS = 36000`. Keying by `/proc/sys/kernel/random/boot_id` means a reboot drops all sessions — `valkey-server --save 60 1` would otherwise resurrect them from an RDB snapshot. Retaining sessions across a reboot would be nicer UX but is not a requirement, and persistence would cost more than it buys.
- **Revocation works.** `users.token_generation` (schema v4) is embedded in the token and compared on every verify, so disable, demote, delete and logout-all land on the affected account's next request; `auth.invalidate_sessions()` is the single bump point. Household scale makes the extra Postgres read cheap — no cache layer.
- **Session verify** (`GET /_lemonseed/session-verify`) needs three things to agree: the signature, a live Valkey record, and the generation in Postgres. It answers `200` plus `X-Forwarded-{User,Email,Groups,Name,Role}`, which nginx reads with `auth_request_set`. Those headers are only ever set by nginx from the verify response, so client-supplied identity is always dropped. Because the CP now depends on a container it also starts (Valkey, `nixos/modules/data-services.nix`), the gate **fails closed**: store down ⇒ `503` JSON ⇒ the branded not-ready page, never an authenticated surface. A down data plane is a broken appliance; an unauthenticated one is not acceptable.
- **Accounts** live in Postgres: `users(username, display_name, email, disabled, created_at, token_generation, last_login, srp_salt, srp_verifier, password_reset_required, owui_id)`, `groups(name, created_at, system)`, `user_groups` many:many with cascade, `group_attributes(group_name, key, value JSONB)`, and `audit_log`. **There is no password column.** Schema is at **v6** over `BASELINE_SCHEMA_VERSION = 5`: M7 P3 **folded the shipped v4/v5 steps into the baseline and deleted them** (`db.py` D-29), because there is no install base older than M7 and a backfill is permanent debt — so **there is no backfill and no legacy-repair code anywhere in the tree, deliberately** (D-31). The price is stated rather than hidden: `db.migrate_cp_schema` **raises** for any recorded version below the baseline — "this build cannot upgrade it — reinstall the appliance" — instead of falling through and quietly producing a database missing columns; `provision_data_plane` turns that raise into a `{"state": "error"}` report, so the box says reinstall rather than booting half-shaped. Dropping the column was only possible *because* of that rewrite: migrations are additive-only (`test_only_additive_migration_sql` is the guard), so a column can only vanish by rewriting the baseline, and rewriting the baseline is free only while nobody must be upgraded. Roles are **derived** from group membership (`auth.role()`, admin iff member of `admin`); there is no stored role column. Bootstrap is a single `/setup` path, open only while the `admin` group is empty, and it takes a salt + verifier instead of a password; `create_admin`'s upsert **overwrites both SRP columns**, so a credential reset can never leave the old password working. The two system groups (`admin`, `users`) are seeded by the **write layer** (`auth.ensure_system_groups`, which re-asserts `system = TRUE` on every call so a hand-edited row converges without a backfill pass), never by migration SQL — the additive-only guard demands `IF NOT EXISTS` on every non-`ALTER` statement, which an `INSERT … ON CONFLICT` cannot satisfy.
- **`usermgmt.py` is the only code allowed to write `users`** (D-32), and that exclusivity is the design: every primitive is validate → guard → CP transaction → Open WebUI → audit row → notify, so "an account nobody can sign in to" is structurally impossible rather than merely unlikely. Each one is `create`, `set_display_name`, `set_email`, `set_disabled`, `set_groups`, `rotate_password`, `admin_reset_password`, `delete`, `record_login`, plus the group primitives. Guards run **inside** the transaction (D-2), because a UI that hides a button is not a guard — a direct API call must hit the same wall. That includes the last-enabled-admin guardrail (refusing to disable or demote the only enabled admin, and refusing self-disable outright) and the reserved-identity rules of D-22. After each commit the primitives fire events to an in-process observer list (`register_observer`), which is the whole extension point for a module that must keep its own data in step — no plugin framework. `users.disabled` has always been honored when *authenticating* (`auth.get_user` rejects it, group joins add `AND u.disabled = FALSE`); what M7 P3 added is the **write path**, and disabling bumps `token_generation` in the same transaction so a stopped account cannot keep a live session.
- **Two identity keys, never the same string** (D-16′). `users.email` is the household's own address, editable by the account itself via `/_lemonseed/api/profile`, and it is the **portal's only view** of it. Open WebUI's identity is **always** the derived `{username}@localhost` from `auth.owui_email()` — never stored, never mirrored, never read from the row: `create_session` puts the derived value in the session record, so `X-Forwarded-Email` cannot be a real address even by accident. Because the username is immutable (D-4), the derived string cannot move, which is what makes the duplicate-`admin@localhost` bug of D-15 *structurally* impossible instead of merely unlikely. `users.owui_id` records the Open WebUI account id, unique **only where non-NULL** (a partial index), because a row whose OWUI account has not been created yet legitimately has none and two rows must never claim one identity (D-16a).
- **`audit.py` records admin-class actions** to the `audit_log` table *and* a journald line. Postgres is the record of truth because the consumer is a query — "who did what to this account, when", joinable against accounts — while the journal copy is the ~2 lines that survive a wiped database and need no control plane to read. `detail` carries **names of things changed, never values**, masked through the same policy as the config-drift report (D-37): an audit trail that faithfully records a pasted API key is worse than no audit trail, so no password, verifier, salt, OTP, token or key belongs in it, even masked. `actor` comes from `X-Forwarded-User` — nginx-set from the session — never from the request body, which is what makes "who did this" unforgeable; `system` and `cp` are the two non-session actors. `record()` never raises (a failed audit write must not undo the change the operator just made) but logs `COULD NOT RECORD` loudly, because a hole in the trail is a truth problem. Retention is `AUDIT_RETENTION_DAYS = 365` swept by application SQL, not a migration (`test_only_additive_migration_sql` bans `DELETE` from migration statements), from `audit.maybe_sweep()` on the data-plane supervisor tick at most every six hours — an unreachable database does not consume the slot, or a box that boots before Postgres would wait six hours for a sweep that never ran.
- **Sign-in data stays off the list endpoint.** `GET /_lemonseed/api/users` returns accounts, groups, the `first_admin` identity (D-22 — the portal learns which row it may never delete from the appliance rather than by assuming the username `admin`) and the known attribute keys, and it deliberately omits `srp_salt`/`srp_verifier`: a list endpoint is exactly where a verifier must never appear, because it is the one response a browser caches, logs and screenshots.
- **An account somebody else created cannot hold a session until it has rotated** (D-11). `usermgmt.create` and `admin_reset_password` both set `password_reset_required`; `srp/verify` then answers `{M2, rotate_required: true}` with **no `Set-Cookie`**, and the login page runs a fresh challenge into `srp/rotate` before it has anything to navigate to. The alternative — issue a session and gate it on the flag — needs the portal, every CP API **and Open WebUI** to honour the flag, and any surface that forgets becomes a way to sit in a one-time account forever. So the rule is one rule: **an unrotated credential never produces a session at all.** Why the flag exists at all is D-13: the admin's browser derived that verifier, so the admin knows the credential and could sign in as that person and read their conversations. Forced rotation bounds the window and does not remove the capability. **Open item:** D-13 also asks for that consequence to be *said* when the admin generates a code; the panel promises the forced rotation but does not yet spell out that the admin could use the code themselves. Copy is deliberately minimal, so the wording is the admin's call. `auth.create_admin` deliberately does the opposite (`password_reset_required = FALSE`): there the household typed its own password, so nobody else holds it.
- **A non-admin has exactly one surface, and it is not Settings.** `/settings/` is admin-gated, so every signed-in page carries an account popover (`stub.ACCOUNT_JS`) that shows who you are, hides the gear for members, and holds *Log out* and *Edit Profile*; **My Profile** at `/profile/` edits your own display name and address and changes your own password. Both screens call the same `stub.ROTATE_JS` helper, so the People panel and the profile page cannot drift into two different rotation sequences. Rows are built with `textContent` and never `innerHTML`, and a one-time code is generated with `crypto.getRandomValues` over an unambiguous 31-character alphabet (`ACCOUNTS_JS.OTP_ALPHABET`, no `i`/`l`/`1`/`o`/`0`), shown once.
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

**The manifest is the interface.** `control-plane/owui-policy.json` declares the whole CP↔OWUI contract in data, with six top-level blocks: `version`, `owui` (minimum validated version), `settings`, `entities`, `guards`, `api`. Today: **24 setting entries**, two entity collections, **7 guards (all 7 `enforce`)**, a **17-entry** API name→route map. M7 P4 **deleted** six `groups.*` entries (`groups.list`, `groups.create`, `groups.update`, `groups.users_add`, `groups.users_remove`, `groups.delete`) that `owui_policy.py` never referenced: the map is also the CP API key's endpoint allow-list, so an entry nothing calls is a permission nothing needs. The CP owns groups; the manifest must not imply it manages them through Open WebUI's API. `owui_policy.py` validates it strictly at load (`ManifestError` on anything unknown) so the manifest cannot quietly rot.

**Ownership modes** (how much we own):

| Kind | Mode | Semantics |
|------|------|-----------|
| Scalar | `initial_setup` | Delivered as env at first boot / backfilled once if the row is missing; user GUI edits then stick |
| Scalar | `managed` | We own the value; re-asserted on cadence; GUI edits get reverted |
| Collection | `selective_management` | We own *our* entities by ownership key (models: CP-defined id); everything else untouched; entities claiming our reserved name namespace are swept, with a portal log entry. One declared per-kind exception: the `models` kind carries `ensure_public`, an access assertion on foreign entries (below). |
| Collection | `full_management` | We own the intent **within the collection's declared scope**: create missing, fix variation, delete extras |

**Cadence** bounds re-assertion so the reconciler never stomps user intent on a timer: `at_boot` (every time an OWUI slot goes healthy — security/egress/identity/wiring), `on_upgrade` (manifest version bump), `manual` (explicit portal action). Every entry carries a human-readable `reason`, which is what makes the portal's ownership view and a guard's denial message meaningful.

**Entities as built:**
- `models` — `selective_management`, owned by **CP-defined id** (`lemonseed-ai`, `lemonseed-ai-filtered`), reserved display-name namespace `^Lemonseed.*$` (case-insensitive). v0.11.0 `ModelForm.id` is a client-supplied string stored verbatim, so *we* write the ids and they cannot churn under an upgrade; a wiped DB makes our models *missing*, and the reconciler re-creates them under the same ids. A UI-created model that claims a reserved name is removed by the reconcile namespace sweep — path-only nginx guards cannot see request bodies. Definitions land with the LiteLLM phase. **Visibility is asserted, not owned** (D-44, bare-metal review): OWUI's model editor defaults a new entry to owner-only access, which hid the chat model from every non-admin, and model choice is one global, admin-managed setting. The reconciler therefore checks every model entry's `access_grants` (v0.11.0: empty = owner-only) and writes exactly one wildcard read grant (`user:*:read`) through `POST /api/v1/models/model/access/update` — an access-only write that never touches `params`/`meta` and backfills the minimal entry a non-preset model may not have yet. Sweep targets are skipped (they are deleted anyway), and an audit-only tick reports the drift without writing.
- `users` — `full_management` with scope **`["membership", "is_admin", "name"]`**: the loop creates, promotes, demotes and renames but **never deletes** accounts. An explicit member-delete in Lemonseed propagates an immediate OWUI delete through `usermgmt.delete` (account + chats wiped — the delete-button semantics right-to-erasure regimes want), which is why OWUI's user panel can no longer be the place accounts disappear. `%SERVICE_ACCOUNT_ID%` is in `never_touch`. The mirror's intent comes from `auth.owui_identities()`, which emits the **derived** `owui_email(username)` string and never `users.email` (D-16′), and it keys on `users.owui_id` wherever the CP holds one; the id itself is captured by `usermgmt` when it creates the OWUI account (`modules.owui_admin_client()` is what gives the account primitives a keyed OWUI client outside the reconcile loop).

**Guards** (`advertise` = show as Lemonseed-owned in the portal; `enforce` = refuse the request at nginx). The CP renders `/run/lemonseed/owui-guards.conf` from the manifest (atomic; seeded empty, so a boot where the CP never ran blocks nothing while the reconcile loop still enforces intent). Only the OWUI catch-all location carries guards, so "which requests are guarded?" is one line to read. Enforced today:

| Guard | Why |
|-------|-----|
| `POST /api/v1/configs/connections` | writes `direct.enable` (direct model endpoints), which stays off |
| `POST /openai/config/update` | OWUI's connection editor rewrites the **whole** OpenAI connection set, including our Lemonade entry |
| `POST /ollama/config/update` | there is no Ollama on the appliance |
| `POST /api/v1/users/[^/]+/update` | the one route that can change an e-mail, a role or a password, so one rule closes all three edits in OWUI's user panel. The id segment is `[^/]+` and **not** `.*`: greedy `.*` would also swallow Open WebUI's own self-service `users/user/settings/update`, `users/user/status/update` and `users/user/info/update` routes, which Open WebUI still owns |
| `DELETE /api/v1/users/.*` | household accounts are removed in Lemonseed. Flipped from `advertise` once user-list mirroring existed: an account OWUI deleted on its own orphans the CP row, and a later account could inherit it |
| `DELETE /api/v1/users/%CP_OWUI_ID%` | the service account is not removable from OWUI |
| `POST /api/v1/auths/add` | household accounts are created in Lemonseed. The last one to flip, once the portal's People panel existed: an account made in OWUI has no CP row, no SRP credential and nothing to sign in with. Bootstrapping is untouched — a brand-new OWUI database seeds its first user on the `signup` route, a different path |

**Nothing is advisory any more** (M7 P3/P4): the Open WebUI user panel is read-only end to end, which is the point of D-17b — the CP owns accounts, and a panel that can create, edit or delete an account the appliance does not know about is drift waiting to be inherited by the next person to hold that username. `test_owui_policy.py` pins the split in both directions, so flipping a guard either way fails a test first.

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
| `release` | `build-production` (SIGN=1) + `build-installer` + pack + sign + self-verify → `out/release-<version>/` |
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

Distribution is the sharded release set below; the appliance build layout stays the same either way. The channel is **GitHub releases** (M8 P3): discovery is one `GET /releases` call —
`stable` = newest non-draft non-prerelease, `beta` = newest non-draft release — and the
release's asset base (`releases/download/<tag>/`) feeds the exact same signed-manifest
and sharded-payload machinery a `--url` set does. The channel is *only* GitHub's
prerelease flag (D-C1): the signed manifest describes the payload, never the audience,
so a beta → stable flip is `gh release edit <tag> --prerelease=false` — no rebuild, no
re-sign. Switching channels never downgrades (D-C2): the channel filters candidates;
the version comparison decides. A stable box with only prereleases published is *up to
date*, the same as a candidate no newer than the running release. Rate limits (D26):
one API call per check, 60/hour per originating IP, assets ride the CDN — we never
poll. The on-box channel comes from `--channel` / the settings table (materialized to
`/var/lib/lemonseed/channel`, written by the CP's settings API and seeded by the
installer) / the installer's ESP stub / stable, in that order.

`make dev-httpd-start` / `-stop` (D-C3) serve `./out` over HTTP from a pinned busybox
container for local `--url` testing — static files only, bound to the primary
interface's own address, high port 8083. As built (M8 P2), a release **is** a sharded asset set: `make release` stages `out/release-<version>/` containing the signed `manifest.json`, the payload cut into fixed **1 GiB** `payload-NNN.part` parts (a canonical pax tar of `vmlinuz`, `initrd.img` and `root.squashfs`; the final part is a remainder), and the USB installer ISO as its own direct asset recorded in the manifest. The tar is **not gzipped**: the SquashFS is already zstd-compressed and dominates the payload, so a second compression layer costs assemble-time CPU for no size win. The manifest is both the assembly plan (chunk names, order, hashes) and the root of trust (output names, lengths, SHA-256); consumers verify the signature, then each part, then the concatenated tar, then re-verify every extracted file before the A/B slot flips. Design and decisions: [`proposals/M8/PACKAGING.md`](./proposals/M8/PACKAGING.md).

### Signing
- **As built (M8 P1):** the root of trust is a **detached ed25519 signature over
  `manifest.json` alone** (D12) — every artifact, part and installer-image hash lives
  inside the signed bytes, so one signature covers the whole release. `scripts/sign.sh`
  signs only the manifest and records
  `"signing": {"scheme": "detached-ed25519", "enabled": true, "public_key": …}` inside
  the signed metadata; `SIGN=1 make build-*` signs, unsigned builds keep `enabled: false`
  and are refused by every production consumer. `SIGN_MODE` is the backend seam
  (`file` today; PKCS#11/hardware tap-to-sign reserved, D15) — the interface never
  changes.
- **The verifier is one module, two callers:** `scripts/lemonseed_verify.py`, imported by
  `scripts/lemonseed_update.py` (`check`/`apply`) and the installer's vendored pipeline
  (`installer/install.py`). Verification is **verify-before-parse**: the detached
  signature is checked against the baked trust set *before* the manifest is parsed, so
  its SHA-256 values become trustworthy only after the root of trust holds. A missing,
  empty, malformed, wrong-key or altered-manifest signature is a hard refusal with a
  legible reason — never a warning, never a hash-only fallback (D10). P3's `--dev` will
  be the only bypass, and it is CLI-only. The backend is a subprocess
  `openssl pkeyutl -verify -rawin` — the signer's primitive, so the seam can carry
  PKCS#11 verification later without touching a caller (`openssl` the binary is now in
  the appliance, installer and updater closures).
- **The trust set ships in the image (D13):** a baked `release-keys.json` — the
  authority, carrying a monotonically increasing set `version` and an explicit key list
  with a per-key `algorithm` — plus the key files it lists, at
  `/usr/share/lemonseed/release-keys/` (via `nixos/packages/release-keys.nix`, copied
  explicitly: no globs anywhere). The repo stores the same pair
  (`keys/release-keys.json` + tracked `release-v*.pub`) and the same file pair is
  published on lemonseed.ai for out-of-band human comparison, never fetched at runtime.
  An empty, malformed or keyless trust set refuses — fail-closed even to a broken build.
- **Key ceremony:** one long-lived ed25519 keypair generated on the dedicated
  CI/build/signing box (`scripts/gen-keys.sh --name release-v1`); the private key never
  leaves the box and is escrowed privately (details deliberately not in the repo). The
  public key is committed and published. Rotation is **designed, not exercised** (D13):
  a transition image signed by the old key embeds the new key; the verifier accepts any
  key in the baked set during the overlap window.
- **GH-compromise posture:** an attacker with GitHub write access can publish releases
  and flip channel labels, but cannot produce a valid manifest signature. Worst case is
  selecting among *our* signed releases (downgrade/upgrade confusion), never arbitrary
  code. A compromised build box, however, can sign anything — that is mitigated by the
  box being dedicated and the key escrowed, not by this design.
- **QEMU validation:** `make test-qemu` runs a signing smoke (`lemonseed-signing-smoke`
  in the QEMU profile, against the tracked QEMU-only trust set in `keys/qemu/`) that
  verifies a correctly signed set, refuses a flipped payload byte at the hash check,
  and refuses an altered manifest at the signature — asserted on the serial log.
  `scripts/test-data/signing/` pins the openssl invocation with a committed
  known-answer vector.

### QEMU validation
- `make run-qemu` boots kernel + SquashFS + serial console.
- `make test-qemu` boots the appliance-qemu profile and probes `/healthz`, `/api/status`, `/api/system`, `/api/boot`, `/api/data`, `/api/modules`, the shared theme stylesheet, the session gate (including that an unauthenticated **XHR** gets a real 401 rather than a 302) and the OWUI guards. The profile pre-pulls the pinned Postgres/Valkey images and serves Open WebUI from a host virtio-fs export (`nixos/modules/qemu-docker-cache.nix`) so smoke tests do not depend on registry pulls inside the guest. Opt-in e2e: `QEMU_E2E_CHAT=1` (pull + load + assert chat ready), `QEMU_E2E_GC=1` (disk-space eviction).
- Full A/B disk and GPU validation happens on **real hardware**; QEMU has no GPU.

---

## 9. Security posture (as built)

| Layer | Approach |
|-------|----------|
| Transport / update authenticity | HTTP + **detached ed25519 over `manifest.json`**, verified fail-closed against the baked trust set before any hash is trusted (M8 P1); TLS deliberately absent on the LAN |
| Root filesystem | Read-only SquashFS; no overlay |
| Mutable state | Minimal btrfs binds; restrictive mount flags |
| Edge auth | **CP-native login** at the nginx boundary: signed stateless session cookie, `auth_request` → CP, identity via `X-Forwarded-*` set **only** from the verify response. Modules stay auth-naive; OWUI's own login is disabled |
| Credential on the wire | **SRP-6a** (§3.3), because the appliance is HTTP-only forever: the password is never transmitted, only proof of knowing it. Closes passive sniffing and credential reuse. **Does not** close an active on-path attacker who rewrites the login page — that needs TLS, which is deliberately out of scope, so anyone claiming this makes the LAN trusted is overselling it. The bearer cookie is likewise still a bearer cookie over HTTP: SRP protects the credential, not the session |
| Password policy | **Client-side only, and that is a real weakening.** Enrollment derives the verifier in the browser, so the server never holds the password and cannot apply a rule to it: the minimum-8 check belongs to `stub.SRP_ENROLL_JS` and its rotation twins, and a client that skips the JS can pick any verifier it likes. A server-side policy would require seeing the password, which is the exact property SRP was adopted to give up. There is no server-side length, complexity or reuse check, and there cannot be one under this design |
| Account recovery | A forgotten **admin** password is recovered by USB reinstall with preserve-user-data + **Reset admin credentials** checked (D-20): the installer writes `/var/lib/lemonseed/reset-admin` **into the target's user-data** (D-27 — the ESP handoff never worked; see `proposals/M6/OPEN-WEBUI-UX.md`), the CP consumes it at boot, `/setup` reopens, and every former admin row is overwritten or deleted rather than left able to sign in (D-20a). No in-portal admin recovery exists or will be built: anything that can set a verifier without the old password is reachable by the whole LAN. A **user** password reset is an admin action (`usermgmt.admin_reset_password`, D-10): the admin's **browser** generates the one-time password and posts only its verifier, so the OTP never crosses the network; the target gets `password_reset_required`, and `srp/verify` answers `M2` with **no session cookie** until they rotate (D-11) — an unrotated credential cannot produce a session at all, for the portal, the APIs or Open WebUI. The accepted cost, stated plainly (D-13): the admin knows the OTP and can sign in as that user until they rotate |
| Audit trail | Append-only `audit_log` in Postgres plus a journald line per event (§3.3); actor taken from the session, detail masked and value-free, 365-day retention swept from the data-plane supervisor tick |
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

Moved to [BACKLOG](BACKLOG.md)

---

## 12. Open questions / deferred decisions

1. ~~**Initrd:**~~ soft goal of none; still shipping one.
2. ~~**Exact GPT type UUIDs**~~ for A/B SquashFS — linux-generic in use for bring-up.
3. ~~**`/var` mount flags**~~ — validate empirically under Docker/Lemonade.
4. ~~**Planned reboot vs `boot_once`:** **answered.**~~ Only GRUB writes `boot_attempted`, so an ordinary reboot silently completes an armed one-shot upgrade. The portal therefore never issues a bare reboot while an update is staged: it offers **"Reboot into the new version X"** (default/highlighted) versus **"Reboot without updating"**, so the two intents stay distinct (M7 P5). *(Superseded during M7 P5 as built — D4: the update flow reboots by itself, so the choice dialog is gone.)*
5. ~~**Graceful data-plane shutdown:** **resolved empirically**~~ (2026-10-11): `shutdown -h`, `shutdown -r` and the ACPI power button all shut the current image down and bring Postgres back clean. An explicit ordered-stop assertion in QEMU is still worth adding alongside the M7 P5 power controls, but it is no longer an open correctness question.
6. ~~**Session revocation mechanism:** **answered**~~ — a per-user **generation counter in Postgres** that `session-verify` compares against the token, not a per-box secret rotation. Cheap per event, no global logout on a single revoke, and it answers "who is still signed in" as a side effect (M6.6).
7. ~~**Portal XHR error contract:** **answered**~~ — one rule for every portal request: **401** redirects `window.top` to `/login?rd=…`, **403** is surfaced as "signed in, but not permitted" and never redirected, **5xx** is retried with backoff. See §3.3 and [`proposals/M6/SESSION-AND-ERROR-CONTRACT-M66.md`](./proposals/M6/SESSION-AND-ERROR-CONTRACT-M66.md).
8. **`lemond` packaging:** from-source Nix today; whether to flake-input `nix-amd-ai` later for AMD only, or vendor a slim copy.
9. **Heterogeneous and Multi-GPU planner:** inventory-ready schema now; scheduling later.
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
| GPU | Drivers in base; NVIDIA CUDA + AMD Vulkan (Mesa RADV); single image covers both; GPU modules load only when the hardware is present — a udev modalias rule (`services.udev.extraRules`, PCI class 03, vendor 1002/10de) fires `modprobe` on device add, so AMD-only boxes never load the NVIDIA stack (and vice versa; udev's built-in coldplug missed amdgpu on an updated image, and a manual `modprobe` bound instantly — hence the explicit rule); no VRAM pooling; ROCm/NPU deferred. NVIDIA validated on hardware; AMD **Strix Halo APU validated on hardware**; AMD **dGPU (PCIe) implemented but never booted** on the appliance |
| Inference UX | Open WebUI primary; catalog-selected LLM via `lemond` |
| Models | Stored under `/var/lib/lemonseed/`; versioned catalog fit; LRU disk-space maintenance; `auto_check_model_updates` off |
| llama.cpp release | **Build from source at image-build time, pinned to a tagged upstream release** (never HEAD). One build per backend (`llamacpp-cpu`/`-cuda`/`-vulkan`) that lemond is pointed at; backend choice is the CP's (`select_backend`), not lemond's. CUDA is our own source build; CPU/Vulkan track `nix-amd-ai`. |
| Lemonade packaging | From-source Nix; wire Nix backends; patterns from nix-amd-ai |
| Control plane | systemd root; Python stdlib; privileged API + web UI in one process; **CP state in Postgres** |
| CP API namespacing | CP APIs live under `/_lemonseed/api/*`, not `/api/*`. Open WebUI's frontend calls `GET /api/models` for its OpenAI-style list, and an exact-match CP location was silently winning and returning the picker payload to OWUI. `/_lemonseed/` is the reserved internal namespace. |
| Builds | Local; flakes; `build-production` / `build-qemu` / `run-qemu` / `qemu-clean` |
| Version | Date + local counter; `VERSION` override |
| Partition config | YAML source (`partitions.yaml`) → `gen-repart.py` → systemd-repart as apply engine |
| Signing | **M8 P1 (as built):** detached ed25519 over `manifest.json` alone (D12); verifier `scripts/lemonseed_verify.py` shared by the updater and the installer, fail-closed, verify-before-parse; trust set = baked `release-keys.json` + listed keys, no globs (D13); openssl subprocess backend, modular for PKCS#11 (D15); `--dev` (P3) the only bypass. Rotation designed, not exercised. |
| Distribution | HTTP; versioned artifact directory |
| Installer | M4; partitions + optional admin lockout reset marker; no auth work for fresh installs; reinstall releases LVM/DM |
| Encryption | Deferred; TPM + GRUB password planned together with closing the bring-up root login |
| Open WebUI / auth | **M6.1:** OWUI auth off. **M6.2:** edge auth only at the nginx boundary — modules stay auth-naive (no per-container OAuth) |
| Edge auth (M6.2) | **CP-native login** (signed HMAC-SHA256 session cookie, branded page). Replaces Authelia (whose hardcoded `https` requirements were unfixable on an HTTP-only LAN). nginx `auth_request` → CP `/_lemonseed/session-verify`; identity headers set **only** from that response. Cookie: `Path=/; HttpOnly; SameSite=Lax`, no `Secure` by design. State in Valkey: **sliding** 1 h idle window under a 10 h absolute ceiling, keyed by boot id; `users.token_generation` gives real revocation; the gate **fails closed** if the store is down. |
| Admin gating | Enforced in the **CP** from `X-Forwarded-Groups`; the nginx admin include is intentionally empty because nginx `if` runs before `auth_request` sets its variables |
| CP authority | **CP + Postgres is the source of truth** (accounts = standard **users + groups**, many:many, Linux-style; elevation = `admin` group, no singular owner); config files / env / secrets are derived and regenerated |
| Admin bootstrap | **Single `/setup` path** (synchronous; name + timezone + admin creds) — first admin only while the `admin` group is empty; lockout = preserve-reinstall + “Reset admin credentials” marker (physical access) clears the `admin` group so `/setup` reopens. M7 P3 corrected **where** that marker is written: the installer writes `@var/lib/lemonseed/reset-admin` **into the target's user-data filesystem**, which it already has mounted at `/run/lemonseed-install-user-data`; the ESP handoff described in the M6 record never worked and is deleted (D-27, see `proposals/M6/OPEN-WEBUI-UX.md`). The reset also **deletes** former admins whose username changed, which an upsert alone would leave able to sign in (D-20a), and the CP publishes `admins.json` so the installer can name the accounts it is about to reset rather than asking for consent in the abstract (D-28). [2026-10, M7 P3] |
| OWUI config authority (M6.3b) | **DB after first boot.** OWUI's Postgres `config` table is authoritative; env = bootstrap-only (the import-time set in §3.5 plus a CP env-generation marker). The CP reconciles via OWUI's admin API from a versioned manifest (`control-plane/owui-policy.json`) declaring per-key mode + cadence + `reason`, so behavior stays legible outside Python. |
| OWUI interaction modes | Two orthogonal axes: **mode** per entity kind (`initial_setup`, `managed`, and for collections `selective_management` / `full_management`) and **cadence** (`at_boot` / `on_upgrade` / `manual`) bounding re-assertion. `initial_setup` must mean "delivered as env at first boot", because v0.11.0 seeds *every* known key into the table at first boot. |
| OWUI entity ownership | By **CP-defined id**, namespaced by a reserved display name (`^Lemonseed.*$`), collision-swept at reconcile. Chosen over UUID-based ownership because v0.11.0 `ModelForm.id` is a client-supplied string we write, so ids cannot churn across upgrades/reinstalls; a wiped DB makes our entities *missing* and the reconciler re-creates them under the same ids. |
| OWUI users collection | Managed as **membership + `is_admin` + `name`** (M7 P3 widened the scope): the loop creates, promotes, demotes and renames but **never deletes** accounts; an explicit CP member-delete propagates an immediate OWUI delete (account + chats wiped). Deleting users as routine reconciliation is too heavyweight — users own their own settings, including per-user API keys. Since M7 P3 the mirror is keyed by **`users.owui_id`**, not email, so a failed push is drift that the next reconciliation repairs rather than a duplicate account (D-18); `owui_id` is captured at create and there is deliberately **no** backfill for rows created before it existed (D-31). |
| OWUI service account | The CP authenticates to OWUI as a built-in super admin (`lemonseed-cp`, display name **“Lemonseed Control Plane”**, `@localhost` email) over trusted headers **direct to the slot ports**, plus an endpoint-restricted `sk-` key derived from the manifest's api map. OWUI cannot hide users from the admin list, so protection = unmistakable name + id-targeted guards + OWUI primary-admin immunity (the CP account must be OWUI's first user). |
| Synthetic identity emails | One domain, `@localhost` — the only no-dot domain OWUI's `validate_email_format` exempts, so its admin UI can manage such rows (`@lemonseed` is unwriteable; D-19). **M7 P3 (D-16′) made the OWUI identity derived, not stored:** it is always `auth.owui_email(username)` = `{username}@localhost`, it is never mirrored from `users.email`, and `users.email` is the household's own address, portal-only. Three identifiers, three jobs — `users.username` (immutable, bound into the SRP hash), the derived OWUI email, and `users.owui_id` (the row key). This is the fix for the duplicate-`admin@localhost` defect (D-15): email-keyed identity meant two accounts could claim the same address, and OWUI duplicates rather than renames. |
| Guard denial shape | A guard's 403 body is **JSON with a non-empty `detail`** and the location sets `default_type application/json`; with `text/plain` Open WebUI discards the reason and shows its own “Server connection failed”. |
| Guard vs login redirect | Accepted: guarded paths answer **403 before** the auth gate can redirect (nginx rewrite phase beats access phase), so an expired admin session on a guarded path sees “managed by Lemonseed” rather than a sign-in prompt. Revisited in M6.6, where the XHR contract gives the client a way to recover; the mechanism is an open decision there. |
| Settings reset | **Admin-only**, explicit confirm, and done as a window: stop OWUI → `TRUNCATE TABLE config` → start → full reconcile → cut back over. Never guard `POST /api/v1/configs/import` — that is the reconciler's own writer. Reset clears OWUI **config** (admin-level settings); user preferences and model settings survive. |
| Portal content rule | The settings page shows **only what a home admin can act on**. Ownership/guard breakdowns, API method tables and other sausage-making moved out of the UI; developer-facing detail should be a build artifact. |
| OWUI guards day one | **M6.3b:** five mutating OWUI paths enforce; `POST /api/v1/auths/add` and `DELETE /api/v1/users/.*` stay `advertise` until the users & groups work (M7, formerly M6.2b) supplies user-list mirroring (enforcing them earlier would leave no way to create a user). The manifest strings that say so are pinned by tests as a tripwire. **M7 P3 (now the shipped state):** six enforce — `DELETE /api/v1/users/.*` **flipped to enforce**, and `POST /api/v1/users/%CP_OWUI_ID%/update` **widened** to `POST /api/v1/users/[^/]+/update` so that one rule covers every identity-or-role edit in OWUI's user panel, not just the CP's own row. Exactly **one** remains `advertise`: `POST /api/v1/auths/add`, because OWUI's account-creation path is still the only way to create a user until the portal's People panel exists. The widened pattern is deliberately `[^/]+` and **not** `.*`: greedy `.*` would also swallow Open WebUI's own self-service `users/user/settings/update`, `users/user/status/update` and `users/user/info/update` routes, which Open WebUI still owns (D-17). **Closed by M7 P3's People panel:** `POST /api/v1/auths/add` is `enforce` too, so all 7 guards enforce and the OWUI user panel is read-only end to end. The reason it was ever advisory was "no way to create a user", and that reason expired rather than being argued away. [2026-10, M7 P3/P4] |
| Session storage | **Sliding sessions in Valkey** (already running), keyed by boot id so a reboot logs everyone out; state is disposable by design, which is what makes it cheap. Revocation via a **per-user generation counter in Postgres**, compared on every verify. Rejected: a per-box secret rotation (revokes everyone to drop one), and a purely stateless absolute-expiry token (cannot revoke, cannot slide). |
| Gate availability | The gate **fails closed** when Valkey is down, even though that means a login outage: the CP starts Valkey itself, so a stateless-token fallback would silently downgrade authentication. A broken-but-authenticated appliance is acceptable; an unauthenticated one is not. |
| Portal XHR errors | **401** → `window.top` to `/login?rd=…`; **403** → surfaced as “signed in, but not permitted”, never a redirect; **5xx** → retry with backoff. Rejected: swallowing 403 silently (dead UI, see the `refreshStatus` string-pinned test that asserted doubly-dead code), and redirecting on 403 (loses the distinction between “not you” and “not yours”). |
| `/api/status` exposure | Split, and shipped: `/api/status` answers pre-login with an explicit **public subset** (installer handoff only) and the full snapshot moved to the session-gated **`/api/system`**. Household facts (e.g. admin user count) had been readable pre-login, and an unauthenticated poll could make the appliance run seven `systemctl` probes plus Docker and lemond queries. |
| One portal fetch helper | **One** `portalFetch()` for every CP call the portal makes, rather than per-caller handling: fifteen hand-written `fetch()` calls each invented their own answer to a dead session, and the one that looked correct was unreachable. Twelve were control-plane calls and now go through the helper; three stay outside it for stated reasons. Retries are capped and **GET-only**. Rejected: swallowing 403 silently (dead UI), redirecting on 403 (loses "not you" vs "not yours"), and retrying POSTs (a lost response would run a reset twice). |
| Gateway error pages | Lemonseed-branded **static** page served by nginx for 500/502/503/504 (`error_page … =503 /_lemonseed/cp-down`, an `internal` location reading the CP's store-sourced static dir). Replaces the `@cp_interstitial` proxy, which could never render: nginx does not run `error_page` inside a location it reached *through* `error_page`, and the fallback proxied to the very process that was down. Self-reloads once `/api/status` answers |
| My Profile vs Settings | Non-admins get a self-service **My Profile** (name, email, own password; shows role and last login) and **never** the Settings/System Control Panel. **Built in M7 P3**, with one deviation from this row's first draft: logout lives in an **account popover on every signed-in page**, not inside My Profile. The reason is that the popover is where *Edit Profile* has to be discoverable, and an item that is only reachable by first navigating somewhere is not an item. Hiding the gear for members is the same popover's job, and it reads `/api/profile` to do it because the CP — not nginx — knows the groups. The routes were already there: `GET`/`POST /_lemonseed/api/profile` act only on the caller's own username taken from the session header, and rotation is `POST /_lemonseed/api/srp/rotate`. [2026-10, M7 P3] |
| Update policy | **Never auto-download and never auto-install.** A weekly check (heavily fuzzed, `sleep $(( $RANDOM * 86400 / 32768 ))`) and a manual “check now” run the same workflow; on success they light a red dot on the settings icon for admins and arm the update button. Rejected: silent auto-update on a household box that may be mid-pull. |
| Shutdown timing | **systemd owns the timeout** for reboot/shutdown; the CP asks and gets out of the way rather than wrapping systemd in another timer script. |
| Destructive-action confirms | Typed "type the word to continue" interstitials are reserved for **data-destroying** actions (settings reset, user delete). Reboot, shutdown and update are data-safe, so they get an explainer plus a confirm — no typing. |
| Release channel | **GitHub releases** since M8 P3: discovery is one `GET /releases` per check; `stable` = newest non-draft non-prerelease, `beta` = newest non-draft release; assets ride the CDN. The channel is *only* GitHub's prerelease flag (D-C1) — the signed manifest describes the payload, so a flip is a metadata edit, and switching channels never downgrades (D-C2). The setting lives in the CP settings table (`cp_meta`), materialized to `/var/lib/lemonseed/channel` for the DB-free root jobs; the installer seeds it. The old `test.lemonseed.ai` origin survives only as a `--url` value. |
| Release payload (M8 P2) | **All appliance artifacts ship as one canonical tar cut into fixed 1 GiB `payload-NNN.part` parts**, never gzipped — the SquashFS is already zstd-compressed, so a second layer wins nothing. `manifest.json` stays outside the tar and describes both the chunks and the outputs; the installer ISO ships as its own direct asset. `make release` builds → packs → signs → self-verifies → stages `out/release-<version>/`; the hand-run SCP (`push-image-to-website.sh`) is removed (D23) and publishing becomes P4's GitHub-release job. Design: [`proposals/M8/PACKAGING.md`](./proposals/M8/PACKAGING.md). [2026-10, M8 P2] |
| Auth without TLS | The appliance is reached by mDNS name over **plain HTTP**, so TLS is not available: there is no FQDN for a public cert, browsers reject self-signed certs and re-block them, and importing a private CA is beyond a home user. Because the transport cannot be trusted, the design goal becomes **never put the plaintext password on the wire at all** — a PAKE (SRP-6a) rather than client-side hashing, which is only obfuscation. See `proposals/M7/PLAN.md` D1. |
| SRP rollout | **SRP-only, no migration path** (chosen over shipping SRP alongside bcrypt, and over a one-time migrate-on-next-sign-in). Reason: a mixed tree is a tree with two auth paths, one of which is the thing we are removing; and there is **no install base**, so a breaking change is free today and expensive later. bcrypt cannot yield an SRP verifier (one-way), so "migrate on next sign-in" would have kept the plaintext POST alive indefinitely. Consequence: **all existing test machines need a fresh reinstall**, and A/B rollback across the boundary cannot sign anyone in (§11). |
| Password policy | **Client-side, in `SRP_ENROLL_JS`.** This is a consequence, not a preference: enrollment derives the verifier in the browser, so the server never holds the password and cannot apply a rule to it. A server-side policy would require the server to see the password, which is the property we just gave up. Stated plainly so nobody later reads the missing server check as an oversight. |
| Lint gate | `make lint` (ruff from nixpkgs, config in `ruff.toml`) over `control-plane/ scripts/ installer/`, kept **out of** `make test` so unit tests stay offline. Curated rule selection; permanent documented exclusions are `UP045` (conflicts with AGENTS.md's `Optional` guidance), `TRY004` (those `ValueError`s are deliberately caught by the enclosing handler — `TypeError` would turn 400s into 500s), and `external = [BLE001, N802, PLC0415]` so the call-site `noqa` documentation stays without enabling the rules. |
| Test determinism | The suspected control-plane suite flake is **disproven**: 36 perturbed runs (shuffled, reversed, `PYTHONHASHSEED`, per-module isolation, leak probes) were all green. The real hygiene defect was process-global state (`owui_policy._last_good_manifest`, `_status`) leaking between test classes; the reconcile class now snapshots and restores it via `addCleanup`. |
| Build closure pin | `appliance-artifacts` writes the toplevel path into `$out/closure-root`. Necessary because `mksquashfs` output is opaque to nix's reference scanner, so the image's closure had **no** GC root and the post-build `nix-gc.sh` collected the locally compiled CUDA llama.cpp every single build. |
| Sole writer of `users` (M7 P3, D-32) | **`control-plane/usermgmt.py` is the only code allowed to write `users` and `user_groups`.** Every primitive is validate → guard → one CP transaction → Open WebUI push → audit row → `notify(event, payload)` to in-process observers, so guards and audit cannot be skipped by a caller and a future module extends through the observer list instead of a plugin framework. Guards run **inside** the transaction (D-2) because a UI that hides a button is not a guard. Rejected: spreading the writes across `auth.py` and the HTTP handlers, where a fourth path would eventually forget the last-admin check. [2026-10, M7 P3] |
| Username is immutable (M7 P3, D-4/D-33) | `users.username` never changes; the portal edits `display_name`. Root cause: `x` is bound into the SRP hash (`srp.py`), so renaming an account invalidates its stored verifier and silently makes it unsignable — a data-destroying surprise hiding inside a rename. Usernames are `[a-z][a-z0-9_.-]{1,15}`, lower-cased on every path (D-33) so sign-in is case-insensitive while the stored key stays single-valued. [2026-10, M7 P3] |
| Three identifiers, not one (M7 P3, D-15/D-16′/D-18) | `users.username` is the human key and is immutable; the Open WebUI email is **derived** (`{username}@localhost`) and never stored; `users.owui_id` (partial unique index, `WHERE owui_id IS NOT NULL`) is the row key for every mirror write. Rejected: keying the mirror on email, which is the verified root cause of the duplicate-`admin@localhost` defect. Consequence accepted: a mirror write that fails is drift the next reconciliation repairs, not a duplicate account. [2026-10, M7 P3] |
| Account recovery is physical (M7 P3, D-20/D-20a/D-27/D-28) | A forgotten **admin** password is recovered only by preserve-reinstall with **Reset admin credentials** checked. Rejected: any in-portal recovery, because a path that can set a verifier without knowing the old password is reachable by the whole LAN — physical access is already the appliance's root of trust (§9, empty root password). A **user** password is reset by an admin, whose **browser** generates the one-time password and posts only its verifier (D-10), so the OTP never transits; the target gets `password_reset_required` and `srp/verify` answers `M2` with **no session cookie** until they rotate (D-11), which is one rule that cannot be forgotten per-surface (the portal, the APIs and Open WebUI all inherit it). Lockout applies to OTP sign-ins so reset is not a lockout bypass (D-12). Accepted and disclosed (D-13): the admin knows the OTP and can sign in as that user until they rotate. [2026-10, M7 P3] |
| Audit trail (M7 P3, D-35/D-36/D-37) | An append-only **`audit_log` in Postgres** plus a one-line journald copy per event, actor/target/action shaped with a JSONB `detail` masked through the **same** secret-masking the OWUI manifest uses (D-37) — an audit log that records secrets is worse than none. Chosen over journald-only because the consumer is a query (“who changed this, when”), and journald trims by volume, not by meaning. Retention is **365 days**, swept by an ordinary `DELETE` from the data-plane supervisor tick (`audit.maybe_sweep`), which keeps the additive-only migration test green by staying application SQL. The actor comes from the session, never from the request body. [2026-10, M7 P3] |
| Group attributes are typed in code (M7 P4, D-3/D-30) | `group_attributes(group_name, key, value JSONB)` stores real JSON booleans, and the **set of valid keys with their validators lives in `usermgmt.ATTRIBUTE_KEYS`** — an unknown key is a refused write, not a typeless blob. Growing the value types is a new dict entry, not a new schema version, which is what the additive-only migration rule makes cheap. System groups (`admin`, `users`) carry `groups.system = TRUE` and the group routes refuse to delete them. [2026-10, M7 P4] |
| Reserved identities are rows, not names (M7 P3, D-22) | The appliance's own admin and service identities are recognized by their database row, not by matching the string `admin`. The first admin's username is whatever `/setup` typed; `admin` is only the default, and nothing may key authorization on the literal. [2026-10, M7 P3] |
| Deleting an account deletes its Open WebUI account (M7 P3, D-16a/D-23) | Immediate, typed-confirm, and it deletes the Open WebUI row too (chats included), because OWUI keys identity on email and an orphan could later be **adopted** by a new account with the same username — one user inheriting another's data. Orphans are never adopted; `usermgmt.cleanup_owui_accounts` sweeps ones Open WebUI created behind our back. Deferred, not denied: the tombstoned 7-day-undelete window (§11). [2026-10, M7 P3] |
| Baseline rewrite over backfill (M7 P3, D-21/D-29/D-31) | `SCHEMA_VERSION = 6` over `BASELINE_SCHEMA_VERSION = 5`: the shipped v4/v5 steps were folded into the baseline and **deleted**, which is the only way a column can vanish under an additive-only migration rule, and `users.password_hash` vanished with them. **No backfill and no pre-M7 repair code exist anywhere in the tree, on purpose** — backfill is permanent debt incurred for machines that will be reinstalled anyway. The failure mode is loud by design: `migrate_cp_schema` **raises** “this build cannot upgrade it — reinstall the appliance” for any version below the baseline rather than falling through to a half-shaped database, and `provision_data_plane` reports that as an error state. Cheaper than a vestigial column, and affordable only while there is no install base. [2026-10, M7 P3] |

---

*Historical review checklists for M1/M5/M6 live in [`proposals/M6/REVIEW-CHECKLISTS.md`](./proposals/M6/REVIEW-CHECKLISTS.md).*
