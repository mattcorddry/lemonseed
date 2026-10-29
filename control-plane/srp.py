"""SRP-6a arithmetic for portal sign-in (M7 P2).

Why this exists: the appliance is HTTP-only forever (no FQDN, no usable CA), so
the previous design put the plaintext password in a JSON body where anyone on the
LAN could read it. SRP-6a is the one PAKE that needs no dependencies and no
build pipeline — native ``pow(g, x, N)`` here, native ``BigInt`` in the browser —
and it removes the credential from the wire. See ``documentation/roadmap/M7/PLAN.md`` D1 for
the option ranking, and note what SRP does **not** buy: the session cookie is
still a bearer token on plaintext HTTP, and an active on-path attacker can still
rewrite the login page.

Deliberately stdlib-only, and deliberately **not** a general-purpose SRP library:

* The group is the fixed RFC 5054 2048-bit prime, never generated. RFC 5054 §3.2
  is explicit that trusting ``N`` comes from matching a published list, not from
  checking it, so the hex is transcribed verbatim and pinned by a KAT.
* Every primitive takes ``(n, g, hash_name)`` explicitly. That seam exists for
  one reason: RFC 5054's Appendix B vectors are SHA-1 over the 1024-bit group,
  so driving the *same* code with those arguments is what proves our formulas
  match the specification. Production binds SHA-256 + 2048 via ``SERVER_GROUP``.
* The two SRP-6a checks are enforced on both sides. RFC 5054 §2.5.3/§2.5.4 make
  ``B % N != 0`` a client obligation and ``A % N != 0`` a server one; ``u != 0``
  is the small-subgroup check that is the entire difference between 6 and 6a.

M1/M2 come from RFC 2945's definition, not from a published vector: RFC 5054
Appendix B stops at the premaster secret and publishes no evidence-message
vectors. They are covered by cross-implementation agreement with the browser
client (``test_srp_client_js.py``), which is the honest form of coverage here.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Optional

# RFC 5054 Appendix A, group 3 (2048-bit) and group 1 (1024-bit). Transcribed
# verbatim from the RFC; ``test_srp.py`` re-reads this text against the published
# hex so a fat-fingered digit cannot survive ``make test``. The 1024-bit group
# exists only to run Appendix B and is refused by the handshake API.
GROUP_HEX = {
    "2048": """
        AC6BDB41 324A9A9B F166DE5E 1389582F AF72B665 1987EE07 FC319294
        3DB56050 A37329CB B4A099ED 8193E075 7767A13D D52312AB 4B03310D
        CD7F48A9 DA04FD50 E8083969 EDB767B0 CF609517 9A163AB3 661A05FB
        D5FAAAE8 2918A996 2F0B93B8 55F97993 EC975EEA A80D740A DBF4FF74
        7359D041 D5C33EA7 1D281E44 6B14773B CA97B43A 23FB8016 76BD207A
        436C6481 F1D2B907 8717461A 5B9D32E6 88F87748 544523B5 24B0D57D
        5EA77A27 75D2ECFA 032CFBDB F52FB378 61602790 04E57AE6 AF874E73
        03CE5329 9CCC041C 7BC308D8 2A5698F3 A8D0C382 71AE35F8 E9DBFBB6
        94B5C803 D89F7AE4 35DE236D 525F5475 9B65E372 FCD68EF2 0FA7111F
        9E4AFF73
    """,
    "1024": """
        EEAF0AB9 ADB38DD6 9C33F80A FA8FC5E8 60726187 75FF3C0B 9EA2314C
        9C256576 D674DF74 96EA81D3 383B4813 D692C6E0 E0D5D8E2 50B98BE4
        8E495C1D 6089DAD1 5DC7D7B4 6154D6B6 CE8EF4AD 69B15D49 82559B29
        7BCF1885 C529F566 660E57EC 68EDBC3C 05726CC0 2FD4CBF4 976EAA9A
        FD5138FE 8376435B 9FC61D2F C0EB06E3
    """,
}


def _prime(bits: str) -> int:
    return int("".join(GROUP_HEX[bits].split()), 16)


@dataclass(frozen=True)
class Group:
    """Group parameters plus the hash every derived value is computed over."""

    name: str
    n: int
    g: int
    hash_name: str

    @property
    def byte_len(self) -> int:
        """Width of PAD(): the implicitly-converted byte length of N (RFC 5054 §2.1)."""
        return (self.n.bit_length() + 7) // 8


# The one group the appliance will authenticate with. 1024-bit is present solely
# so the published vectors exercise this code; nothing in the request path can
# select it (``test_handshake_refuses_any_other_group`` pins that).
SERVER_GROUP = Group("rfc5054-2048-sha256", _prime("2048"), 2, "sha256")
VECTOR_GROUP = Group("rfc5054-1024-sha1", _prime("1024"), 2, "sha1")

SALT_BYTES = 16
# RFC 5054 §3.1: private values SHOULD be at least 256 bits.
PRIVATE_BITS = 256
# A pending handshake is single-use and short-lived. Long enough for a browser
# round trip on a loaded TCG guest, short enough that an abandoned one is gone
# before a person retypes their password.
PENDING_TTL_SECONDS = 120
# Memory guard for the pending store: challenges are unauthenticated, so an
# attacker must not be able to grow the dict without bound.
PENDING_MAX = 1024

# Labeled subkey derivation for the unknown-user fold salt (see ``fold_salt``).
FOLD_SALT_LABEL = b"lemonseed-srp-fold-salt"


class SrpError(ValueError):
    """A handshake value failed validation. The caller answers 401, never 500."""


# --- encoding ---------------------------------------------------------------


def digest(hash_name: str, *parts: bytes) -> bytes:
    h = hashlib.new(hash_name)
    for part in parts:
        h.update(part)
    return h.digest()


def hash_to_int(hash_name: str, *parts: bytes) -> int:
    return int.from_bytes(digest(hash_name, *parts), "big")


def to_bytes(value: int) -> bytes:
    """RFC 5054 §2.1 implicit conversion: big-endian, most-significant byte non-zero.

    ``A``, ``B`` and ``S`` are hashed in this form; only ``u`` uses PAD(). Mixing
    the two is the classic way to produce an implementation that agrees with
    itself and with nothing else.
    """
    if value == 0:
        return b""
    return value.to_bytes((value.bit_length() + 7) // 8, "big")


def pad(value: int, group: Group) -> bytes:
    """PAD(): the same integer left-padded with zeros to the byte length of N."""
    return value.to_bytes(group.byte_len, "big")


# --- the protocol -----------------------------------------------------------


def multiplier(group: Group) -> int:
    """``k = H(N | PAD(g))`` — the SRP-6 multiplier that blocks a 2-for-1 attack."""
    return hash_to_int(group.hash_name, to_bytes(group.n), pad(group.g, group))


def x_from_password(group: Group, salt: bytes, identity: str, password: str) -> int:
    """``x = H(s | H(I | ":" | P))`` (RFC 5054 §2.4).

    Binding the identity inside the inner hash is what stops one verifier being
    portable across two accounts, so the username is part of the secret, not
    just a lookup key.
    """
    inner = digest(group.hash_name, identity.encode("utf-8"), b":", password.encode("utf-8"))
    return hash_to_int(group.hash_name, salt, inner)


def make_verifier(group: Group, x: int) -> int:
    """``v = g^x % N``. The stored half: knowing it does not let you log in."""
    return pow(group.g, x, group.n)


def new_salt() -> bytes:
    return secrets.token_bytes(SALT_BYTES)


def new_private(group: Group) -> int:
    """A fresh private exponent, ``>= 256`` bits and strictly below ``N``."""
    while True:
        value = int.from_bytes(secrets.token_bytes(PRIVATE_BITS // 8), "big")
        if 0 < value < group.n:
            return value


def client_public(group: Group, a: int) -> int:
    """``A = g^a % N``."""
    return pow(group.g, a, group.n)


def server_public(group: Group, k: int, verifier: int, b: int) -> int:
    """``B = k*v + g^b % N`` (RFC 5054 §2.5.3)."""
    return (k * verifier + pow(group.g, b, group.n)) % group.n


def scrambler(group: Group, a_public: int, b_public: int) -> int:
    """``u = H(PAD(A) | PAD(B))`` — the scrambling parameter, checked non-zero."""
    return hash_to_int(group.hash_name, pad(a_public, group), pad(b_public, group))


def client_secret(
    group: Group,
    k: int,
    salt: bytes,
    identity: str,
    password: str,
    a_private: int,
    b_public: int,
) -> tuple[int, int]:
    """``S = (B - k*g^x)^(a + u*x) % N``. Returns ``(S, u)``.

    Python's ``%`` always returns a non-negative residue, which matters here:
    ``B - k*g^x`` is negative for a wrong password, and a language with
    truncating modulo would silently compute a different ``S``.
    """
    x = x_from_password(group, salt, identity, password)
    u = scrambler(group, client_public(group, a_private), b_public)
    if u == 0:
        raise SrpError("scrambler is zero")
    base = (b_public - k * pow(group.g, x, group.n)) % group.n
    return pow(base, a_private + u * x, group.n), u


def server_secret(group: Group, a_public: int, verifier: int, u: int, b_private: int) -> int:
    """``S = (A * v^u)^b % N`` (RFC 5054 §2.6)."""
    return pow(a_public * pow(verifier, u, group.n), b_private, group.n)


def session_key(group: Group, s: int) -> bytes:
    """``K = H(S)`` — the shared key. Unused for secrecy today, but it is what
    lets ``M2`` prove the server holds the verifier."""
    return digest(group.hash_name, to_bytes(s))


def client_proof(group: Group, a_public: int, b_public: int, s: int) -> bytes:
    """``M1 = H(A | B | S)`` (RFC 2945 §2.4): proof of password knowledge."""
    return digest(group.hash_name, to_bytes(a_public), to_bytes(b_public), to_bytes(s))


def server_proof(group: Group, a_public: int, m1: bytes, key: bytes) -> bytes:
    """``M2 = H(A | M1 | K)`` (RFC 2945 §2.4): proof the server holds the verifier."""
    return digest(group.hash_name, to_bytes(a_public), m1, key)


# --- validation -------------------------------------------------------------


def parse_group_value(raw: object, group: Group, what: str) -> int:
    """Hex on the wire; reject anything that is not a group element.

    ``>= N`` is rejected rather than reduced: accepting it would let a peer
    present two names for one value, and the checks below are only meaningful on
    a canonical encoding.
    """
    text = str(raw or "").strip()
    if not text or len(text) > group.byte_len * 2 + 8:
        raise SrpError(f"{what} is missing or absurdly long")
    try:
        value = int(text, 16)
    except ValueError as exc:
        raise SrpError(f"{what} is not hexadecimal") from exc
    if value <= 0 or value >= group.n:
        raise SrpError(f"{what} is outside 1..N-1")
    return value


def check_client_public(group: Group, a_public: int) -> None:
    """RFC 5054 §2.5.4: the server MUST abort if ``A % N == 0``."""
    if a_public % group.n == 0:
        raise SrpError("client public value is 0 mod N")


def check_server_public(group: Group, b_public: int) -> None:
    """RFC 5054 §2.5.3: the client MUST abort if ``B % N == 0``.

    Enforced here too so a test can prove the browser's refusal has a mirror,
    and so a future change cannot quietly drop the check.
    """
    if b_public % group.n == 0:
        raise SrpError("server public value is 0 mod N")


def parse_proof(raw: object, group: Group) -> bytes:
    """A hash digest, hex-encoded, with a length ceiling so this cannot be a blob."""
    text = str(raw or "").strip()
    limit = len(digest(group.hash_name)) * 2
    if len(text) != limit:
        raise SrpError(f"proof must be {limit} hex characters")
    try:
        value = bytes.fromhex(text)
    except ValueError as exc:
        raise SrpError("proof is not hexadecimal") from exc
    return value


# --- enrollment -------------------------------------------------------------


def enroll(group: Group, salt_hex: object, verifier_hex: object) -> tuple[bytes, int]:
    """Validate the ``(salt, verifier)`` a browser produced for a new password.

    This is the whole server-side half of enrollment: the plaintext password
    never reaches the appliance, so the CP cannot re-derive the verifier and can
    only check that what arrived is a well-formed group element. The same is true
    of every SRP server — a verifier is a public value the moment it is sent.
    """
    salt_text = str(salt_hex or "").strip()
    if len(salt_text) != SALT_BYTES * 2:
        raise SrpError(f"salt must be {SALT_BYTES * 2} hex characters")
    try:
        salt = bytes.fromhex(salt_text)
    except ValueError as exc:
        raise SrpError("salt is not hexadecimal") from exc
    verifier = parse_group_value(verifier_hex, group, "verifier")
    if verifier == 0:
        raise SrpError("verifier is zero")
    return salt, verifier


def make_enrollment(
    group: Group, identity: str, password: str, salt: Optional[bytes] = None
) -> tuple[bytes, int]:
    """The client half of enrollment: ``(salt, v)`` for a brand-new password.

    Used by ``srp_client.py`` and by the QEMU smoke test, which is the only place
    a real password is known to Python. The browser runs the same two lines in
    ``SRP_CLIENT_JS``; ``test_srp_client_js.py`` proves they agree.
    """
    salt = new_salt() if salt is None else salt
    x = x_from_password(group, salt, identity, password)
    return salt, make_verifier(group, x)


def fold_salt(secret: bytes, identity: str) -> bytes:
    """Deterministic salt for a name with no verifier (RFC 5054 §2.5.1.3).

    Simulating an unknown account requires the *same* salt every time, or two
    challenges for the same name would themselves be the tell. It is an HMAC
    under a secret the browser never sees.

    The label is a derivation, not a second use of the session secret as-is: one
    secret, two distinct subkeys, so a leak of one does not flatten the other.
    """
    derived = hmac.new(secret, FOLD_SALT_LABEL, hashlib.sha256).digest()
    return hmac.new(derived, identity.encode("utf-8"), hashlib.sha256).digest()[:SALT_BYTES]


# --- pending handshakes -----------------------------------------------------


@dataclass
class Pending:
    """One in-flight handshake, keyed by an opaque id the client echoes back.

    ``b_private`` is the only secret here; it lives in process memory and is
    never persisted or logged. A control-plane restart therefore costs a client
    one retry, which is the correct trade against writing ``b`` to disk next to
    the verifiers.

    ``b_public`` is kept so ``finish_handshake`` can derive ``u`` and ``S``
    without re-reading the row: the value the client was told must be the exact
    value the server hashes.
    """

    username: str
    verifier: int
    b_public: int
    b_private: int
    expires_at: float


class PendingStore:
    """Short-lived, single-use handshakes.

    Single-use is what makes a replayed ``M1`` fail: the first ``finish`` pops
    the entry, so the second finds nothing. It also bounds the damage of a
    leaked challenge id.

    In-process on purpose. The control plane is one ``ThreadingHTTPServer``
    process, a pending handshake is worth nothing on its own, and 120 seconds of
    state is not worth a Valkey round trip on the sign-in path.
    """

    # Expiry is stamped by ``begin_handshake`` (which owns the ttl), not here, so
    # a stored ``Pending`` can never disagree with the value it was created with.
    def __init__(self, max_entries: int = PENDING_MAX) -> None:
        self._max = max_entries
        self._lock = threading.Lock()
        self._items: dict[str, Pending] = {}

    def put(self, key: str, pending: Pending, now: Optional[float] = None) -> None:
        moment = time.monotonic() if now is None else now
        with self._lock:
            self._prune(moment)
            # Eviction is oldest-first and only over the cap: an unauthenticated
            # flood must not be able to grow this without bound.
            while len(self._items) >= self._max:
                oldest = min(self._items, key=lambda k: self._items[k].expires_at)
                del self._items[oldest]
            self._items[key] = pending

    def take(self, key: str, now: Optional[float] = None) -> Optional[Pending]:
        """Pop a pending handshake. Expired or unknown both return None."""
        moment = time.monotonic() if now is None else now
        with self._lock:
            pending = self._items.pop(key, None)
        if pending is None or pending.expires_at <= moment:
            return None
        return pending

    def _prune(self, now: float) -> None:
        for key in [k for k, v in self._items.items() if v.expires_at <= now]:
            del self._items[key]

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)


PENDING = PendingStore()


def begin_handshake(
    group: Group,
    username: str,
    verifier: int,
    now: Optional[float] = None,
    ttl: float = PENDING_TTL_SECONDS,
) -> tuple[int, Pending]:
    """Server's first move. Returns ``(b_public, pending)``.

    Kept as a helper so the HTTP handler cannot reorder the checks: the
    multiplier is computed, ``B`` is formed, and ``B % N != 0`` is checked here.
    ``k`` is deliberately *not* returned; it has no use once ``B`` exists, and a
    value that is easy to pass onward is a value that eventually gets logged.
    """
    k = multiplier(group)
    b = new_private(group)
    b_public = server_public(group, k, verifier, b)
    check_server_public(group, b_public)
    moment = time.monotonic() if now is None else now
    pending = Pending(
        username=username,
        verifier=verifier,
        b_public=b_public,
        b_private=b,
        expires_at=moment + ttl,
    )
    return b_public, pending


def finish_handshake(
    group: Group, pending: Pending, a_public: int, client_proof_value: bytes
) -> bytes:
    """Server's second move. Returns ``M2``. Raises ``SrpError`` to mean 401.

    Bad ``A``, zero ``u`` and a wrong password all raise the same exception type,
    because the caller's answer is identical for all three and a distinguishable
    error would be a free oracle. Note that a wrong password is never *compared*
    to anything: a wrong ``x`` yields a wrong ``S``, hence a wrong ``M1``, and the
    comparison below is what fails.

    The comparison is constant-time. ``M1`` is a digest of a secret, so a timing
    leak here is not obviously exploitable, but there is no reason to offer one.
    """
    check_client_public(group, a_public)
    u = scrambler(group, a_public, pending.b_public)
    if u == 0:
        raise SrpError("scrambler is zero")
    secret = server_secret(group, a_public, pending.verifier, u, pending.b_private)
    expected = client_proof(group, a_public, pending.b_public, secret)
    if not hmac.compare_digest(expected, client_proof_value):
        raise SrpError("client proof mismatch")
    return server_proof(group, a_public, expected, session_key(group, secret))
