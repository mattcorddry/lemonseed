"""The client half of an SRP-6a handshake, in Python.

Two callers, neither of them a browser:

* ``test_srp.py`` drives a full in-process handshake against ``srp.py``. If the
  two halves of one spec disagree, that test fails, which is the cheapest
  possible check that the formulas were transcribed consistently.
* ``scripts/qemu-api-smoke-test.sh`` drives the real appliance over HTTP, so the
  QEMU gate exercises SRP end to end instead of being deleted along with the
  plaintext login it used to post.

The browser runs the same math in JavaScript (``SRP_CLIENT_JS`` in ``stub.py``).
Reusing *this* module in a browser would be pointless, but reusing it in tests is
not circular: ``test_srp_client_js.py`` checks the JS against published SHA-256
vectors and against ``hashlib``, and the RFC 5054 vectors check the formulas.
Correctness reaches the browser transitively through those two, not through here.

Run directly for ad-hoc debugging::

    PYTHONPATH=control-plane python3 -m srp_client enroll alice hunter2
    PYTHONPATH=control-plane python3 -m srp_client auth <salt_hex> <b_hex> alice hunter2
"""

from __future__ import annotations

import hmac
import sys

import srp
from srp import Group, SrpError


def enroll(identity: str, password: str, group: Group = srp.SERVER_GROUP) -> tuple[str, str]:
    """Produce the ``(salt_hex, verifier_hex)`` a setup form should post.

    This is the only moment a password exists on the client, and what leaves is
    derived from it one ways: ``x`` is folded into ``v = g^x`` and is never
    returned.
    """
    salt, verifier = srp.make_enrollment(group, identity, password)
    return salt.hex(), f"{verifier:x}"


class ClientHandshake:
    """Sign-in state for one challenge. Mirrors ``SRP_CLIENT_JS`` step for step.

    Order matters and is enforced by the constructor: validate ``B`` *before*
    using it, because RFC 5054 §2.5.3 makes ``B % N != 0`` a client obligation,
    and an implementation that skips it will happily compute a ``K`` that only it
    believes in.
    """

    def __init__(
        self,
        identity: str,
        password: str,
        salt_hex: str,
        b_hex: str,
        group: Group = srp.SERVER_GROUP,
    ) -> None:
        self.group = group
        self.identity = identity
        salt_text = str(salt_hex or "").strip()
        if len(salt_text) != srp.SALT_BYTES * 2:
            raise SrpError(f"salt must be {srp.SALT_BYTES * 2} hex characters")
        try:
            self.salt = bytes.fromhex(salt_text)
        except ValueError as exc:
            raise SrpError("salt is not hexadecimal") from exc

        self.b_public = srp.parse_group_value(b_hex, group, "server public value")
        srp.check_server_public(group, self.b_public)

        self.a_private = srp.new_private(group)
        self.a_public = srp.client_public(group, self.a_private)

        self.secret, self.u = srp.client_secret(
            group,
            srp.multiplier(group),
            self.salt,
            identity,
            password,
            self.a_private,
            self.b_public,
        )
        self.key = srp.session_key(group, self.secret)
        self.m1 = srp.client_proof(group, self.a_public, self.b_public, self.secret)
        self._expected_m2 = srp.server_proof(group, self.a_public, self.m1, self.key)

    # --- what goes on the wire ---------------------------------------------

    @property
    def a_hex(self) -> str:
        return f"{self.a_public:x}"

    @property
    def m1_hex(self) -> str:
        return self.m1.hex()

    # --- and the server's reply --------------------------------------------

    def check(self, m2_hex: str) -> bool:
        """True only if the server proved it holds the verifier.

        A sign-in that skips this is not authenticated: it has only proved
        something to the server.
        """
        try:
            candidate = srp.parse_proof(m2_hex, self.group)
        except SrpError:
            return False
        return hmac.compare_digest(self._expected_m2, candidate)

    def expected_m2_hex(self) -> str:
        return self._expected_m2.hex()


def main(argv: list[str]) -> int:
    """Print the values a shell test needs. Deliberately tiny; see the docstring."""
    if len(argv) == 3 and argv[0] == "enroll":
        _, identity, password = argv
        salt, verifier = enroll(identity, password)
        print(f"{salt} {verifier}")
        return 0
    if len(argv) == 5 and argv[0] == "auth":
        _, salt_hex, b_hex, identity, password = argv
        try:
            hs = ClientHandshake(identity, password, salt_hex, b_hex)
        except SrpError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print(f"{hs.a_hex} {hs.m1_hex} {hs.expected_m2_hex()}")
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
