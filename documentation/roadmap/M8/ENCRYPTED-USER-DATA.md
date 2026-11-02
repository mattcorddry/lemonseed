# M8 P7 — Encrypted user data + console lockdown (design record)

Status: **draft for review — second pass, revised 2026-10-29** after the first
review. The design gate required by [`PLAN.md`](./PLAN.md) §P7 (deliverable:
`ENCRYPTED-USER-DATA.md`). The decisions below implement **D24, D25 and D27**
as settled in the plan, amended by this review where marked. New decisions
continue a phase-scoped **D-E…** series, the pattern [`CHANNELS.md`](./CHANNELS.md)
used for its D-C… series.

Related: [PLAN.md](./PLAN.md) §P7 · [DESIGN.md](../../DESIGN.md) §6 (root
filesystem model), §9 (security posture), §4 (A/B boot protocol) ·
`installer/install.py`, `scripts/appliance-storage-provision.sh`,
`scripts/disk-release.sh`, `scripts/lemonseed_update.py`,
`nixos/modules/{storage,console,ab-boot}.nix`.

---

## 1. Problem and threat model

User-data (the btrfs LV carrying `@etc`/`@var`, Postgres, Valkey, models,
container state) is unencrypted, root has an **empty password**, and tty2
auto-logs-in a root shell. A stolen box — or just a pulled disk — hands over
household data.

**What this design protects:** the data at rest on the user-data volume, against
the disk being moved to other hardware (the "pulled disk" case) and against the
TPM being replaced or erased (the "sealed key is gone" case). With the D-E1
checkbox an unencrypted install is also possible; such a box deliberately
accepts the pulled-disk case, and the checkbox copy says so.

**What it does not protect — stated plainly, next to the flows that depend on
it:**

- A **running, unlocked box**. The LUKS key is in the kernel keyring while the
  system is up; anyone with the box powered on and unlocked can read the disk.
- Someone holding **admin credentials on the LAN**. The Hardware password is
  *deliberately* viewable in Settings → Advanced (D27), so it lives in the CP
  database; the database is inside the encrypted volume, but an admin who can
  reach the CP can read it. Encryption protects the disk against theft, not the
  household against its own admins. It does not bring TLS back. This plaintext
  storage is **provisional** — revisit is backlogged (§9.6), and nothing in
  this design may depend on it (§5.3).
- A thief who **boots alternate media on the same, intact box** (see §3.3,
  PCR policy — the residual is documented and backlogged, not hidden).
- The **ESP**, which stays unencrypted. Nothing secret may be written there
  (§3.6).

**Product constraint (D-KVM):** the household never needs a KVM on the
appliance. Every recovery path below works from the boot menu or the web UI;
a console prompt is a *fallback*, never the only path.

## 2. Migration

Existing machines migrate by **USB reinstall with data loss** (D9 — both test
boxes are rebuildable; there is no in-place encryption path). There is no
conversion step, no keyfile escrow, no plaintext-to-ciphertext dance on a live
volume: a pre-P7 box either reinstalls fresh (data loss) or, once on a P7
release, uses the preserve-reinstall flow of §5.

Which volume a box gets is decided **at install** (D-E1): the options-page
checkbox defaults to encrypted whenever the TPM is usable. A pre-P7
preserve-reinstall keeps its plain volume and the new appliance runs
unencrypted — accepted: re-encrypting in place was rejected with the migration,
and the next fresh reinstall encrypts. Post-P8 images mount **both** kinds of
user-data volume: LUKS2 (new installs with the checkbox on) and plain
btrfs-over-LVM (pre-P7 boxes and unencrypted installs). The provisioner
branches on `cryptsetup isLuks` (§3.4), so nothing above the mapper needs to
know which kind it is.

## 3. Boot and unlock architecture

### 3.1 Where the volume comes from today

The installer creates partitions only: ESP, two raw-SquashFS slots, and a
`lemonseed-user-data` partition that is an **LVM PV**. On first boot the
initrd runs `appliance-storage-provision` (`storage.nix`), which creates
VG `appliance` → LV `user-data` → btrfs → subvolumes `@etc`/`@var` before the
`sysroot-etc`/`sysroot-var` mounts; the same script runs in stage 2
(`local-fs-pre.target`) as an every-boot no-op when the LV exists.

### 3.2 What changes: LUKS2 under the PV

The user-data partition becomes a **LUKS2 container** whose PV is the
`/dev/mapper` device, so the provisioner's PV/VG/LV/btrfs pipeline is unchanged
above it. Two additions:

- **Installer (§4):** LUKS-formats the partition, enrolls the TPM, and
  provisions the volume while it is open (the provisioner is idempotent, so the
  installer can simply invoke it) — this also lets the installer seed
  `/var/lib/lemonseed/hardware-password` into `@var` for the CP to adopt
  (§5.1).
- **Provisioner (§3.4):** before `pvcreate`, unlocks the container — TPM token
  first, console passphrase second, legible failure last.

The `@etc`/`@var` mounts, Postgres, Valkey and models come up exactly as today;
nothing above the mapper knows encryption exists.

### 3.3 Key slots and PCR policy (D24)

The LUKS2 header carries two key material sources:

| Slot | Material | Purpose |
|------|----------|---------|
| 0 | **Hardware password** (passphrase) | Recovery: installer preserve-reinstall, console unlock if a keyboard is attached |
| token | **systemd TPM2 token**, sealed to the TPM's unique key + the PCR policy | Every-boot unattended unlock |

Slot 0 is a **passphrase** by decision (confirmed in review): the Hardware
password alone recovers the volume without the TPM, so the sealed token is the
convenience path, never the only one.

**PCR policy: PCR 7 only** (the Secure-Boot-policy register). Rationale and the
residual, stated:

- PCR 7 is stable across our A/B slot switches (GRUB writes to the ESP; the
  firmware does not re-measure it), so **updates and rollbacks reboot
  unattended** — the M7 P5 self-rebooting update must not hang on a passphrase,
  and it does not: a healthy TPM unseals silently on every boot.
- Binding tighter (PCR 4/9 via GRUB's TPM measurements, or PCR 11 via
  systemd-stub) would re-bind the seal on every update and every rollback —
  GRUB measures the kernel/initrd it loads, so each new slot changes the
  policy — and would need re-enrollment machinery the milestone does not need.
- **Residual:** with Secure Boot off, PCR 7 is effectively a constant, so the
  token unlocks for *anyone who can run software on the intact box* — a thief
  who boots alternate media from the firmware menu on the same machine defeats
  a PCR-7-only seal. The disk-pulled and TPM-replaced cases (the stated threat)
  are fully covered. Binding the seal to the measured kernel is **backlogged as
  *Alternatives to TPM*** (D24) alongside the no-TPM alternatives; see §9.4 for
  the Secure Boot answer.

A firmware update that changes PCR 7 breaks the seal; recovery is the
preserve-reinstall flow of §5, which reloads the password into the TPM — the
same flow, not a new one.

### 3.4 The unlock step (provisioner)

`appliance-storage-provision.sh` gains an `unlock_storage_device` step between
device discovery and `pvcreate`, with a **three-way branch** (review point:
how the system tells a LUKS volume from a blank or corrupted one — the LUKS2
magic is unique, and `cryptsetup isLuks` checks it, so detection is reliable):

1. **LUKS2** (`cryptsetup isLuks` succeeds): unlock — as built,
   `cryptsetup open --type luks2 --token-only` first (§11.5; the earlier
   `systemd-cryptsetup attach … --tpm2-device=auto` formulation mixed the
   systemd-cryptsetup option into the cryptsetup CLI). No prompt on this
   path; an unattended reboot never waits on a passphrase. Token failure
   (TPM absent on an encrypted install, replaced, erased, no key enrolled,
   or PCR policy drifted) falls back to one bounded passphrase attempt
   (§3.7). If the mapper device already exists — the installer invokes the
   provisioner with the container open (§4.2) — the unlock short-circuits.
2. **Not LUKS, not blank** (a PV/LVM/btrfs signature — the pre-P7 plain volume
   or any unencrypted install): behave exactly as today.
3. **Blank** (neither): provision from scratch, plain — because the installer
   creates the LUKS container itself when the checkbox (D-E1) is on, a blank
   device reaching the provisioner means an unencrypted install (or a wiped
   partition, indistinguishable from a wiped disk and accepted).

A **corrupted LUKS header** (magic intact, metadata invalid — LUKS2 keeps a
secondary header copy at 16 KiB and `cryptsetup` falls back to it
automatically; both copies damaged = corrupted) fails the boot through the
§3.7 legible path. A corrupted volume is **never silently reformatted**:
reformatting is a data decision only a reinstall — which asks, and preserves
only what it can open — is allowed to make. There is no "wipe and reprovision"
shortcut in the provisioner.

4. The mapper device (`/dev/mapper/user-data-luks` — a fixed name, since
   `MAPPER_PATH` for the LV is derived from the VG/LV names) is then used as the
   PV; `vgchange -ay` follows as today.

The initrd gains `systemd-cryptsetup`, `cryptsetup`, the TPM userspace stack
and the `tpm_crb`/`tpm_tis` kernel modules (`boot.initrd.availableKernelModules`
+ `storePaths` in `storage.nix`). Both the initrd and stage-2 copies of the
provisioner share the unlock logic. As built, the unlock also needs the cipher
module set and the token-plugin library path — §11.2/§11.4.

**Fail vs. skip:** the existing `APPLIANCE_STORAGE_REQUIRED=1` contract already
fails the boot when the volume cannot be assembled; unlock failures fail the
boot the same way — legibly (§3.7) — with the message on the console and in the
journal.

### 3.5 Unattended reboots (the update contract)

The updater writes the inactive slot, flips `boot_once` and reboots. With a
healthy TPM the unlock is token-only and silent — the reboot proceeds into the
new slot and the appliance comes back whole. The QEMU test asserts the whole
round trip (§7), because this is precisely what TPM sealing bought M7 P5.

### 3.6 The ESP stays secret-free

The ESP is unencrypted and the one part of the disk a thief reads first, so **no
plaintext key material ever lands on it**. The new ESP artifacts are
`grub/hardware-params` (§4.4), which carries the GRUB **PBKDF2** hash of the
Hardware password — a deliberately slow hash over a high-entropy passphrase,
safe to expose — and the unlock-attempt counter (§3.7), which is a number.
Both are safe by construction; the rule stands: no plaintext key material on
the ESP, ever.

### 3.7 When the volume will not open (D-E2)

A token unlock fails for one of the reasons above; a passphrase attempt fails
when the Hardware password is wrong or lost. The boot response is the same,
and it is **bounded** — some failures are transient (a slow TPM, a firmware
hiccup), so one failed boot should not strand a box that a reboot would fix,
but a perpetual reboot loop is worse than a stopped, legible one: nobody is
watching at 3 a.m., and a looping box that eventually boots would look healthy
when it is not.

- Each failed boot prints the legible copy on the console — *"user-data cannot
  be unlocked; boot the USB installer to recover"* — waits **30 seconds** (so a
  transient failure can self-heal on the retry, and a human who wandered off
  at power-on can come back), then reboots and tries again.
- After **3 failed attempts** the box **halts** on the same message. The
  attempt counter lives in `grub/unlock-attempts` on the **ESP** (writable,
  unencrypted, nothing secret there — §3.6) and is **deleted once an unlock
  succeeds** (the initrd removes it as soon as the volume is open — it counts
  unlock failures, nothing else; a boot that gets past the unlock cannot loop
  on it), and a reinstall's rewritten ESP leaves without it.
- The recovery copy names the one path that always works: **boot the USB
  installer** (preserve-reinstall prompts for the Hardware password, §4.3).

**Why console-only, no HTTP on :80:** a failed boot has no portal, and serving
one would mean an HTTP + mDNS stack in the initrd — a second mini-appliance to
maintain, reached by a browser that over plain HTTP cannot authenticate the box
any better than the console message can. The 30-second message and the
halt-with-copy give the same information without that surface.

## 4. Installer changes

### 4.1 Preflight and the encryption checkbox (D-E1, amends D24)

TPM is required **for encryption, not for the install** (review decision). The
options page gains an **"Encrypt user data (recommended)"** checkbox:

- **TPM usable** (`/sys/class/tpm/tpm0` present and a working `/dev/tpmrm0`,
  proven by a trial seal): checkbox **checked** by default. The user may untick
  it — a VM whose host volume is already encrypted, for instance — and the
  install proceeds unencrypted.
- **TPM absent, or a trial seal cannot be established:** checkbox **greyed
  out**, with the inline warning *"This PC has no trusted platform module
  (TPM), so Lemonseed cannot encrypt the data storage drive. You can still
  install in unencrypted mode."*

The old hard preflight failure is gone; D24's preflight *assert* moves into the
encryption path (§4.2 step 2 is simply skipped when the checkbox is off). The
installer image gains the TPM/cryptsetup packages so the preflight is a real
trial seal, not a file check.

### 4.2 Fresh install: the format step

In `run_install`, after `partition_disk`:

- **Checkbox on (default):** the LUKS path, before `write_raw`:
  1. **Generate** the Hardware password — the existing **N-words-with-dashes**
     scheme from the non-admin password-reset flow, so it is readable and
     writable by hand.
  2. `cryptsetup luksFormat --type luks2` on `lemonseed-user-data` with it
     (keyslot 0).
  3. Open the volume; `systemd-cryptenroll --tpm2-device=auto` enrolls the TPM
     token; then **prove the seal**: close the volume and re-open it token-only
     before continuing. An enrollment that cannot be re-unsealed fails the
     install here, not at first boot.
  4. Run the (idempotent) provisioner against the open mapper: VG/LV/btrfs/
     subvolumes, and seed `/var/lib/lemonseed/hardware-password` (mode 0600)
     into `@var`.
  5. Close the volume; continue with the existing `write_raw`/`seed_esp` steps.
- **Checkbox off:** none of the above — the provisioner's blank-device branch
  (§3.4) provisions plain, exactly as pre-P7.

**Presentation (D27 copy):** the Hardware password is shown on the installer's
options page — before "Install" starts — in large type with a "write this down"
instruction and this explainer:

> **Hardware password.** Keep this password somewhere safe, away from this
> computer. It may be required if the trusted platform module (part of the
> motherboard) is replaced or erased; if decryption fails, a reinstall reloads
> the password into the TPM.

The value lives in a **reveal dialog** (the M7 one-dialog component, D-39) with
the password in a `user-select: all` field and a **Copy** button that falls
back to `execCommand('copy')` (D-45): `navigator.clipboard` exists only in a
secure context, so on the plain-HTTP portal the fallback is the working path,
not the edge case. A **Generate again** affordance regenerates until the user
continues (no rotation exists post-install — D27 — so the choice is only free
before it is committed).

### 4.3 Preserve-reinstall: ask, verify, re-enroll (D27)

When the target's user-data is LUKS2, the options page runs a **trial unlock**
as soon as preserve mode is confirmed:

- **TPM token unlocks** → nothing is asked; the reinstall proceeds and the
  preserved volume is opened token-only.
- **Token fails** (TPM replaced/erased, or PCR drift) → the options page
  **prompts for the Hardware password** with the D27 copy, validates it by
  trial-unlock *before* the install starts, and **refuses a wrong one** with a
  legible error — the volume is not touched until the password verifies.
- During the reinstall the installer re-enrolls the TPM (a reinstall *reloads
  the password into the TPM*), rewrites `grub/hardware-params` (§4.4) and
  refreshes the root-password hash from the preserved database (§5.2).

A preserve-reinstall of a **pre-P7 box** (plain volume) skips all of this and
preserves an unencrypted volume (§2).

### 4.4 GRUB lockdown (D25)

`boot/grub.cfg` gains:

```grub
set superusers="grub"
password_pbkdf2 grub ${HARDWARE_PBKDF2}
```

with every menu entry `--unrestricted` — **choosing among existing boot entries
stays unrestricted** (the rollback path works from the boot menu without a
KVM), while editing entries and the GRUB console require the superuser
password. Because the slot-version labels are grubenv variables expanded at
boot, the template does not need to change per update.

The `${HARDWARE_PBKDF2}` value is **not** templated into the file — the config
sources a second ESP file, `grub/hardware-params` (the pattern of the M8 P3
`channel-params` stub):

```grub
if [ -f ($root)/grub/hardware-params ]; then
  source ($root)/grub/hardware-params
fi
```

`hardware-params` carries the `superusers`/`password_pbkdf2` lines and is:

- written by `seed_esp` at every install (fresh and preserve) — `grubenv` and
  the ESP are otherwise untouched by the updater, so the file survives A/B
  updates;
- refreshed by the CP whenever the Hardware password changes (it does not, in
  this milestone — D27), so the file and the database never drift.

The lockdown is **uniform across encrypted and unencrypted installs** (D-E5):
the same password guards GRUB editing and the root console either way; on an
unencrypted box only the LUKS use of it is absent. A pre-P7 box's ESP (no
`hardware-params`) boots an updated image unchanged: the `if [ -f … ]` guard
keeps the source line optional, and unencrypted user-data needs no unlock.

### 4.5 The installer image

The installer profile gains `cryptsetup`, `systemd-cryptenroll`, the TPM
userspace stack (`tpm2-tools` is not needed — enrollment is via
`systemd-cryptenroll`), and the LVM/btrfs tools the provisioner uses when it is
invoked against the open volume. `disks.py` preflight gains the TPM trial-seal
check of §4.1.

The **installer image's root stays unlocked** (`initialHashedPassword = ""`, a
mutable live system) — the lockdown of §6 is appliance-image-only. Debugging a
failed install at the console must not require the Hardware password.

### 4.6 Boot order (D-E3)

`seed_esp`'s `efibootmgr --create` registers the entry but says nothing about
where it lands in `BootOrder` — the resulting order is firmware-dependent. The
installer now **pins it**: after `--create`, read `BootOrder` from `efibootmgr`
and rewrite it (`efibootmgr -o …`) with the Lemonseed entry first and the
machine's existing entries preserved behind it, so the firmware stops
preferring whatever it preferred before (or a leftover OS entry) over the
installed appliance. The removable-path fallback (`BOOTX64.EFI`) is unchanged —
a USB stick stays bootable by hand from the firmware's boot menu; pinning the
NVRAM order only stops the firmware from *auto*-preferring something else.

## 5. Secrets and their lifecycle (D25/D27)

One secret, three uses: the **Hardware password** is the LUKS recovery
passphrase, the GRUB superuser password, and the local root password. Generated
at install, presented with the D27 copy, and:

| Where | What | Why |
|-------|------|-----|
| LUKS2 keyslot 0 | passphrase | recovery unlock (installer, console fallback) |
| LUKS2 TPM2 token | sealed key | every-boot silent unlock |
| CP database (`cp_meta`) | plaintext | the admin can read it in Settings → Advanced (that is the design, D27) |
| `@var/lib/lemonseed/grub-hardware.pbkdf2` | PBKDF2 hash, 0600 | the updater rewrites the ESP `grub.cfg` on every update and must carry the superuser block |
| `/etc/shadow` (root) | hash | the tty2 login prompt (§6) |
| ESP `grub/hardware-params` | PBKDF2 hash | GRUB checks it before the disk is unlocked |

On an **unencrypted install** (D-E1) the same password is still generated and
presented: two of the three uses remain — GRUB superuser and root console — and
the Settings → Advanced row stays meaningful. The LUKS slot and the TPM token
simply do not exist, and the bootstrap file of §5.1 is not seeded.

**The database plaintext is optional by design.** Storing the clear-text
Hardware password in `cp_meta` is provisional (revisit backlogged, §9.6): every
consumer above except the §5.3 reveal works from the hashes alone — the LUKS
slot 0 passphrase and the TPM token never read the database, `/etc/shadow` and
`grub-hardware.pbkdf2` are hashes, and the preserve-reinstall prompt (§4.3)
asks the *user*. If the plaintext is ever dropped, the only change is that the
Advanced row reports the password as not stored instead of showing it.

### 5.1 First boot

The CP adopts the installer's bootstrap file
(`@var/lib/lemonseed/hardware-password`) into the database at first boot, then:

- writes `grub-hardware.pbkdf2` (0600) for the updater;
- sets the root password hash in `/etc/shadow` (the image ships root **locked**;
  the empty-password root and the tty2 autologin are gone — §6);
- **deletes the plaintext bootstrap file** — from then on the database is the
  only copy (the caveat of §1).

### 5.2 Preserve-reinstall

The preserved database already holds the password; the preserved
`grub-hardware.pbkdf2` keeps the ESP file and the shadow hash in sync. Where
the installer re-enrolls the TPM after a prompt, nothing else changes — the
password that opened the volume *is* the stored one (there is no rotation,
D27).

### 5.3 Settings → Advanced (D27)

The SYSTEM panel gains an **Advanced** section with a **Hardware password** row
and an info button explaining what it is, where it is used, and when to reach
for it — so it does not read as the login password. Retrieval is an
admin-gated API (the machine secret is not household-readable); the value is
masked in logs and audit detail per **D-37**, and the reveal action writes an
audit entry like the other secret reveals. The reveal is a **dialog** (the
D-39 one-dialog component) with the value in a `user-select: all` field and a
**Copy** button falling back to `execCommand('copy')` (D-45) — not a spinner
and not a bare page: copy-to-clipboard is the point of the feature, and on
plain HTTP the fallback is the only working clipboard path. If the plaintext
is ever absent from the database (§5, revisit backlogged), the row degrades to
"not stored" — the reveal is the only feature that reads it.

## 6. Console lockdown (D25, D-E4)

- `console.nix` drops the **tty2 root autologin**; tty2 becomes an ordinary
  login prompt. `getty@tty1`/`tty4` behaviour and the status splash are
  unchanged. **No autologin anywhere.**
- An **`/etc/issue`** greets every login TTY (console and serial) so a keyboard
  attached to a locked-up box explains itself:

  ```
  Lemonseed <hostname> — appliance console

  This console is for recovery and maintenance. Sign in as `root` with the
  Hardware password — visible in the web portal (http://<name>.local) under
  Settings → Advanced. Regular account passwords do not work here; accounts
  are managed and used through the web portal.
  ```

- **Serial consoles:** the existing `serial-getty@ttyS0` guard is extended to
  `ttyS1`, so physical serial ports get login prompts when the firmware marks
  them consoles, while QEMU's virtual serial keeps working for
  `make run-qemu`. The guard pattern is the mechanism; the list of guarded
  ports is the knob. **As built (round-5 bare metal):** the original
  `ExecStartPre test -c /dev/ttyS0` guard was doubly wrong — a failing
  `ExecStartPre` *fails* the unit (the clean skip needs `ExecCondition`, whose
  1–254 exits skip without failure), and `console=ttyS0` in kernelParams makes
  the kernel create `/dev/ttyS0` even with no UART behind it, so the node test
  passed and agetty failed "not a tty". The guard is now an `ExecCondition`
  that additionally requires the port's `/proc/tty/driver/serial` line to
  name a real UART (`uart:` other than `unknown`), which stays true on QEMU's
  q35 16550A so `make run-qemu` keeps working.
- The image ships root **locked** (`users.users.root.hashedPassword` forced to
  a locked value, not `""`); the first-boot step of §5.1 applies the Hardware
  password hash, so the tty root shell is reachable only with the Hardware
  password — which the admin can read from Settings → Advanced when they are
  debugging with a keyboard attached.
- Boot-target selection (the GRUB menu, including rollback) needs **no
  password**; editing entries and the GRUB console do (§4.4).

## 7. Tests

Mirroring PLAN §P7 (plus the review additions):

- **QEMU (swtpm):** the QEMU profile gains a TPM 2.0 (`swtpm`, state persisted
  under `out/qemu-tpm/` until `make clean` — §9.3). The installer provisions an
  encrypted user-data volume; first boot unseals via TPM; `@etc`/`@var`,
  Postgres, Valkey come up; the appliance is whole.
- **Unencrypted install (D-E1):** the QEMU profile without swtpm installs with
  the checkbox greyed out (or unticked); first boot provisions plain; the
  update flow works end to end — the plain path keeps working.
- **Update flow on an encrypted box:** apply → unattended reboot → the box
  returns without a passphrase prompt; assert the unlock took the token path
  (journal) and that the reboot latency is unchanged.
- **Corrupted LUKS metadata:** flip bytes in the LUKS header region of a
  provisioned user-data partition; the boot fails legibly through §3.7 and
  **never reformats**.
- **TPM present but no key enrolled / wrong key:** (a) a LUKS volume with no
  token enrolled and (b) a token enrolled against a *different* TPM (swtpm
  state reset) both fail the token path and land on the §3.7 retry; the
  journal distinguishes the failure reasons; recovery is the same in both.
- **Bounded retry:** the attempt counter increments on the ESP, clears on the
  first successful unlock, and the third failure halts rather than loops.
- **Preserve-reinstall:** with the reset marker under encryption; and after the
  TPM seal is broken (swtpm state reset), the installer **prompts for the
  Hardware password** before touching the preserved volume and **refuses a
  wrong one**.
- **TPM-absent install** shows the greyed checkbox with the warning copy and
  installs unencrypted (QEMU profile without swtpm).
- **Console:** no root autologin on tty2; boot-target selection needs no
  password while editing/console do (grub-emu or scripted `grub.cfg` asserts);
  the tty root password is the stored one, not empty; `/etc/issue` renders on
  console and serial gettys; the ttyS1 guard skips absent devices.
- **Hardware password:** retrievable by an admin from Settings → Advanced via
  the reveal dialog with working Copy (fallback asserted); masked in logs and
  audit; the reveal writes an audit entry.
- **Unit tests:** the provisioner's three-way unlock branch (LUKS / plain /
  blank) against a stubbed `cryptsetup`/`systemd-cryptsetup`, including the
  already-open short-circuit; the grub template's conditional source; the CP
  adoption and PBKDF2 materialization.

## 8. Gate and bare-metal drill

Design doc approved → `make test` + QEMU green. **BM-3 (yours):** install on
bare metal, then the stolen-box check — pull the disk / boot without the TPM and
confirm nothing readable comes up; confirm the Hardware password recovers the
box through preserve-reinstall; and confirm the update flow reboots unattended.
Optionally, one unencrypted bare-metal install to sanity-check the D-E1 path on
real firmware.

## 9. Residuals, and review answers

1. **PCR policy** — **resolved in review: PCR 7 only** for this milestone (§3.3).
   The alternate-media residual and the PCR 4/9/11 binding are backlogged as
   *Alternatives to TPM* work.
2. **No rotation** (D27) — **confirmed in review.** Rotation would touch the
   LUKS keyslot, the TPM enrollment, the GRUB PBKDF2, `/etc/shadow` and the DB
   in one transaction; backlogged with the TPM alternatives.
3. **swtpm state lifecycle in QEMU** — **resolved in review:** persist until
   `make clean`. The state lives under `out/qemu-tpm/`, and `make clean`
   already removes all of `out/`, so the lifecycle needs no new knob — sprints
   run `make clean` between milestones, so residual test data does not survive.
   The broken-seal test paths (§7) remove just that directory.
4. **Secure Boot** — answered: the NixOS route is **Lanzaboote** (`lzbt`) — a
   UKI signed at build time against our own PK/KEK/db (or vendor keys + MOK),
   which is precisely the build-time signing machinery D11/D15 established for
   releases. With Secure Boot enabled, PCR 7 measures the SB policy and the
   §3.3 seal stops being a constant — that evaluation is the *Alternatives to
   TPM* backlog item.
5. **Keyslot choice** — **confirmed in review:** the passphrase in **slot 0**
   (§3.3), so the Hardware password alone fully recovers the volume without the
   TPM.
6. **Plaintext Hardware password in the database** — kept for now (D27 as
   built), but **provisional**: revisit is backlogged as *Revisit whether we
   want to store the clear text hardware password on Lemonseed*. The design
   must not depend on it — every consumer except the Settings reveal works
   from the shadow and GRUB hashes alone (§5).

## 10. Decision log (this review)

| Decision | Ruling |
|----------|--------|
| **D-E1** | Encryption is a choice at install (amends D24): the options page gains an "Encrypt user data (recommended)" checkbox — checked by default when the TPM is usable, greyed with a warning when absent. TPM is required *for encryption*, not for install; unencrypted installs are a supported, first-class state. |
| **D-E2** | Unlock-failure boot UX is bounded: each failed boot shows the legible message for 30 s, reboots, retries; after 3 failures (ESP counter) the box halts on the message. No HTTP/mDNS stack in the initrd — the recovery copy points at the USB installer. |
| **D-E3** | The installer pins the NVRAM `BootOrder` with the Lemonseed entry first (`efibootmgr -o`), existing entries preserved behind it; the removable-path fallback is unchanged. |
| **D-E4** | The console contract is legible without the portal: `/etc/issue` explains that root uses the Hardware password and regular accounts live in the web portal; serial gettys get the ttyS0-style guard on physical ports; no autologin anywhere. |
| **D-E5** | The Hardware password is generated and presented on **both** install kinds; GRUB lockdown and the root console are uniform. On unencrypted installs only the LUKS slot and TPM token are absent. |
| **D-E6** | The database plaintext of the Hardware password is **provisional**: kept for now (D27), revisit backlogged, and the design must work with hashes only — the reveal is the sole plaintext consumer (§5, §9.6). The installer image's root stays **unlocked** for install debugging; the §6 lockdown is appliance-image-only. |

## 11. As built — bare-metal findings (rounds 3–4)

The first bare-metal exercise of the encrypted install surfaced four findings
that the approved design did not anticipate; all are fixed and this section
records them as built.

### 11.1 The volume key is the bare Hardware password, byte for byte

The canonical volume key is the Hardware password **exactly as typed — no
trailing newline**. Every cryptsetup call (`luksFormat`, token enrollment,
`open`, the preserve-reinstall re-enroll) feeds it over `--key-file=-`, whose
stdin bytes are used verbatim; `_passphrase_stdin` therefore returns the
password unchanged, so the enrolled key and the keyboard fallback at boot
(where the TTY *does* strip the Enter) agree byte for byte.

Round-3's first-boot failure (correct Hardware password, unlock still failing,
D-E2 boot loop) was caused by a newline mismatch between the enrollment keyfile
and this canonical form. Confirmed on the box: enrolling a newline-terminated
keyfile succeeds ("New TPM2 token enrolled as key slot 1") where the bare form
failed with the same error — proving the mismatch, not the key, was the fault.

### 11.2 The initrd must carry the LUKS2 cipher stack (kernel 6.18 module names)

The same boot loop had a second, independent cause: the initrd shipped
`dm-crypt` but none of its cipher dependencies, so every unlock failed with
`crypt: unknown target type` regardless of the key. `storage.nix` now lists the
cipher/block-mode set alongside `dm_mod`/`dm_crypt`.

Two kernel-6.18 constraints shaped the list:

- **`modules-shrunk` fails hard on a root module with no `.ko`** — and 6.18 has
  no `aes.ko`, `aes_x86_64.ko`, `ecb.ko`, `sha256.ko` or `sha512.ko` (aes is
  provided by `aesni-intel` via the `aes` alias; ecb/sha are built-in). The
  ciphers that *are* modules carry their `_generic` names (`blowfish_generic`,
  `twofish_generic`, `serpent_generic`). Every name in the list is verified
  against `modules.dep`.
- **Eager loading is required for the non-device modules.** udev coldplug only
  loads device drivers, so `xts`/`cryptd`/`aesni_intel` sit in
  `boot.initrd.kernelModules` (loaded by `systemd-modules-load`, which the
  provisioner already orders after) rather than relying on demand loading in
  the initrd.

Verified as built: the 20261101.02 initrd contains `dm-mod`, `dm-crypt`,
`aesni-intel`, `xts`, `cryptd`, `cbc`, `lrw`, `af_alg`, `algif_skcipher` and
the `*_generic` ciphers, and its `modules-load.d` conf carries exactly the
eager list (`aesni_intel btrfs cryptd dm-crypt dm-mod dm_mod tpm_crb tpm_tis
xts`).

### 11.3 A locked volume is identified by its LUKS2 label, then its contents

`luksFormat` stamps `--label lemonseed-user-data`; the installer reads the
label keylessly (`luksDump`, `disks.luks_label`) so the options page can offer
preserve on a locked volume before any password is presented. The offer is
additionally gated on content: `luks_preserved_identity` trial-unlocks with the
candidate Hardware password and checks `@etc`/`@var` plus the storage identity
file, so an empty LUKS2 container (a failed previous install) is refused with
the erase path instead of a preserve that would preserve nothing.

### 11.4 The TPM2 token plugin lives off cryptsetup's default search path

`cryptsetup open --token-only …` dlopens
`libcryptsetup-token-systemd-tpm2.so` from systemd's libdir. Both the installer
web service and the initrd/stage-2 provisioner units carry
`LD_LIBRARY_PATH=${systemd}/lib/cryptsetup`; without it the token path fails
with `cannot open shared object file` even on a healthy TPM (round-3 finding).

### 11.5 The token attempt must use cryptsetup's --token-only, never --tpm2-device

`--tpm2-device=auto` is a **systemd-cryptsetup** option; plain `cryptsetup`
(2.8.6) has no such option and refuses it. The round-5 bare-metal run
therefore prompted for the Hardware password on **every** boot: the token
attempt died on an unknown option before touching the TPM, and the passphrase
fallback (which worked) masked it. The install-side "prove the seal" never
caught it because it already used the valid `--token-only` form. All cryptsetup
invocations now use `--token-only`, the token plugin coming from systemd's
libdir via the unit's `LD_LIBRARY_PATH` (§11.4), and a failed token attempt
logs cryptsetup's stderr before the passphrase fallback so the next failure is
diagnosable in the journal.

This is also the answer to "why not NixOS's native LUKS path"
(`boot.initrd.luks.devices` → initrd crypttab → `systemd-cryptsetup@.service`,
which auto-detects cryptenroll tokens and ships the plugin itself): the native
path needs the LUKS device known at build time, while our installer is
hardware-agnostic — the container is created at install time, so its UUID is
only known then. Keeping the provisioner also preserves the bounded-retry UX
(§3.7), which the native path's ask-password flow would have to reimplement.
The native path becomes attractive again under Secure Boot (below), where
`rd.luks.uuid=` on an installer-seeded kernel line could carry the per-install
UUID.

### 11.6 Secure Boot is not required for the token; it is the follow-up

With Secure Boot off, PCR 7 is constant, so the seal survives A/B switches and
reboots — the every-boot prompt was §11.5, not a PCR mismatch
(`systemd-analyze has-tpm2` = yes; `ConditionSecurity=measured-uki` being
unmet only skips `systemd-tpm2-setup`, which is harmless because
cryptenroll created the persistent SRK at install). Secure Boot via
**Lanzaboote** remains the hardening follow-up: a signed UKI makes
`measured-uki` true, gives §3.3's PCR-7 residual a real answer, and would let
the native crypttab path (§11.5) replace the provisioner's unlock branch.
Backlogged as *Alternatives to TPM* work (§9.4).

### 11.7 Validation state

`make test` green (996 tests), `make lint` clean, `make release` clean at
20261101.06 (test-signed, self-verified) and QEMU API smoke green after the
two §11.5/§11.6 fixes. Bare metal: the round-5 drill confirmed the Hardware
password unlocks the volume (the §11.1 key agreement) and root/tty2, the
Settings reveal, and preserve-reinstall all work; the remaining every-boot
prompt is §11.5's fix, pending the next bare-metal round.
