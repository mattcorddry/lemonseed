# Milestone review checklists (archive)

> **Milestone design artifact.** Moved out of `DESIGN.md` during the
> M6 close-out docs cleanup. `DESIGN.md` describes the product **as built**;
> this file is the historical design record for the milestone and may
> describe intent that later work changed. Check `DESIGN.md` first.

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

