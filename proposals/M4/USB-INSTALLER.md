# M4 / M4.1 — USB installer & user-data preservation (design record)

> **Milestone design artifact.** Moved out of `DESIGN.md` during the
> M6 close-out docs cleanup. `DESIGN.md` describes the product **as built**;
> this file is the historical design record for the milestone and may
> describe intent that later work changed. Check `DESIGN.md` first.

---

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

