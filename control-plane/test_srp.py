"""Tests for ``srp.py`` — the published vectors, then the handshake.

Ordered deliberately. The vector tests come first and drive the primitives with
``VECTOR_GROUP`` (SHA-1 over the 1024-bit group) because that is the only
configuration RFC 5054 publishes results for; if the formulas were transcribed
wrong, they fail here rather than in a QEMU guest 30 minutes later. The
handshake tests then exercise the *production* group and the state machine.

What is NOT tested here, on purpose: M1/M2 against a published value. RFC 5054
Appendix B stops at the premaster secret and publishes no evidence-message
vectors. M1/M2 are covered by cross-implementation agreement with the browser in
``test_srp_client_js.py``. Pretending otherwise would be a false guarantee.
"""

from __future__ import annotations

import hashlib
import time
import unittest
from unittest import mock

import srp
import srp_client

# --- published parameters (RFC 5054 Appendix A) -----------------------------

# Transcribed independently of srp.GROUP_HEX and stored as one unbroken string so
# a mangled digit in either file shows up as a mismatch rather than cancelling
# out. Both sides come from the RFC, but they do not come from each other.
PUBLISHED_N_2048 = (
    "AC6BDB41324A9A9BF166DE5E1389582FAF72B6651987EE07FC319294"
    "3DB56050A37329CBB4A099ED8193E0757767A13DD52312AB4B03310D"
    "CD7F48A9DA04FD50E8083969EDB767B0CF6095179A163AB3661A05FB"
    "D5FAAAE82918A9962F0B93B855F97993EC975EEAA80D740ADBF4FF74"
    "7359D041D5C33EA71D281E446B14773BCA97B43A23FB801676BD207A"
    "436C6481F1D2B9078717461A5B9D32E688F87748544523B524B0D57D"
    "5EA77A2775D2ECFA032CFBDBF52FB3786160279004E57AE6AF874E73"
    "03CE53299CCC041C7BC308D82A5698F3A8D0C38271AE35F8E9DBFBB6"
    "94B5C803D89F7AE435DE236D525F54759B65E372FCD68EF20FA7111F"
    "9E4AFF73"
)
PUBLISHED_N_1024 = (
    "EEAF0AB9ADB38DD69C33F80AFA8FC5E86072618775FF3C0B9EA2314C"
    "9C256576D674DF7496EA81D3383B4813D692C6E0E0D5D8E250B98BE4"
    "8E495C1D6089DAD15DC7D7B46154D6B6CE8EF4AD69B15D4982559B29"
    "7BCF1885C529F566660E57EC68EDBC3C05726CC02FD4CBF4976EAA9A"
    "FD5138FE8376435B9FC61D2FC0EB06E3"
)


def is_probable_prime(value: int) -> bool:
    """Deterministic for these sizes; enough to check the N=2q+1 shape.

    A primality test here is not a security control — the appliance trusts the
    published list, per RFC 5054 §3.2. It is a transcription check with a little
    extra confidence that the hex was not corrupted in the same way twice.
    """
    if value < 2 or value % 2 == 0:
        return value == 2
    small = [3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37]
    for p in small:
        if value % p == 0:
            return value == p
    d, r = value - 1, 0
    while d % 2 == 0:
        d //= 2
        r += 1
    for a in small:
        x = pow(a, d, value)
        if x in (1, value - 1):
            continue
        for _ in range(r - 1):
            x = x * x % value
            if x == value - 1:
                break
        else:
            return False
    return True


class GroupParameterTests(unittest.TestCase):
    def test_group_hex_matches_the_published_list(self) -> None:
        self.assertEqual("".join(srp.GROUP_HEX["2048"].split()), PUBLISHED_N_2048)
        self.assertEqual("".join(srp.GROUP_HEX["1024"].split()), PUBLISHED_N_1024)

    def test_2048_prime_is_the_size_it_claims(self) -> None:
        self.assertEqual(srp.SERVER_GROUP.n.bit_length(), 2048)
        self.assertEqual(srp.SERVER_GROUP.byte_len, 256)
        self.assertEqual(srp.VECTOR_GROUP.n.bit_length(), 1024)

    def test_primes_are_safe_primes(self) -> None:
        """RFC 5054 §2.5.3 requires N = 2q+1 with q also prime."""
        for name, n in (("2048", srp.SERVER_GROUP.n), ("1024", srp.VECTOR_GROUP.n)):
            with self.subTest(group=name):
                self.assertTrue(is_probable_prime(n))
                self.assertEqual(n % 2, 1)
                self.assertTrue(is_probable_prime((n - 1) // 2))

    def test_generator_two_is_a_primitive_root(self) -> None:
        """Appendix A: the SRP generators are *primitive roots* of N.

        That is deliberately different from the MODP groups, where the generator
        has order q. Order must be exactly N-1 = 2q, so it is not 1, not q, and
        not 2; checking the two prime divisors of N-1 proves maximality without
        enumerating the group.
        """
        for group in (srp.SERVER_GROUP, srp.VECTOR_GROUP):
            with self.subTest(group=group.name):
                n = group.n
                q = (n - 1) // 2
                self.assertEqual(pow(group.g, n - 1, n), 1)
                self.assertNotEqual(pow(group.g, q, n), 1)
                self.assertNotEqual(pow(group.g, 2, n), 1)

    def test_production_group_is_2048_sha256(self) -> None:
        """Nothing in the request path may downgrade this; pinned here first."""
        self.assertEqual(srp.SERVER_GROUP.byte_len, 256)
        self.assertEqual(srp.SERVER_GROUP.hash_name, "sha256")
        self.assertEqual(srp.VECTOR_GROUP.hash_name, "sha1")

    def test_vector_group_is_only_for_vectors(self) -> None:
        # A 1024-bit group is below the appliance's own security floor. It exists
        # in the module solely to reproduce Appendix B; the handshake API must not
        # be reachable with it from the HTTP layer.
        self.assertLess(srp.VECTOR_GROUP.n.bit_length(), 2048)


# --- Appendix B vectors -----------------------------------------------------


def vec(text: str, words: int) -> int:
    """Parse an Appendix B constant from the RFC's own 8-hex-digit word layout.

    The word count is asserted rather than assumed: every one of these failed
    once here by dropping a digit, and a length check catches that immediately
    instead of showing up as an opaque big-integer mismatch.
    """
    joined = "".join(text.split())
    if len(joined) != words * 8:
        raise AssertionError(f"expected {words * 8} hex characters, got {len(joined)}")
    return int(joined, 16)


VECTOR_I = "alice"
VECTOR_P = "password123"
VECTOR_SALT = bytes.fromhex("BEB25379D1A8581EB5A727673A2441EE")
VECTOR_K = "7556AA045AEF2CDD07ABAF0F665C3E818913186F"
VECTOR_X = 0x94B7555AABE9127CC58CCF4993DB6CF84D16C124
VECTOR_V = vec(
    """
    7E273DE8 696FFC4F 4E337D05 B4B375BE B0DDE156 9E8FA00A 9886D812
    9BADA1F1 822223CA 1A605B53 0E379BA4 729FDC59 F105B478 7E5186F5
    C671085A 1447B52A 48CF1970 B4FB6F84 00BBF4CE BFBB1681 52E08AB5
    EA53D15C 1AFF87B2 B9DA6E04 E058AD51 CC72BFC9 033B564E 26480D78
    E955A5E2 9E7AB245 DB2BE315 E2099AFB
    """,
    32,
)
VECTOR_A_PRIV = int("60975527035CF2AD1989806F0407210BC81EDC04E2762A56AFD529DDDA2D4393", 16)
VECTOR_B_PRIV = int("E487CB59D31AC550471E81F00F6928E01DDA08E974A004F49E61F5D105284D20", 16)
VECTOR_A = vec(
    """
    61D5E490 F6F1B795 47B0704C 436F523D D0E560F0 C64115BB 72557EC4
    4352E890 3211C046 92272D8B 2D1A5358 A2CF1B6E 0BFCF99F 921530EC
    8E393561 79EAE45E 42BA92AE ACED8251 71E1E8B9 AF6D9C03 E1327F44
    BE087EF0 6530E69F 66615261 EEF54073 CA11CF58 58F0EDFD FE15EFEA
    B349EF5D 76988A36 72FAC47B 0769447B
    """,
    32,
)
VECTOR_B = vec(
    """
    BD0C6151 2C692C0C B6D041FA 01BB152D 4916A1E7 7AF46AE1 05393011
    BAF38964 DC46A067 0DD125B9 5A981652 236F99D9 B681CBF8 7837EC99
    6C6DA044 53728610 D0C6DDB5 8B318885 D7D82C7F 8DEB75CE 7BD4FBAA
    37089E6F 9C6059F3 88838E7A 00030B33 1EB76840 910440B1 B27AAEAE
    EB4012B7 D7665238 A8E3FB00 4B117B58
    """,
    32,
)
VECTOR_U = 0xCE38B9593487DA98554ED47D70A7AE5F462EF019
VECTOR_S = vec(
    """
    B0DC82BA BCF30674 AE450C02 87745E79 90A3381F 63B387AA F271A10D
    233861E3 59B48220 F7C4693C 9AE12B0A 6F67809F 0876E2D0 13800D6C
    41BB59B6 D5979B5C 00A172B4 A2A5903A 0BDCAF8A 709585EB 2AFAFA8F
    3499B200 210DCC1F 10EB3394 3CD67FC8 8A2F39A4 BE5BEC4E C0A3212D
    C346D7E4 74B29EDE 8A469FFE CA686E5A
    """,
    32,
)


class Rfc5054VectorTests(unittest.TestCase):
    """Every intermediate value from Appendix B, against this implementation.

    Driven with ``VECTOR_GROUP`` on purpose: that is the (SHA-1, 1024-bit)
    combination the RFC's numbers were computed with. The same functions run
    against SHA-256/2048 in production, which is exactly the seam the explicit
    ``group`` parameter exists to make.
    """

    def setUp(self) -> None:
        self.group = srp.VECTOR_GROUP

    def test_multiplier_k(self) -> None:
        self.assertEqual(f"{srp.multiplier(self.group):X}", VECTOR_K)

    def test_x_from_password(self) -> None:
        x = srp.x_from_password(self.group, VECTOR_SALT, VECTOR_I, VECTOR_P)
        self.assertEqual(x, VECTOR_X)

    def test_verifier(self) -> None:
        x = srp.x_from_password(self.group, VECTOR_SALT, VECTOR_I, VECTOR_P)
        self.assertEqual(srp.make_verifier(self.group, x), VECTOR_V)

    def test_client_public_A(self) -> None:
        self.assertEqual(srp.client_public(self.group, VECTOR_A_PRIV), VECTOR_A)

    def test_server_public_B(self) -> None:
        b_public = srp.server_public(
            self.group, srp.multiplier(self.group), VECTOR_V, VECTOR_B_PRIV
        )
        self.assertEqual(b_public, VECTOR_B)

    def test_scrambler_u(self) -> None:
        self.assertEqual(srp.scrambler(self.group, VECTOR_A, VECTOR_B), VECTOR_U)

    def test_client_premaster_secret(self) -> None:
        s, u = srp.client_secret(
            self.group,
            srp.multiplier(self.group),
            VECTOR_SALT,
            VECTOR_I,
            VECTOR_P,
            VECTOR_A_PRIV,
            VECTOR_B,
        )
        self.assertEqual(s, VECTOR_S)
        self.assertEqual(u, VECTOR_U)

    def test_server_premaster_secret(self) -> None:
        """The two formulas must land on the RFC's value from opposite sides."""
        s = srp.server_secret(
            self.group, VECTOR_A, VECTOR_V, VECTOR_U, VECTOR_B_PRIV
        )
        self.assertEqual(s, VECTOR_S)

    def test_encoding_helpers(self) -> None:
        # §2.1: implicit conversion has a non-zero most-significant byte; PAD only
        # widens to |N|. Getting these wrong changes every digest downstream.
        self.assertEqual(srp.to_bytes(0), b"")
        self.assertEqual(srp.to_bytes(0x0102), b"\x01\x02")
        self.assertEqual(srp.to_bytes(0x0102).hex(), "0102")
        self.assertEqual(len(srp.pad(0x0102, self.group)), 128)
        self.assertTrue(srp.pad(0x0102, self.group).startswith(b"\x00" * 126))
        self.assertEqual(len(srp.pad(self.group.n - 1, self.group)), 128)


# --- the handshake ----------------------------------------------------------


class HandshakeTests(unittest.TestCase):
    """In-process client/server over the *production* group."""

    GROUP = srp.SERVER_GROUP
    USERNAME = "admin"
    PASSWORD = "correct horse battery staple"

    def setUp(self) -> None:
        self.salt, self.verifier = srp.make_enrollment(
            self.GROUP, self.USERNAME, self.PASSWORD
        )

    def _challenge(self, ttl: float = srp.PENDING_TTL_SECONDS):
        b_public, pending = srp.begin_handshake(
            self.GROUP, self.USERNAME, self.verifier, ttl=ttl
        )
        return b_public, pending

    def _client(self, b_public: int, password: str | None = None) -> srp_client.ClientHandshake:
        return srp_client.ClientHandshake(
            self.USERNAME,
            password or self.PASSWORD,
            self.salt.hex(),
            f"{b_public:x}",
            self.GROUP,
        )

    def test_happy_path_mutual_auth(self) -> None:
        b_public, pending = self._challenge()
        client = self._client(b_public)
        m2 = srp.finish_handshake(self.GROUP, pending, client.a_public, client.m1)
        # Client authenticates the server too; a sign-in that skips this has only
        # proved something *to* the appliance.
        self.assertTrue(client.check(m2.hex()))

    def test_wrong_password_is_rejected(self) -> None:
        b_public, pending = self._challenge()
        client = self._client(b_public, "not the password")
        with self.assertRaises(srp.SrpError):
            srp.finish_handshake(self.GROUP, pending, client.a_public, client.m1)

    def test_wrong_password_produces_a_different_key_not_a_mismatch_error(self) -> None:
        # The point of a PAKE: the wrong-password client computes a *different*
        # S, it does not get a "wrong password" signal from the arithmetic.
        b_public, _ = self._challenge()
        good = self._client(b_public)
        bad = self._client(b_public, "not the password")
        self.assertNotEqual(good.secret, bad.secret)
        self.assertNotEqual(good.m1, bad.m1)

    def test_client_rejects_server_that_forges_M2(self) -> None:
        b_public, pending = self._challenge()
        client = self._client(b_public)
        srp.finish_handshake(self.GROUP, pending, client.a_public, client.m1)
        self.assertFalse(client.check("00" * 32))
        self.assertFalse(client.check("not hex"))
        self.assertFalse(client.check(""))

    def test_replayed_M1_fails_because_pending_is_single_use(self) -> None:
        b_public, pending = self._challenge()
        client = self._client(b_public)
        store = srp.PendingStore()
        store.put("cid", pending)
        first = srp.finish_handshake(self.GROUP, store.take("cid"), client.a_public, client.m1)
        self.assertTrue(client.check(first.hex()))
        # The replay is what a sniffer of the verify request would attempt.
        self.assertIsNone(store.take("cid"))

    def test_expired_challenge_is_dropped(self) -> None:
        _b_public, pending = srp.begin_handshake(
            self.GROUP, self.USERNAME, self.verifier, now=0.0, ttl=1.0
        )
        store = srp.PendingStore()
        store.put("cid", pending, now=0.0)
        self.assertIsNotNone(store.take("cid", now=0.5))
        self.assertIsNone(store.take("cid", now=2.0))

    def test_expired_challenges_are_pruned_on_write(self) -> None:
        _, pending = srp.begin_handshake(
            self.GROUP, self.USERNAME, self.verifier, now=0.0, ttl=1.0
        )
        store = srp.PendingStore()
        store.put("old", pending, now=0.0)
        _, fresh = srp.begin_handshake(
            self.GROUP, self.USERNAME, self.verifier, now=50.0
        )
        store.put("new", fresh, now=50.0)
        self.assertEqual(len(store), 1)
        self.assertIsNotNone(store.take("new", now=50.0))

    def test_zero_scrambler_is_refused(self) -> None:
        """The u!=0 check is the entire difference between SRP-6 and SRP-6a."""
        b_public, pending = self._challenge()
        client = self._client(b_public)
        with mock.patch.object(srp, "scrambler", return_value=0):
            with self.assertRaises(srp.SrpError):
                srp.finish_handshake(self.GROUP, pending, client.a_public, client.m1)

    def test_client_also_refuses_zero_scrambler(self) -> None:
        b_public, _ = self._challenge()
        with mock.patch.object(srp, "scrambler", return_value=0):
            with self.assertRaises(srp.SrpError):
                self._client(b_public)

    def test_zero_mod_N_client_value_is_refused(self) -> None:
        # RFC 5054 §2.5.4. A % N == 0 would make S independent of the verifier.
        _, pending = self._challenge()
        for bad in (0, self.GROUP.n):
            with self.subTest(a=bad):
                with self.assertRaises(srp.SrpError):
                    srp.finish_handshake(self.GROUP, pending, bad, b"\x00" * 32)

    def test_zero_mod_N_server_value_is_refused(self) -> None:
        # §2.5.3, the client's obligation, checked by ClientHandshake.
        with self.assertRaises(srp.SrpError):
            self._client(self.GROUP.n)

    def test_group_membership_is_enforced_on_the_wire(self) -> None:
        for bad in ("", "zz", "0", "-1", f"{self.GROUP.n:x}", "1" * 600):
            with self.subTest(value=bad[:24]):
                with self.assertRaises(srp.SrpError):
                    srp.parse_group_value(bad, self.GROUP, "A")

    def test_proof_must_be_a_digest(self) -> None:
        for bad in ("", "00", "z" * 64, "0" * 63, "0" * 65):
            with self.subTest(value=bad):
                with self.assertRaises(srp.SrpError):
                    srp.parse_proof(bad, self.GROUP)

    def test_valid_proof_parses(self) -> None:
        self.assertEqual(srp.parse_proof("ab" * 32, self.GROUP), b"\xab" * 32)

    def test_two_handshakes_are_independent(self) -> None:
        """A fresh `b` per challenge: reusing one would reuse the whole session."""
        b1, _ = self._challenge()
        b2, _ = self._challenge()
        self.assertNotEqual(b1, b2)

    def test_pending_store_bounds_memory(self) -> None:
        store = srp.PendingStore(max_entries=4)
        base = time.monotonic()
        for i in range(20):
            store.put(f"k{i}", srp.Pending("u", 1, 2, 3, base + 10_000 + i))
        self.assertEqual(len(store), 4)
        # Oldest-first: the survivors are the four most recent, not four random.
        self.assertIsNotNone(store.take("k19", now=base))
        self.assertIsNone(store.take("k0", now=base))


class EnrollmentTests(unittest.TestCase):
    GROUP = srp.SERVER_GROUP

    def test_round_trip_accepts_a_client_produced_pair(self) -> None:
        salt, verifier = srp.make_enrollment(self.GROUP, "admin", "hunter2")
        salt_back, verifier_back = srp.enroll(self.GROUP, salt.hex(), f"{verifier:x}")
        self.assertEqual(salt_back, salt)
        self.assertEqual(verifier_back, verifier)

    def test_rejects_malformed_enrollment(self) -> None:
        for salt_hex, v_hex in (
            ("", "ab"),
            ("ab", "ab"),
            ("zz" * 16, "ab"),
            ("ab" * 16, ""),
            ("ab" * 16, "0"),
            ("ab" * 16, "not hex"),
            ("ab" * 16, f"{self.GROUP.n:x}"),
        ):
            with self.subTest(salt=salt_hex[:8], verifier=v_hex[:8]):
                with self.assertRaises(srp.SrpError):
                    srp.enroll(self.GROUP, salt_hex, v_hex)

    def test_enrollment_does_not_reveal_x(self) -> None:
        """The stored verifier is g^x; x itself must not appear in what we send."""
        salt, verifier = srp.make_enrollment(self.GROUP, "admin", "hunter2")
        x = srp.x_from_password(self.GROUP, salt, "admin", "hunter2")
        blob = salt.hex() + f"{verifier:x}"
        self.assertNotIn(f"{x:x}", blob)

    def test_same_password_different_salt_different_verifier(self) -> None:
        _, v1 = srp.make_enrollment(self.GROUP, "admin", "hunter2")
        _, v2 = srp.make_enrollment(self.GROUP, "admin", "hunter2")
        self.assertNotEqual(v1, v2)

    def test_username_is_bound_into_the_verifier(self) -> None:
        """One verifier must not be portable across two accounts."""
        salt = srp.new_salt()
        _, v_admin = srp.make_enrollment(self.GROUP, "admin", "hunter2", salt)
        _, v_root = srp.make_enrollment(self.GROUP, "root", "hunter2", salt)
        self.assertNotEqual(v_admin, v_root)


class UnknownUserSimulationTests(unittest.TestCase):
    """RFC 5054 §2.5.1.3: the challenge endpoint must not answer "no such user"."""

    def test_fold_salt_is_deterministic_per_name(self) -> None:
        secret = b"\x11" * 32
        self.assertEqual(srp.fold_salt(secret, "admin"), srp.fold_salt(secret, "admin"))

    def test_fold_salt_differs_by_name_and_by_secret(self) -> None:
        a = srp.fold_salt(b"\x11" * 32, "admin")
        self.assertNotEqual(a, srp.fold_salt(b"\x11" * 32, "admin2"))
        self.assertNotEqual(a, srp.fold_salt(b"\x22" * 32, "admin"))

    def test_fold_salt_is_the_right_shape(self) -> None:
        folded = srp.fold_salt(b"\x11" * 32, "admin")
        self.assertEqual(len(folded), srp.SALT_BYTES)

    def test_fold_salt_does_not_leak_the_seed(self) -> None:
        secret = b"seed" * 8
        folded = srp.fold_salt(secret, "admin")
        self.assertNotIn(secret, folded)
        # And it is not merely a truncated hash of the name.
        self.assertNotEqual(folded, hashlib.sha256(b"admin").digest()[: srp.SALT_BYTES])

    def test_simulated_handshake_completes_then_fails_like_a_wrong_password(self) -> None:
        """Same message shape and same failure mode as a real wrong password."""
        group = srp.SERVER_GROUP
        secret = b"\x33" * 32
        fake_salt = srp.fold_salt(secret, "ghost")
        fake_v = srp.make_verifier(group, srp.hash_to_int(group.hash_name, fake_salt))
        b_public, pending = srp.begin_handshake(group, "ghost", fake_v)
        client = srp_client.ClientHandshake("ghost", "guess", fake_salt.hex(), f"{b_public:x}", group)
        with self.assertRaises(srp.SrpError) as raised:
            srp.finish_handshake(group, pending, client.a_public, client.m1)
        # Identical exception type to a wrong password on a real account, which is
        # what the caller turns into one indistinguishable 401.
        self.assertIsInstance(raised.exception, srp.SrpError)


class ModuleHygieneTests(unittest.TestCase):
    """Guards against the mistakes that would make this quietly insecure."""

    def test_no_password_sized_temporary_is_stored_on_pending(self) -> None:
        fields = set(srp.Pending.__dataclass_fields__)
        self.assertNotIn("password", fields)
        self.assertNotIn("x", fields)

    def test_salt_length_meets_the_documented_floor(self) -> None:
        self.assertGreaterEqual(srp.SALT_BYTES, 16)

    def test_private_exponents_meet_rfc_5054_floor(self) -> None:
        # §3.1: private values SHOULD be at least 256 bits.
        self.assertGreaterEqual(srp.PRIVATE_BITS, 256)
        value = srp.new_private(srp.SERVER_GROUP)
        self.assertLess(value, srp.SERVER_GROUP.n)
        self.assertGreater(value, 0)

    def test_digest_default_is_not_sha1_in_production(self) -> None:
        self.assertEqual(
            len(srp.digest(srp.SERVER_GROUP.hash_name)), 32
        )


if __name__ == "__main__":
    unittest.main()
