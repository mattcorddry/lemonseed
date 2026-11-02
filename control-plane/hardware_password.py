"""The Hardware password: adoption, derived material, and the admin reveal (M8 P7).

One secret, three uses (ENCRYPTED-USER-DATA.md §5): the LUKS recovery
passphrase in keyslot 0, the GRUB superuser password, and the root console
password. The installer generates it, seals the TPM token, and leaves a
plaintext bootstrap file at ``/var/lib/lemonseed/hardware-password``; the CP
adopts it at first boot and then:

* stores the plaintext in ``cp_meta`` (D27 — **provisional**, D-E6: everything
  except :func:`reveal` must work when this row is absent);
* writes ``/var/lib/lemonseed/grub-hardware.pbkdf2`` (0600) — the GRUB
  ``pbkdf2`` line the installer/updater tooling seat on the ESP;
* writes ``/var/lib/lemonseed/root-password-hash`` (0600) — the SHA-512 crypt
  hash ``users.users.root.hashedPasswordFile`` reads on every activation
  (``users.mutableUsers`` is false, so this is the only way a runtime-set root
  hash survives), and applies the same hash to ``/etc/shadow`` so boot 1's
  console works without waiting for a second activation;
* deletes the plaintext bootstrap file — from then on the database is the
  only copy (§1's stated caveat).

The SHA-512 crypt hash is computed in pure Python: the appliance Python no
longer ships the stdlib ``crypt`` module, and the plaintext must never sit in
a process argument. ``test_hardware_password.py`` pins it to openssl-produced
known answers.
"""

from __future__ import annotations

import hashlib
import os
import syslog
import threading
import time
from pathlib import Path
from typing import Any, Optional

import db

STATE_DIR = Path("/var/lib/lemonseed")
BOOTSTRAP_FILE = STATE_DIR / "hardware-password"
PBKDF2_FILE = STATE_DIR / "grub-hardware.pbkdf2"
ROOT_HASH_FILE = STATE_DIR / "root-password-hash"
SHADOW_FILE = Path("/etc/shadow")
# cp_meta key (settings-table style: the reveal reads the row, never a file).
DB_KEY = "hardware_password"

_PBKDF2_ITERATIONS = 10000
_PBKDF2_SALT_BYTES = 16
# $6$ defaults (crypt(5)): 5000 rounds when the spec carries no rounds=.
_SHA512_CRYPT_ROUNDS = 5000
# crypt's base64 variant: LSB-first bit packing over this alphabet, chosen so
# a salt byte can never carry a ':' (field separator) or a '$' (spec marker).
_B64_CHARS = "./0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"


def sha512_crypt(password: str, salt: str, rounds: int = _SHA512_CRYPT_ROUNDS) -> str:
    """The ``$6$`` (SHA-512 crypt) hash of *password* under *salt*.

    A faithful port of Drepper's sha512-crypt (libxcrypt ``crypt-sha512.c``,
    public domain) — kept stdlib-only and argument-safe: the plaintext stays
    a Python value, never an argv entry. Known answers produced by
    ``openssl passwd -6`` are pinned in ``test_hardware_password.py``.
    """
    pw = password.encode("utf-8")
    slt = salt.encode("utf-8")
    rounds = max(1000, min(rounds, 999999999))

    # Alternate sum: SHA512(phrase + salt + phrase) — added to the first
    # context below (libxcrypt: "Compute alternate SHA512 sum with input
    # PHRASE, SALT, and PHRASE").
    alt = hashlib.sha512(pw + slt + pw).digest()

    # The real work starts: phrase + salt.
    ctx = hashlib.sha512(pw + slt)

    # "Add for any character in the phrase one byte of the alternate sum":
    # the alternate sum, recycled, covers len(phrase) bytes.
    remaining = len(pw)
    while remaining >= 64:
        ctx.update(alt)
        remaining -= 64
    ctx.update(alt[:remaining])

    # Binary representation of len(phrase): every 1 adds the alternate sum,
    # every 0 the phrase.
    remaining = len(pw)
    while remaining > 0:
        if remaining & 1:
            ctx.update(alt)
        else:
            ctx.update(pw)
        remaining >>= 1
    result = ctx.digest()

    # P byte sequence: SHA512 over the phrase repeated len(phrase) times.
    p_ctx = hashlib.sha512()
    for _ in range(len(pw)):
        p_ctx.update(pw)
    p_bytes = p_ctx.digest()

    # S byte sequence: SHA512 over the salt repeated 16 + result[0] times.
    s_ctx = hashlib.sha512()
    for _ in range(16 + result[0]):
        s_ctx.update(slt)
    s_bytes = s_ctx.digest()

    # Burn CPU cycles: rounds of mixing, with the %3 / %7 salt and phrase
    # insertions of the original specification.
    def recycled(block: bytes, length: int, sink: Any) -> None:
        """Feed *block* repeated to cover *length* bytes (the recycled-bytes
        helper of the reference implementation)."""
        while length >= 64:
            sink(block)
            length -= 64
        if length:
            sink(block[:length])

    for i in range(rounds):
        ctx = hashlib.sha512()
        if i & 1:
            recycled(p_bytes, len(pw), ctx.update)
        else:
            ctx.update(result)
        if i % 3 != 0:
            recycled(s_bytes, len(slt), ctx.update)
        if i % 7 != 0:
            recycled(p_bytes, len(pw), ctx.update)
        if i & 1:
            ctx.update(result)
        else:
            recycled(p_bytes, len(pw), ctx.update)
        result = ctx.digest()

    prefix = f"$6${salt}$"
    if rounds != _SHA512_CRYPT_ROUNDS:
        prefix = f"$6$rounds={rounds}${salt}$"
    return prefix + _encode_digest(result)


def _encode_digest(result: bytes) -> str:
    """crypt(5)'s byte permutation over the 64-byte digest, base64'd in
    24-bit groups LSB-first (the ``b64_from_24bit`` calls of the reference
    implementation), 86 characters total."""
    groups = (
        (0, 21, 42), (22, 43, 1), (44, 2, 23), (3, 24, 45),
        (25, 46, 4), (47, 5, 26), (6, 27, 48), (28, 49, 7),
        (50, 8, 29), (9, 30, 51), (31, 52, 10), (53, 11, 32),
        (12, 33, 54), (34, 55, 13), (56, 14, 35), (15, 36, 57),
        (37, 58, 16), (59, 17, 38), (18, 39, 60), (40, 61, 19),
        (62, 20, 41),
    )
    out: list[str] = []
    for b2, b1, b0 in groups:
        w = (result[b2] << 16) | (result[b1] << 8) | result[b0]
        for _ in range(4):
            out.append(_B64_CHARS[w & 0x3F])
            w >>= 6
    # The final 2 characters encode the last byte alone.
    w = result[63]
    for _ in range(2):
        out.append(_B64_CHARS[w & 0x3F])
        w >>= 6
    return "".join(out)


def make_pbkdf2_line(password: str) -> str:
    """One ``grub.pbkdf2.sha512…`` line for the ESP ``grub/hardware-params``.

    GRUB's format is self-describing, so this is byte-compatible with
    ``grub-mkpasswd-pbkdf2`` output without shelling out to it (which wants a
    TTY).
    """
    salt = os.urandom(_PBKDF2_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac(
        "sha512", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS
    )
    return (
        f"grub.pbkdf2.sha512.{_PBKDF2_ITERATIONS}.{salt.hex()}.{digest.hex()}"
    )


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def _set_root_shadow(hash_: str) -> bool:
    """Rewrite root's ``/etc/shadow`` password field with *hash_*.

    The declarative ``hashedPasswordFile`` applies the same value on every
    activation; this direct write exists only so boot 1's console needs no
    second activation. Atomic (tmp + replace), and the value on the wire is
    the hash, never the plaintext.
    """
    try:
        lines = SHADOW_FILE.read_text(encoding="utf-8").splitlines(keepends=True)
        out = []
        found = False
        for line in lines:
            if line.startswith("root:"):
                fields = line.rstrip("\n").split(":", 8)
                fields[1] = hash_
                out.append(":".join(fields) + "\n")
                found = True
            else:
                out.append(line)
        if not found:
            return False
        tmp = SHADOW_FILE.with_name(SHADOW_FILE.name + ".tmp")
        tmp.write_text("".join(out), encoding="utf-8")
        os.chmod(tmp, 0o640)
        tmp.replace(SHADOW_FILE)
        return True
    except (OSError, IndexError) as exc:
        syslog.syslog(
            syslog.LOG_WARNING,
            f"lemonseed-hardware-password: could not update /etc/shadow: {exc}",
        )
        return False


def stored_plaintext() -> Optional[str]:
    """The plaintext from the database, or ``None`` when not stored (D-E6)."""
    try:
        value = db.cp_get_meta(DB_KEY)
    except Exception:  # noqa: BLE001 — read boundary
        return None
    return value if isinstance(value, str) and value else None


def _read_bootstrap() -> str:
    try:
        return BOOTSTRAP_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _ensure_derived(plaintext: str) -> None:
    """(Re)write the two derived files when absent; never rotate the secret.

    No rotation exists in this milestone (D27), so these files are written
    once at adoption; a missing file is recreated from the same plaintext so
    the updater's ESP seating and root logins never drift from the database.
    """
    if not PBKDF2_FILE.exists():
        _write_private(PBKDF2_FILE, make_pbkdf2_line(plaintext) + "\n")
    if not ROOT_HASH_FILE.exists():
        salt = "".join(
            _B64_CHARS[b % len(_B64_CHARS)] for b in os.urandom(16)
        )
        _write_private(ROOT_HASH_FILE, sha512_crypt(plaintext, salt) + "\n")
        _set_root_shadow(sha512_crypt(plaintext, salt))


def _drop_bootstrap() -> None:
    try:
        BOOTSTRAP_FILE.unlink()
    except OSError:
        pass


def adopt() -> bool:
    """Adopt the installer's bootstrap file once; ``True`` when absorbed.

    Idempotent and safe to call repeatedly: an adopted box is a no-op beyond
    re-checking the derived files, and a box that never had the file (pre-P7
    preserve of a plain volume) is an unperturbed no-op. Failure paths are
    reported, never raised — callers are startup boundaries.
    """
    stored = stored_plaintext()
    if stored is not None:
        _ensure_derived(stored)
        _drop_bootstrap()
        return True

    plaintext = _read_bootstrap()
    if not plaintext:
        return False

    if not db.cp_set_meta(DB_KEY, plaintext):
        syslog.syslog(
            syslog.LOG_WARNING,
            "lemonseed-hardware-password: database unavailable; adoption deferred",
        )
        return False

    _ensure_derived(plaintext)
    _drop_bootstrap()
    syslog.syslog(
        syslog.LOG_INFO, "lemonseed-hardware-password: adopted at first boot"
    )
    return True


def reveal() -> Optional[str]:
    """The plaintext for the admin reveal; ``None`` means "not stored" (D-E6).

    The bootstrap is adopted lazily here too, so a control plane that started
    before Postgres still answers once the database comes up.
    """
    adopt()
    return stored_plaintext()


_worker: Optional[threading.Thread] = None
_spawn_lock = threading.Lock()


def ensure_adopted() -> None:
    """Kick the first-boot adoption pass; returns immediately.

    The database may still be starting when the control plane comes up, so
    the pass retries with a bounded backoff and gives up legibly — a later
    :func:`reveal` adopts lazily, so no path is lost, only delayed.
    """
    global _worker

    def go() -> None:
        for attempt in range(30):
            try:
                if adopt():
                    return
            except Exception as exc:  # noqa: BLE001 — startup boundary: report
                syslog.syslog(
                    syslog.LOG_WARNING,
                    f"lemonseed-hardware-password: adoption attempt failed: {exc}",
                )
            time.sleep(10)
        syslog.syslog(
            syslog.LOG_WARNING,
            "lemonseed-hardware-password: adoption gave up; the reveal adopts lazily",
        )

    with _spawn_lock:
        if _worker is not None and _worker.is_alive():
            return
        _worker = threading.Thread(target=go, name="lemonseed-hw-password", daemon=True)
        _worker.start()
