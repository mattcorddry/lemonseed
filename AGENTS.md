# AGENTS.md — Lemonseed agent guide

Guidance for AI agents (and humans) working in this repository. Prefer this file
plus `DESIGN.md` over inventing process. When they conflict, **DESIGN.md** wins
on product/architecture; **this file** wins on how to change code and close a sprint.

## Project snapshot

Lemonseed is a **NixOS appliance** (read-only SquashFS, A/B updates) with:

- **Control plane** — Python on `127.0.0.1:8080` (`control-plane/`)
- **Gateway** — nginx `:80` only public entry (`nixos/modules/gateway.nix`)
- **Inference** — headless `lemond` in SquashFS (not Docker)
- **Data plane** — PostgreSQL (+ pgvector) and Valkey on user-data
- **Modules** — Docker apps (e.g. Open WebUI) managed by the control plane

Read `DESIGN.md` before changing milestones, auth, storage, or UX routing.

## Operating principles

1. **Small, purposeful diffs** — match existing style; no drive-by refactors or unrelated files.
2. **DESIGN.md is the product contract** — update it when decisions change; do not silently diverge.
3. **Flakes only see tracked/staged files** — new paths must be `git add`’d before `nix build` / `make build-*` or Nix will not see them. `make test` and `make build-*` run `scripts/check-untracked.sh` and **fail** if any non-ignored untracked files exist (bypass: `ALLOW_UNTRACKED=1`).
4. **Do not commit or push** unless the user explicitly asks.
5. **Do not skip the test lifecycle** at the end of a work sprint (see below).

## Python guidelines (`control-plane/`, `scripts/`)

- Target **Python 3** as used on the appliance (stdlib-first; add deps only via Nix packaging in `nixos/modules/control-plane.nix`).
- Use `from __future__ import annotations`, type hints on public functions, and `Optional`/`Any` where needed.
- Prefer clear names over comments; comments explain *why*, not *what*.
- Catch broad exceptions only at service boundaries (HTTP handlers, background workers); log or return structured `{state, error}` — do not swallow failures silently in provisioning/module code.
- Background work: non-blocking locks + daemon threads (same pattern as `chat_ready`, `data_services`, `modules`).
- Unit tests live next to code as `test_*.py` and run via `unittest` (`make test` → `scripts/run-unit-tests.sh`).
- Mock subprocess/`docker`/`psycopg2` in unit tests; do not require Postgres, Docker, or a GPU for `make test`.
- Avoid adding `useMemo`-style premature optimization; keep CP code straightforward and readable.

## Nix / appliance guidelines

- Modules under `nixos/modules/`; wire imports in `nixos/configuration.nix` (or profile overlays like `qemu-test.nix`).
- Keep **LAN exposure** to nginx `:80`. Bind Postgres, Valkey, `lemond`, and CP to localhost (or host-network with `127.0.0.1` listen).
- Containers join the user-defined Docker network **`lemon`**. Host-facing ports use `-p 127.0.0.1:…` only (never `0.0.0.0` / LAN NICs). Inter-container DB/cache access uses Docker DNS names (e.g. `lemonseed-postgres`). Host services (CP, nginx) use the localhost publishes. `host.docker.internal` is reserved for reaching **host** processes (e.g. `lemond`) from a container.
- **Module images:** always target the **latest upstream stable** release. Pin an explicit version tag in-tree (e.g. `ghcr.io/open-webui/open-webui:v0.11.0`) — never `:latest` / `:main` in appliance code — so deployed boxes do not silently pull untested digests. Rebase the pin regularly in the build pipeline after `make test` / QEMU (and hardware when relevant) validate the candidate.

## Development lifecycle (required)

Treat a **work sprint** as: implement → validate → report. Do not return to the user claiming “done” until the checks below that apply have passed (or you have reported a concrete blocker).

### 1. During implementation

- Keep changes scoped to the task.
- Stage new files early enough that flake builds can see them (`make check-untracked` / `make test` will fail until you do).
- If behavior changes, extend or add unit tests in the same change set.

### 2. End of sprint — always

Run from repo root:

```bash
make test
```

This is the **Tier A** gate (no root, no hypervisor): update/artifacts tests, gen-repart, control-plane unit tests. **Must be green** before you say the sprint is complete.

### 3. End of sprint — when the change touches boot, gateway, CP HTTP, data plane, or modules

```bash
make build-qemu          # if artifacts missing or Nix inputs changed
QEMU_API_TIMEOUT=1200 make test-qemu
```

Notes:

- `test-qemu` boots the appliance-qemu profile and probes `/healthz`, `/api/status`, `/api/boot`, `/api/data`, `/api/modules`, etc.
- Without `/dev/kvm` access the guest runs under TCG and is slow — raise `QEMU_API_TIMEOUT` (e.g. `1200`) rather than declaring failure early.
- The **appliance-qemu** profile preloads pinned Postgres and Valkey at boot and loads Open WebUI from a host virtio-fs export during `make test-qemu` (`nixos/modules/qemu-docker-cache.nix`) so smoke tests do not depend on registry pulls inside the VM.
- If QEMU fails, iterate (fix → rebuild → re-test) until green or escalate with logs (`out/qemu-api-smoke.log`).

### 4. Optional / on request

| Command | When |
|---------|------|
| `make build-production` | Shipping an image / release |
| `scripts/push-image-to-website.sh` | Only when the user asks to publish |
| Hardware smoke on NVIDIA prototype | GPU/chat/OWUI iframe validation beyond QEMU |
| `make test-update-unit` | Narrow rerun of update tests only |

### 5. Closing the sprint (report)

When coming back to the user, include briefly:

1. What changed (files / behavior)
2. **`make test` result**
3. **`make test-qemu` result** if step 3 applied (or why it was skipped)
4. Remaining risks or follow-ups (do not hide known failures)

## Quick command map

| Goal | Command |
|------|---------|
| Unit tests | `make test` |
| Untracked files (flake gate) | `make check-untracked` |
| QEMU HTTP smoke | `make build-qemu && make test-qemu` |
| Interactive QEMU serial | `make run-qemu` |
| Production artifacts | `make build-production` |
| Help | `make` / `make help` |

## Doc map

| Doc | Role |
|-----|------|
| `DESIGN.md` | Product, milestones, architecture decisions |
| `AGENTS.md` | How agents should work and close sprints |
| `README.md` | Human-oriented project entry |
| `Requirements.md` / `Concept.md` | Historical / broader context — defer to DESIGN when unsure |
