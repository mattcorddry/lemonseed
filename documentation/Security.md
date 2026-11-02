# Lemonseed Security

This document explains, in plain language, what Lemonseed does to keep your
data safe, what risks remain, and what you can do about them. It is an
overview, not a design record: the engineering detail lives in
[DESIGN.md](./DESIGN.md).

## What Lemonseed protects, and how

### Your password never leaves your browser

Lemonseed is reached over plain HTTP on your home network (for example
`http://larry.local`), and it always will be — a `.local` name cannot get a
trusted certificate, and self-signed certificates are worse to live with than
no TLS at all. Because the transport cannot be trusted, **your password is
never sent to the appliance**. Signing in uses a challenge–response method
(SRP-6a) that proves to the appliance that you know the password without ever
transmitting it — not even in hashed form. The appliance could not leak your
password even by accident.

Two consequences worth knowing:

- Signing in requires JavaScript; there is no server-side fallback.
- The session cookie is still a bearer token over plain HTTP. An attacker
  already on your network could impersonate the login page rather than just
  listen to it. Anyone with physical access to the box owns it.

### Software updates are signed

Every Lemonseed release is published with a digital signature produced with a
private key that never leaves our signing environment. Before your appliance
trusts anything it downloads — before it even trusts a checksum — it verifies
that signature against public keys baked into the appliance itself. A fake
update, or a corrupted one, is refused before a single byte is installed.

### Your data is encrypted at rest

User data — accounts, chat history, databases, models — is stored on an
encrypted partition. The encryption key lives in your PC's **trusted platform
module (TPM)**, sealed so that it only unlocks on that machine, for that
installation. Pulling the disk out and reading it elsewhere, or moving it to
another PC, yields nothing readable.

The key that recovers this if the TPM fails or is replaced is the **Hardware
password**, generated when Lemonseed is installed. Write it down and keep it
away from the computer. You can view it at any time in the web portal under
**Settings → Advanced**. If you lose both the Hardware password and the TPM
seal, the only recovery is a reinstall that erases user data — so the password
is worth the trouble of a note in a drawer.

### The console is locked down

The Linux console on the appliance (a keyboard attached directly to the box)
does not hand out a free root shell. The root account there uses the Hardware
password; your ordinary Lemonseed accounts only sign in through the web
portal. Choosing a boot target — for example rolling back to the previous
version — never requires a password, but altering boot parameters does.

## What is still risky, and what you can do

Honest list; none of these are hidden, and most have practical mitigations.

| Risk | What it means | What you can do |
| ---- | ------------- | --------------- |
| Someone boots the PC from other media | With Secure Boot off, an attacker who boots a USB stick from the firmware menu on the *same, intact* PC can run software that reads the disk while the system is up — or attack the running system. | Set a **BIOS supervisor password** and disable **legacy/CSM boot** in the firmware setup, so only the installed drive (and a stick you choose) can boot. |
| Physical access to a running box | While Lemonseed is powered on and unlocked, the data is readable by anything running on it. | Treat the box like an unlocked computer: put it somewhere physically controlled, and power it off when it is out of your hands. |
| An attacker on your network | Plain HTTP means someone on your LAN could impersonate the login page or the appliance. | Keep the appliance on a home network you control (a standard WPA2/WPA3-protected router is enough); avoid using it over guest or shared networks. |
| A household admin | An admin can view the Hardware password in the portal and manage all accounts. Admins can do a lot by design. | Only promote household members you trust to admin. |
| A stolen disk | Encrypted at rest — this is the case Lemonseed fully covers. | Keep the Hardware password away from the disk. |
| Firmware vulnerabilities | TPM and boot security ultimately lean on the PC's firmware. | Install motherboard/BIOS updates from the vendor when security fixes are announced. |
| You, without the Hardware password | If the TPM seal is lost (mainboard replacement, firmware reset) and the Hardware password is lost, user data cannot be recovered. | Write the Hardware password down at install time; it is viewable in Settings → Advanced. |

## The one-paragraph version

Your password is never sent anywhere; updates are signature-verified before
they are trusted; your data is encrypted against disk theft; the console asks
for the Hardware password instead of giving away a root shell. The remaining
risks are the ones physics and firmware allow: someone with the box powered
on, someone with your home network, or someone who can boot the PC from other
media — and each of those has a mitigation above.
