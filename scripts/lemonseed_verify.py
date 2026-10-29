#!/usr/bin/env python3
"""Release-manifest signature verification (M8 P1).

One implementation, two callers: scripts/lemonseed_update.py (the in-place
updater, packaged by nixos/packages/lemonseed-update.nix) and the installer's
vendored pipeline (installer/install.py, run from the ISO). The verifier trusts
exactly the key set baked into the running image: a ``release-keys.json`` that
lists every permitted key file explicitly — never a directory glob — next to
the key files themselves, copied into the image by
nixos/packages/release-keys.nix. Nothing is fetched at runtime.

Signatures are detached ECDSA over the exact manifest bytes — NIST P-256,
SHA-256 digest, DER-encoded ``ECDSA-Sig-Value`` — verified with
``openssl pkeyutl -verify -rawin`` — the same primitive the signer uses, so the
subprocess seam can carry other signature backends without touching a caller.
Everything here is fail-closed: a missing, malformed or
wrong-key signature is a hard refusal, never a warning, never a hash-only
fallback (D10).
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

# Default baked location; the packaged wrappers point this env var at the
# trust set baked into their own image (appliance vs installer differ).
TRUST_DIR_ENV = "LEMONSEED_RELEASE_KEYS_DIR"
TRUST_DIR_DEFAULT = "/usr/share/lemonseed/release-keys"
TRUST_MANIFEST_NAME = "release-keys.json"

# Trust-set algorithm name → verifier backend. The signer's SIGN_MODE seam
# mirrors this. ECDSA signatures are randomized (RFC 6979 is NOT used): never
# assume deterministic nonces, and never normalize an ``s`` value — either
# half-order must verify as-is.
_ALGORITHMS = ("ecdsa-p256",)

# The only signature scheme the manifest's signing block may name. One trust
# generation: the previous detached-ed25519 scheme is rejected outright, not
# merely superseded.
_SCHEME = "detached-ecdsa-p256"


class VerificationError(Exception):
    """A manifest signature did not verify. The caller must refuse the release."""


def trust_dir() -> Path:
    """Directory holding the baked trust set (release-keys.json + key files)."""
    return Path(os.environ.get(TRUST_DIR_ENV) or TRUST_DIR_DEFAULT)


def load_trust_set(directory: Path | None = None) -> list[Path]:
    """Read the baked trust manifest and return the explicit key-file paths.

    The JSON is the authority; key filenames are a human convenience and are
    never matched with a glob. A missing directory, missing or unparseable
    JSON, a non-integer set version, an unknown algorithm, an empty key list,
    or a listed file absent on disk is a hard refusal — fail-closed even to a
    broken build.
    """
    directory = Path(directory) if directory is not None else trust_dir()
    trust_path = directory / TRUST_MANIFEST_NAME
    if not trust_path.is_file():
        raise VerificationError(
            f"release trust set missing: {trust_path} does not exist — refusing"
        )
    try:
        data = json.loads(trust_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise VerificationError(
            f"release trust set unreadable: {trust_path}: {exc} — refusing"
        ) from exc
    return trust_keys(data, trust_path)


def trust_keys(data: dict[str, Any], origin: Path | str = "release-keys.json") -> list[Path]:
    """Validate a parsed trust manifest and resolve its key files on disk.

    Split from ``load_trust_set`` so unit tests can feed parsed JSON directly.
    """
    directory = Path(origin).parent if Path(origin).parent != Path(".") else Path(".")
    version = data.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise VerificationError(
            f"release trust set {origin}: 'version' must be a positive integer — refusing"
        )
    keys = data.get("keys")
    if not isinstance(keys, list) or not keys:
        raise VerificationError(
            f"release trust set {origin}: 'keys' must be a non-empty list — refusing"
        )
    resolved: list[Path] = []
    for entry in keys:
        if not isinstance(entry, dict):
            raise VerificationError(
                f"release trust set {origin}: key entries must be objects — refusing"
            )
        algorithm = entry.get("algorithm")
        if algorithm not in _ALGORITHMS:
            raise VerificationError(
                f"release trust set {origin}: unsupported algorithm {algorithm!r} — refusing"
            )
        name = entry.get("file")
        if not isinstance(name, str) or not name or name != os.path.basename(name):
            raise VerificationError(
                f"release trust set {origin}: 'file' must be a bare filename, got {name!r} — refusing"
            )
        key_path = directory / name
        if not key_path.is_file():
            raise VerificationError(
                f"release trust set {origin}: listed key {name} is missing from "
                f"{directory} — refusing"
            )
        resolved.append(key_path)
    return resolved


def _openssl_verify(key_path: Path, manifest: bytes, signature: bytes) -> tuple[bool, str]:
    """Run one openssl verify attempt. Returns (ok, stderr)."""
    with tempfile.TemporaryDirectory() as tmp:
        manifest_path = Path(tmp) / "manifest.json"
        sig_path = Path(tmp) / "manifest.json.sig"
        manifest_path.write_bytes(manifest)
        sig_path.write_bytes(signature)
        try:
            proc = subprocess.run(
                [
                    "openssl",
                    "pkeyutl",
                    "-verify",
                    "-rawin",
                    "-pubin",
                    "-inkey",
                    str(key_path),
                    "-in",
                    str(manifest_path),
                    "-sigfile",
                    str(sig_path),
                ],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, f"openssl not usable: {exc}"
    return proc.returncode == 0, (proc.stderr or "").strip()


def verify_manifest(
    manifest: bytes,
    signature: bytes,
    directory: Path | None = None,
) -> Path:
    """Verify a detached ECDSA P-256 signature over the exact manifest bytes.

    Returns the trusted key that verified, for legible logging. Every key in
    the baked trust set is attempted; any one verifying is sufficient (the
    rotation overlap window, SIGNING.md §6). Raises VerificationError when the
    trust set cannot be loaded or nothing verifies.
    """
    keys = load_trust_set(directory)
    last_error = "no signature attempt was made"
    for key_path in keys:
        ok, err = _openssl_verify(key_path, manifest, signature)
        if ok:
            return key_path
        last_error = err or "signature does not verify"
    raise VerificationError(
        f"release signature does not verify — refusing (tried "
        f"{len(keys)} key(s) from {directory or trust_dir()}: {last_error})"
    )


def verify_manifest_files(
    manifest_path: Path,
    signature_path: Path | None = None,
    directory: Path | None = None,
) -> Path:
    """Verify manifest.json against its .sig file, both already on disk.

    A missing or empty .sig is its own legible refusal — signature absence is
    never equivalent to a valid signature.
    """
    sig_path = signature_path or Path(str(manifest_path) + ".sig")
    if not sig_path.is_file():
        raise VerificationError(
            f"release signature missing: {sig_path} does not exist — refusing"
        )
    signature = sig_path.read_bytes()
    if not signature:
        raise VerificationError(f"release signature is empty: {sig_path} — refusing")
    try:
        manifest = manifest_path.read_bytes()
    except OSError as exc:
        raise VerificationError(f"manifest unreadable: {manifest_path}: {exc}") from exc
    return verify_manifest(manifest, signature, directory)


def check_signing_metadata(
    manifest: dict[str, Any], directory: Path | None = None
) -> None:
    """Cross-check the signed manifest's own signing block, legibly.

    Called after the signature has verified: the metadata is inside the signed
    bytes, so a mismatch means the signer recorded something the trust set does
    not carry (e.g. a key retired from the image) — worth naming, not silently
    passing. ``directory`` must be the same trust set the signature verified
    against — the publisher's ``--trust-dir`` override, else the baked default.
    """
    signing = manifest.get("signing") or {}
    if not signing.get("enabled"):
        raise VerificationError(
            "release manifest says signing is not enabled — refusing an unsigned build"
        )
    scheme = signing.get("scheme")
    if scheme != _SCHEME:
        raise VerificationError(
            f"release manifest names signature scheme {scheme!r}, expected "
            f"{_SCHEME!r} — refusing"
        )
    public_key = signing.get("public_key")
    if public_key:
        trust = directory or trust_dir()
        if not (trust / str(public_key)).is_file():
            raise VerificationError(
                f"release manifest names signing key {public_key!r} but the baked "
                f"trust set at {trust} does not contain it — refusing"
            )
