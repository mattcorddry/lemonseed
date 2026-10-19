# Lemonseed

Lemonseed is a **local home AI appliance**: a NixOS-based, read-only SquashFS image with A/B updates, a web installer, and a small control plane. The goal is private LAN chat and related workloads on ordinary PC hardware (NVIDIA and AMD Strix Halo APUs supported today), without depending on cloud AI for core use.

This repository builds the appliance artifacts, the USB/EFI installer, and the bring-up tooling. Deeper design lives in [DESIGN.md](./DESIGN.md); product intent in [Concept.md](./Concept.md). Agent/process conventions live in [AGENTS.md](./AGENTS.md).

## Getting started

```bash
make
```

That prints the available targets. Common ones:

| Command | What it does |
|---------|----------------|
| `make build-production` | Appliance SquashFS + kernel (+ initrd) → `out/latest` |
| `make build-qemu` | QEMU smoke profile → `out/latest-qemu` |
| `make run-qemu` | Build QEMU image if needed, then boot a serial console |
| `make build-installer` | EFI/USB installer ISO → `out/latest-installer` |
| `make test` | Unit tests — update, repart, control-plane (no root) |
| `make test-qemu` | QEMU boot + HTTP API smoke (`/healthz`, `/api/status`) |
| `sudo make repart-test` | Loopback GPT/repart smoke (requires root) |
| `sudo make update-test` | Loopback A/B updater smoke (requires root) |

You need [Nix](https://nixos.org/download/) with flakes enabled. See `nix-install.sh` if you are setting up a build host from scratch.

## Tree overview

```
boot/           GRUB config (A/B slot selection)
control-plane/  Appliance HTTP control plane (setup + status)
installer/      USB installer pipeline + web UI
nixos/          NixOS modules (appliance + installer)
  modules/      Storage, A/B root, NVIDIA, lemond, gateway, …
  installer/    Live installer image config
scripts/        Packaging, QEMU, repart, update, and test helpers
partitions.yaml Disk layout for systemd-repart
flake.nix       Nix flake (nixpkgs 26.05)
Makefile        Entry point for builds and smoke tests
```

Build outputs land under `out/` (gitignored). Signing keys under `keys/` are local-only; `make keys` creates test keys when needed.

## Sign-in

The appliance is served over **plain HTTP on your LAN** and always will be: it is reached by
`*.local` mDNS name, so there is no FQDN for a public certificate, and self-signed certs or
importing a private CA are worse UX on household devices than no TLS at all.

Because the transport cannot be trusted, **the password is never sent to the appliance**.
Setup derives a salt and a verifier in your browser; signing in proves you can reproduce a
value derived from the password, without transmitting it ([SRP-6a](https://datatracker.ietf.org/doc/html/rfc5054)).
The control plane never holds, and never could send, your password. Details in
[DESIGN.md](./DESIGN.md) §3.3.

Two honest consequences: sign-in requires JavaScript (there is no server-side fallback), and
the minimum password length is enforced by the browser, because a server that cannot see the
password cannot police it. And two things this does *not* buy: the session cookie is still a
bearer token over HTTP, and an attacker already positioned on your network could rewrite the
login page rather than just sniff it. Anyone with physical access still owns the box.

If you forget the **admin** password there is no email link and no reset page: you boot the
USB installer, keep your data, and tick **Reset admin credentials**, which reopens the
first-boot setup. Forgetting any other account's password is an admin's job — they hand you a
one-time password, and it makes you choose a new one before you get signed in at all. The
mechanism is in [DESIGN.md](./DESIGN.md) §9.

## Status

Pre-release. Docs and packaging will expand when we are ready to publish broadly.
