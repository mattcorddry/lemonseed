"""Executes ``stub.SRP_CLIENT_JS`` in node and checks it against Python.

Same reasoning as ``test_portal_fetch.py``: a test that greps the JavaScript text
passes happily while the arithmetic beside it is wrong. This runs the source the
browser actually gets — read straight out of ``stub``, so it cannot drift from
what ships — in a V8 context, and asserts on the numbers it produces.

Three independent guarantees, and the third is the one that matters most:

1. **SHA-256 against FIPS 180-4.** The hand-rolled digest is the root of every
   value in this protocol; a wrong constant silently breaks sign-in.
2. **SHA-256 against ``hashlib``** over identical inputs, including the block
   boundaries (55/56/63/64/65 bytes) where hand-rolled padding usually breaks.
3. **The handshake against ``srp.py``.** The browser and the server must agree on
   ``x``, ``v``, ``u``, ``S``, ``M1`` and ``M2``. Correctness of the production
   group (SHA-256 over 2048 bits) reaches the browser through *this* test, because
   RFC 5054 publishes no vectors for that combination — the published vectors
   prove the formulas, and this proves the two sides implement them identically.

Node is a dev-box dependency, not an appliance one. Without it these tests skip;
``test_srp.py`` still runs, so the server half keeps full coverage anywhere.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import srp
import srp_client
import stub

# A new V8 context per scenario, for the reason given in test_portal_fetch.py:
# the source is an IIFE whose intrinsics would otherwise be shared, and a
# corrupted-digest run must not be able to influence the clean ones.
_HARNESS = """
'use strict';
const fs = require('fs');
const vm = require('vm');
const realCrypto = require('crypto');

const SOURCE = fs.readFileSync(process.argv[2], 'utf8');
const job = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));

// Mutated by the enrollment scenarios so a fixed salt can be compared with
// Python. Every other run gets the real CSPRNG, exactly as the browser does.
let pinnedSalt = null;

function run(source, script) {
  const sandbox = {
    TextEncoder, TextDecoder, console, JSON, Math, BigInt,
    DataView, Uint8Array, Uint32Array, Int32Array, Error, String, Number,
    parseInt, parseFloat, isNaN,
    crypto: {getRandomValues: (arr) => {
      if (pinnedSalt) {
        for (let i = 0; i < arr.length; i++) arr[i] = pinnedSalt[i % pinnedSalt.length];
        return arr;
      }
      return realCrypto.getRandomValues(arr);
    }},
  };
  const ctx = vm.createContext(sandbox);
  ctx.__pin = (bytes) => { pinnedSalt = bytes; };
  vm.runInContext(source, ctx);
  return vm.runInContext(script, ctx);
}

const results = [];
function push(o) { results.push(o); }

// 1/2. Digests.
const digests = [];
for (const s of job.texts) digests.push(run(SOURCE, `LemonseedSRP.sha256Hex(${JSON.stringify(s)})`));
const byteDigests = [];
for (const h of job.byteStrings) {
  byteDigests.push(run(SOURCE, `LemonseedSRP.sha256HexBytes(${JSON.stringify(h)})`));
}
push({kind: 'digests', texts: digests, bytes: byteDigests});

// KAT gate, clean and corrupted.
push({kind: 'ok', value: run(SOURCE, 'LemonseedSRP.ok')});
if (job.corrupt) {
  // Flip one SHA-256 round constant. A digest that is wrong in the last nibble
  // still "looks like" a hash, which is exactly why the gate must exist.
  const broken = SOURCE.replace('0xc67178f2', '0xc67178f3');
  push({kind: 'ok_corrupt', value: run(broken, 'LemonseedSRP.ok')});
  try {
    run(broken, "LemonseedSRP.authenticate('admin','x','00'.repeat(16),'ab')");
    push({kind: 'corrupt_threw', value: false});
  } catch (e) { push({kind: 'corrupt_threw', value: true}); }
}

// 3. Enrollment, with the salt pinned through the RNG shim.
for (const e of job.enrollments) {
  const script = `
    (function(){
      const bytes = ${JSON.stringify(e.salt)}.match(/../g).map((x) => parseInt(x, 16));
      globalThis.__pin(bytes);
      const r = LemonseedSRP.enroll(${JSON.stringify(e.identity)}, ${JSON.stringify(e.password)});
      globalThis.__pin(null);
      return JSON.stringify(r);
    })()`;
  push({kind: 'enroll', spec: e, result: JSON.parse(run(SOURCE, script))});
}

// 4. Handshake against a server challenge produced by Python.
for (const c of job.challenges) {
  const script = `
    (function(){
      const r = LemonseedSRP.authenticate(${JSON.stringify(c.identity)},
                                          ${JSON.stringify(c.password)},
                                          ${JSON.stringify(c.salt)},
                                          ${JSON.stringify(c.b)});
      return JSON.stringify(r);
    })()`;
  push({kind: 'auth', spec: c, result: JSON.parse(run(SOURCE, script))});
}

// 5. Client-side refusals. Bad B must abort, never compute.
for (const bad of job.badServers) {
  try {
    run(SOURCE, `LemonseedSRP.authenticate('a','p','${"0".repeat(32)}', ${JSON.stringify(bad)})`);
    push({kind: 'refuse', input: bad.slice(0, 16), refused: false});
  } catch (e) {
    push({kind: 'refuse', input: bad.slice(0, 16), refused: true});
  }
}

// 6. M2 checking on the client: a server that cannot prove the verifier.
for (const c of job.challenges) {
  const script = `
    (function(){
      const r = LemonseedSRP.authenticate(${JSON.stringify(c.identity)},
                                          ${JSON.stringify(c.password)},
                                          ${JSON.stringify(c.salt)},
                                          ${JSON.stringify(c.b)});
      return JSON.stringify({
        good: LemonseedSRP.verifyServer(r.expectM2, r.expectM2),
        wrong: LemonseedSRP.verifyServer(r.expectM2, '00'.repeat(32)),
        short: LemonseedSRP.verifyServer(r.expectM2, '00'),
        junk: LemonseedSRP.verifyServer(r.expectM2, 'zz'.repeat(32)),
      });
    })()`;
  push({kind: 'm2check', spec: c, result: JSON.parse(run(SOURCE, script))});
}

process.stdout.write(results.map((r) => JSON.stringify(r)).join('\\n') + '\\n');
"""


def _node() -> str | None:
    return shutil.which("node")


@unittest.skipIf(_node() is None, "node is a dev-box dependency, not an appliance one")
class SrpClientJsTests(unittest.TestCase):
    """The browser half of SRP, executed and compared with the server half."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="lemonseed-srp-js-"))
        (cls.tmp / "client.js").write_text(stub.SRP_CLIENT_JS, encoding="utf-8")
        (cls.tmp / "harness.js").write_text(_HARNESS, encoding="utf-8")

    def _run(self, job: dict) -> list[dict]:
        job_path = self.tmp / "job.json"
        job_path.write_text(json.dumps(job), encoding="utf-8")
        proc = subprocess.run(
            ["node", str(self.tmp / "harness.js"), str(self.tmp / "client.js"), str(job_path)],
            capture_output=True,
            text=True,
            timeout=120,
            # Checked by hand below: an exception would hide node's stderr, which
            # is the only useful diagnostic when the harness itself fails.
            check=False,
        )
        if proc.returncode != 0:
            self.fail(f"node harness failed: {proc.stderr[:2000]}")
        return [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]

    def _rows(self, kind: str, rows: list[dict]) -> list[dict]:
        return [r for r in rows if r["kind"] == kind]

    # --- the digest -------------------------------------------------------

    TEXTS = [
        "",
        "abc",
        # FIPS 180-4 Example 2: two blocks.
        "abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq",
        "hunter2",
        "correct horse battery staple",
        # Non-ASCII: the wire form is UTF-8, so a wrong TextEncoder use shows here.
        "pässwörd→✓",
        "日本語のパスワード",
        # Block boundaries, where hand-rolled padding breaks.
        "x" * 55,
        "x" * 56,
        "x" * 63,
        "x" * 64,
        "x" * 65,
        "x" * 119,
        "x" * 120,
        "x" * 128,
        "x" * 1000,
    ]
    BYTE_STRINGS = [
        "",
        "00",
        "0102",
        "000000",
        "deadbeef",
        "00" * 32,
        "ff" * 64,
        "00" * 55 + "ff",
        "7f" * 128,
        # A wide value, like hashing PAD(A): 256 bytes.
        "ab" * 256,
    ]

    def test_sha256_matches_hashlib_on_identical_inputs(self) -> None:
        rows = self._run(
            {
                "texts": self.TEXTS,
                "byteStrings": self.BYTE_STRINGS,
                "enrollments": [],
                "challenges": [],
                "badServers": [],
            }
        )
        got = self._rows("digests", rows)[0]
        self.assertEqual(len(got["texts"]), len(self.TEXTS))
        for text, digest in zip(self.TEXTS, got["texts"]):
            with self.subTest(text=text[:24]):
                self.assertEqual(digest, hashlib.sha256(text.encode("utf-8")).hexdigest())
        for hexed, digest in zip(self.BYTE_STRINGS, got["bytes"]):
            with self.subTest(bytes=hexed[:16]):
                self.assertEqual(digest, hashlib.sha256(bytes.fromhex(hexed)).hexdigest())

    def test_sha256_matches_the_published_vectors(self) -> None:
        rows = self._run(
            {
                "texts": self.TEXTS,
                "byteStrings": self.BYTE_STRINGS,
                "enrollments": [],
                "challenges": [],
                "badServers": [],
            }
        )
        digests = dict(zip(self.TEXTS, self._rows("digests", rows)[0]["texts"]))
        # FIPS 180-4 Examples 1-3.
        self.assertEqual(
            digests[""],
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        )
        self.assertEqual(
            digests["abc"],
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
        )
        self.assertEqual(
            digests["abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq"],
            "248d6a61d20638b8e5c026930c3e6039a33ce45964ff2167f6ecedd419db06c1",
        )

    def test_the_self_test_reports_healthy(self) -> None:
        rows = self._run(
            {
                "texts": [],
                "byteStrings": [],
                "enrollments": [],
                "challenges": [],
                "badServers": [],
            }
        )
        self.assertIs(self._rows("ok", rows)[0]["value"], True)

    def test_a_corrupted_digest_makes_the_self_test_refuse(self) -> None:
        """The KAT gate is the whole reason a wrong constant is survivable.

        Without it a bad SHA-256 looks like "wrong password" to every user, which
        is indistinguishable from an outage and unwinnable to debug.
        """
        rows = self._run(
            {
                "texts": [],
                "byteStrings": [],
                "enrollments": [],
                "challenges": [],
                "badServers": [],
                "corrupt": True,
            }
        )
        corrupt = self._rows("ok_corrupt", rows)
        self.assertTrue(corrupt, "corrupted source was not exercised")
        self.assertIs(corrupt[0]["value"], False)
        # And the pages gate on `ok`, so a refusal is what a user would hit.
        self.assertIn("LemonseedSRP.ok", stub.page_login(rd="/").decode())
        # The enrollment script only exists on the variant that creates an admin.
        setup = stub.page_setup(_SNAP, review_required=True, auth_admin_required=True).decode()
        self.assertIn("LemonseedSRP.ok", setup)
        self.assertIn("admin_srp_verifier", setup)
        # The plaintext field must not be submitted: it is disabled before submit.
        self.assertIn("pw.disabled = true", setup)

    # --- enrollment -------------------------------------------------------

    def test_enrollment_matches_python_for_a_pinned_salt(self) -> None:
        specs = [
            {"identity": "admin", "password": "hunter2", "salt": "00112233445566778899aabbccddeeff"},
            {"identity": "alice", "password": "password123", "salt": "beb25379d1a8581eb5a727673a2441ee"},
            {"identity": "admin", "password": "", "salt": "ff" * 16},
            {"identity": "élice", "password": "pässwörd→✓", "salt": "0123456789abcdef" * 2},
            {"identity": "a" * 64, "password": "x" * 128, "salt": "7f" * 16},
        ]
        rows = self._run(
            {
                "texts": [],
                "byteStrings": [],
                "enrollments": specs,
                "challenges": [],
                "badServers": [],
            }
        )
        got = self._rows("enroll", rows)
        self.assertEqual(len(got), len(specs))
        for row in got:
            spec = row["spec"]
            with self.subTest(identity=spec["identity"][:12], pw=spec["password"][:8]):
                salt = bytes.fromhex(spec["salt"])
                _, expected = srp.make_enrollment(srp.SERVER_GROUP, spec["identity"], spec["password"], salt)
                self.assertEqual(row["result"]["salt"], spec["salt"])
                self.assertEqual(row["result"]["verifier"], f"{expected:x}")

    def test_enrollment_output_is_storable_by_the_server(self) -> None:
        """Whatever the browser produces must survive ``auth.create_admin``."""
        rows = self._run(
            {
                "texts": [],
                "byteStrings": [],
                "enrollments": [
                    {"identity": "admin", "password": "hunter2", "salt": "a5" * 16}
                ],
                "challenges": [],
                "badServers": [],
            }
        )
        result = self._rows("enroll", rows)[0]["result"]
        salt, verifier = srp.enroll(srp.SERVER_GROUP, result["salt"], result["verifier"])
        self.assertEqual(salt, bytes.fromhex(result["salt"]))
        self.assertGreater(verifier, 0)

    # --- the handshake ----------------------------------------------------

    def test_browser_and_server_agree_on_the_shared_key(self) -> None:
        """The load-bearing test of the whole feature.

        The server accepts the browser's ``M1`` (which only happens if both
        computed the same ``S``, hence the same ``K``), and the browser's expected
        ``M2`` equals the ``M2`` the server actually sent — which is what makes the
        sign-in *mutually* authenticated rather than merely asserted.
        """
        group = srp.SERVER_GROUP
        identity, password = "admin", "correct horse battery staple"
        salt, verifier = srp.make_enrollment(group, identity, password)

        specs = []
        pending: list[tuple] = []
        for _ in range(3):
            b_public, p = srp.begin_handshake(group, identity, verifier)
            pending.append(p)
            specs.append(
                {
                    "identity": identity,
                    "password": password,
                    "salt": salt.hex(),
                    "b": f"{b_public:x}",
                }
            )

        rows = self._run(
            {
                "texts": [],
                "byteStrings": [],
                "enrollments": [],
                "challenges": specs,
                "badServers": [],
            }
        )
        auths = self._rows("auth", rows)
        self.assertEqual(len(auths), len(specs))
        for spec, p, auth in zip(specs, pending, auths):
            a_public = int(auth["result"]["A"], 16)
            self.assertGreater(a_public, 0)
            self.assertLess(a_public, group.n)
            m2 = srp.finish_handshake(group, p, a_public, bytes.fromhex(auth["result"]["M1"]))
            self.assertEqual(m2.hex(), auth["result"]["expectM2"])

    def test_a_wrong_password_from_the_browser_is_rejected_by_the_server(self) -> None:
        group = srp.SERVER_GROUP
        identity, password = "admin", "right"
        salt, verifier = srp.make_enrollment(group, identity, password)
        b_public, p = srp.begin_handshake(group, identity, verifier)
        spec = {
            "identity": identity,
            "password": "wrong",
            "salt": salt.hex(),
            "b": f"{b_public:x}",
        }
        rows = self._run(
            {
                "texts": [],
                "byteStrings": [],
                "enrollments": [],
                "challenges": [spec],
                "badServers": [],
            }
        )
        auth = self._rows("auth", rows)[0]["result"]
        with self.assertRaises(srp.SrpError):
            srp.finish_handshake(group, p, int(auth["A"], 16), bytes.fromhex(auth["M1"]))

    def test_the_browser_refuses_a_poisoned_server_value(self) -> None:
        """RFC 5054 2.5.3 makes B % N == 0 a client obligation."""
        group = srp.SERVER_GROUP
        bad = [
            "0",
            "00",
            "zz",
            "",
            f"{group.n:x}",  # >= N
            "f" * 600,  # absurd
            f"{group.n * 2:x}",
        ]
        rows = self._run(
            {
                "texts": [],
                "byteStrings": [],
                "enrollments": [],
                "challenges": [],
                "badServers": bad,
            }
        )
        refusals = {r["input"]: r["refused"] for r in self._rows("refuse", rows)}
        for value in bad:
            with self.subTest(value=value[:16]):
                self.assertTrue(refusals.get(value[:16], False))

    def test_a_valid_but_unusual_server_value_is_accepted(self) -> None:
        """1600 bits of ones is a legitimate group element; refusing it would be a bug."""
        group = srp.SERVER_GROUP
        identity, password = "admin", "hunter2"
        salt, verifier = srp.make_enrollment(group, identity, password)
        b_public, _pending = srp.begin_handshake(group, identity, verifier)
        rows = self._run(
            {
                "texts": [],
                "byteStrings": [],
                "enrollments": [],
                "challenges": [
                    {
                        "identity": identity,
                        "password": password,
                        "salt": salt.hex(),
                        "b": f"{b_public:x}",
                    }
                ],
                # Smaller than N, so it must NOT be refused.
                "badServers": ["ff" * 200],
            }
        )
        refused = {r["input"]: r["refused"] for r in self._rows("refuse", rows)}
        self.assertFalse(refused.get("ff" * 8, True))
        self.assertTrue(self._rows("auth", rows))

    def test_the_client_only_trusts_a_server_that_proves_the_verifier(self) -> None:
        rows = self._run(
            {
                "texts": [],
                "byteStrings": [],
                "enrollments": [],
                "challenges": [
                    {
                        "identity": "admin",
                        "password": "hunter2",
                        "salt": "11" * 16,
                        "b": "abc123",
                    }
                ],
                "badServers": [],
            }
        )
        checks = self._rows("m2check", rows)[0]["result"]
        self.assertTrue(checks["good"])
        self.assertFalse(checks["wrong"])
        self.assertFalse(checks["short"])
        self.assertFalse(checks["junk"])

    def test_the_python_client_agrees_with_the_browser_client(self) -> None:
        """``srp_client.py`` is what the QEMU smoke test signs in with.

        If it drifted from the browser, QEMU would be green against a protocol no
        real user speaks, which is worse than having no QEMU coverage at all.
        """
        group = srp.SERVER_GROUP
        identity, password = "admin", "hunter2"
        salt, verifier = srp.make_enrollment(group, identity, password)
        b_public, p = srp.begin_handshake(group, identity, verifier)
        spec_b = f"{b_public:x}"
        rows = self._run(
            {
                "texts": [],
                "byteStrings": [],
                "enrollments": [],
                "challenges": [
                    {
                        "identity": identity,
                        "password": password,
                        "salt": salt.hex(),
                        "b": spec_b,
                    }
                ],
                "badServers": [],
            }
        )
        js = self._rows("auth", rows)[0]["result"]
        # Both must complete the handshake against the same challenge. The
        # private exponent differs by design, so M1 differs too; the agreed M2
        # is what proves they reached the same key.
        py_pending_b, py_pending = srp.begin_handshake(group, identity, verifier)
        py = srp_client.ClientHandshake(identity, password, salt.hex(), f"{py_pending_b:x}", group)
        py_m2 = srp.finish_handshake(group, py_pending, py.a_public, py.m1)
        self.assertTrue(py.check(py_m2.hex()))
        js_m2 = srp.finish_handshake(group, p, int(js["A"], 16), bytes.fromhex(js["M1"]))
        self.assertEqual(js_m2.hex(), js["expectM2"])
        # The JS `A` is a group element the server accepted, i.e. the browser and
        # Python agree on what a valid public value looks like.
        self.assertGreater(int(js["A"], 16), 0)
        self.assertLess(int(js["A"], 16), group.n)


_SNAP = {
    "version": "test",
    "chat": {"state": "ready", "display_name": "m"},
    "hardware": {},
    "modules": {},
}


if __name__ == "__main__":
    unittest.main()
