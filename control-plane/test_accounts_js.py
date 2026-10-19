"""Runs the M7 portal JavaScript in node and asserts on what it does.

The reasoning is the one that already earned ``test_srp_client_js.py`` and
``test_portal_fetch.py``: grepping the JavaScript text passes happily while the
code beside it is wrong. So the two things that must not be wrong in the account
screens — how a one-time sign-in code is generated, and what the rotation
sequence actually puts on the wire — are *executed* here, from source read out of
``stub`` so it cannot drift from what ships.

What each guarantee rests on:

1. **The sign-in code** (node). ``makeOtp`` is extracted from ``ACCOUNTS_JS`` and
   run against the shipped word list, so the vocabulary, the shape and the
   randomness source are the ones that ship. A code a person reads over their
   shoulder and types into a phone is a UI artifact: it has to be short enough to
   say, unambiguous enough to spell, and uniform enough not to be guessable.
2. **The rotation sequence** (node). ``LemonseedRotate.change`` runs against a
   recorded fake transport, and the assertion is on the payloads it produced:
   ``salt``/``verifier`` out, never a password. That is this milestone's holding
   condition, so it is asserted on behaviour rather than on a regex.
3. **Cross-language M2 agreement** is *not* this file's job — it is proved
   against ``srp.py`` in ``test_srp_client_js.py``. Scenario 2 therefore checks
   the sequencing and the failure direction, not the arithmetic.

Node is a dev-box dependency, not an appliance one; without it these tests skip,
and the Python halves keep full coverage.
"""

from __future__ import annotations

import contextlib
import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import srp
import stub

NODE = shutil.which("node")


def _extract_function(source: str, signature: str) -> str:
    """Pull one ``function name(...) { ... }`` out of an IIFE.

    ``ACCOUNTS_JS`` is a closure, so its helpers are unreachable from outside —
    and exporting them for the sake of a test would ship a wider surface than the
    page needs. Brace *counting* rather than a regex, because the body contains
    nested blocks; the result is the shipped text verbatim.
    """
    start = source.index(signature)
    open_at = source.index("{", start)
    depth = 0
    for i in range(open_at, len(source)):
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                return source[start : i + 1]
    raise AssertionError(f"unbalanced braces after {signature!r}")


def public_b() -> str:
    """A valid group element, ``B = g^5 mod N``, for the appliance's fixed group.

    ``b = 5`` is known here on purpose: a scenario that needed the server's own
    arithmetic could reconstruct it. Nothing on the client may care.
    """
    return f"{pow(srp.SERVER_GROUP.g, 5, srp.SERVER_GROUP.n):x}"


# One V8 context per scenario. These runs mutate a fake transport and a counter,
# and a deliberately-failing run must not be able to colour a clean one.
_HARNESS = """
'use strict';
const fs = require('fs');
const vm = require('vm');
const realCrypto = require('crypto');

const job = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const results = [];

function contextFor(extra) {
  const sandbox = {
    TextEncoder, TextDecoder, console, JSON, Math, BigInt, Error, String,
    Number, Uint8Array, Uint32Array, parseInt, parseFloat, Promise,
    crypto: { getRandomValues: (a) => realCrypto.getRandomValues(a) },
  };
  const ctx = vm.createContext(sandbox);
  // A browser's `window` *is* the global object, so `window.LemonseedSRP = …`
  // also creates the bare binding `LemonseedSRP`. A vm sandbox is only a plain
  // property bag, so without this alias the shipped scripts would load yet stay
  // unreachable by name -- a harness artifact, not a bug, and one that would
  // make every page script look broken here but work in the browser.
  vm.runInContext('globalThis.window = globalThis;', ctx);
  Object.assign(ctx, extra || {});
  return ctx;
}

function finish() {
  if (job.expect === 'otp') fs.writeFileSync(process.argv[3], JSON.stringify(results));
}

if (job.expect === 'otp') {
  // 1. The sign-in code, exactly as the People panel builds it. The vocabulary is
  // the shipped file, so a word that is not in it cannot pass; the draw is real.
  const words = job.words;
  const live = contextFor({ words: words });
  vm.runInContext(job.otpSource, live);
  results.push({ otp: vm.runInContext(
    '(function(){const o=[];for(let i=0;i<' + job.n + ';i++)o.push(makeOtp(words));return o;})()',
    live) });

  // 1b. Same code with the randomness source pinned to zeros. If anything else
  // contributed entropy -- a timestamp, a counter -- it would show up here as
  // variation, which is how a weak generator hides.
  let calls = 0;
  const fixed = contextFor({ words: words, crypto: { getRandomValues: (a) => {
    for (let i = 0; i < a.length; i++) a[i] = 0;
    calls++;
    return a;
  } } });
  vm.runInContext(job.otpSource, fixed);
  // Exactly one code, so "calls" is the number of draws a single code spends.
  results.push({
    pinned: [vm.runInContext('makeOtp(words)', fixed)],
    calls: calls,
  });

  // 1c. A draw at or past the cutoff must be retried, not folded. The sequence is
  // 0xFFFFFFFF (past the cutoff for any list size), then four usable values, so a
  // modulo implementation would return the wrong words and a correct one spends
  // exactly one extra draw.
  let seqCalls = 0;
  const seq = [0xFFFFFFFF, 5, 6, 7, 8];
  const biased = contextFor({ words: words, crypto: { getRandomValues: (a) => {
    a[0] = seq[Math.min(seqCalls, seq.length - 1)];
    seqCalls++;
    return a;
  } } });
  vm.runInContext(job.otpSource, biased);
  results.push({
    rejected: vm.runInContext('makeOtp(words)', biased),
    seqCalls: seqCalls,
    expected: vm.runInContext('words[5] + "-" + words[6] + "-" + words[7] + "-" + words[8]', biased),
  });

  // 1d. A vocabulary that is too small to be safe must be refused outright.
  const guards = {};
  try { vm.runInContext('makeOtp(["a","b"])', live); guards.small = 'no error'; }
  catch (e) { guards.small = String(e && e.message ? e.message : e); }
  try { vm.runInContext('makeOtp(null)', live); guards.missing = 'no error'; }
  catch (e) { guards.missing = String(e && e.message ? e.message : e); }
  results.push({ guards: guards });
  finish();
}

if (job.expect === 'rotate') {
  // 2/3. The rotation sequence against a recorded transport. The promise is
  // awaited and written from its own handler so a rejection is a *result*, not
  // an uncaught error that leaves the output file missing.
  const posts = [];
  const ctx = contextFor({ post: async (path, payload) => {
    posts.push({ path: path, body: payload });
    if (path.endsWith('/challenge')) return { cid: 'cid-1', salt: job.salt, B: job.B };
    return { M2: job.serverM2 };
  } });
  vm.runInContext(job.srpSource, ctx);
  vm.runInContext(job.rotateSource, ctx);
  vm.runInContext(`(async function () {
    try {
      await LemonseedRotate.change(post, ${JSON.stringify(job.identity)},
        ${JSON.stringify(job.password)}, ${JSON.stringify(job.next)});
      return { resolved: true };
    } catch (err) { return { resolved: false, message: String(err && err.message) }; }
  })()`, ctx).then((outcome) => {
    results.push({ posts: posts, outcome: outcome });
    fs.writeFileSync(process.argv[3], JSON.stringify(results));
  }, (err) => {
    fs.writeFileSync(process.argv[3], 'HARNESS REJECTION: ' + (err && err.stack));
  });
}
"""


class _NodeHarnessTest(unittest.TestCase):
    """Shared runner for the two scenarios above."""

    EXPECT = ""

    def run_node(self, job: dict) -> list[dict]:
        if not NODE:
            self.skipTest("node is not installed")
        job = dict(job, expect=self.EXPECT)
        with tempfile.TemporaryDirectory() as tmp:
            job_path = Path(tmp) / "job.json"
            out_path = Path(tmp) / "out.json"
            harness = Path(tmp) / "harness.js"
            job_path.write_text(json.dumps(job), encoding="utf-8")
            harness.write_text(_HARNESS, encoding="utf-8")
            proc = subprocess.run(
                [NODE, str(harness), str(job_path), str(out_path)],
                capture_output=True,
                text=True,
                timeout=120,
                # Checked by hand below: an exception would hide node's stderr,
                # which is the only useful diagnostic when the harness fails.
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertTrue(out_path.exists(), "the harness wrote nothing: " + proc.stderr)
            text = out_path.read_text(encoding="utf-8")
            self.assertFalse(text.startswith("HARNESS REJECTION"), text)
            return json.loads(text)


class OtpGenerationTests(_NodeHarnessTest):
    """The sign-in code an admin reads aloud to somebody else."""

    EXPECT = "otp"

    @classmethod
    def setUpClass(cls) -> None:
        # Constants and helpers are lifted out of the shipped ACCOUNTS_JS text, so
        # the harness cannot drift from the page: renaming OTP_WORDS or dropping
        # the cutoff check fails here rather than in a household.
        constants = re.findall(
            r"const (OTP_WORDS|WORDLIST_MIN) = (\d+);", stub.ACCOUNTS_JS
        )
        names = {name for name, _value in constants}
        assert {"OTP_WORDS", "WORDLIST_MIN"} <= names, "ACCOUNTS_JS lost its OTP constants"
        cls.otp_source = "\n".join(
            [f"const {name} = {value};" for name, value in constants]
            + [
                _extract_function(stub.ACCOUNTS_JS, "function wordIndex(words)"),
                _extract_function(stub.ACCOUNTS_JS, "function makeOtp(words)"),
            ]
        )
        cls.words = [
            line.strip()
            for line in stub.WORDLIST_FILE.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")
        ]

    def _results(self) -> list[dict]:
        return self.run_node({
            "otpSource": self.otp_source,
            "words": self.words,
            "n": 200,
        })

    def test_codes_are_four_words_from_the_shipped_vocabulary(self) -> None:
        vocabulary = set(self.words)
        for code in self._results()[0]["otp"]:
            with self.subTest(code=code):
                parts = code.split("-")
                self.assertEqual(len(parts), 4, code)
                for part in parts:
                    self.assertIn(part, vocabulary, code)

    def test_codes_do_not_repeat(self) -> None:
        """~44 bits over four draws; a repeat means the draw is not uniform."""
        codes = self._results()[0]["otp"]
        self.assertGreater(len(set(codes)), 190, "sign-in codes are not varied")

    def test_the_csprng_is_the_only_source_of_entropy(self) -> None:
        pinned = self._results()[1]
        self.assertEqual(len(set(pinned["pinned"])), 1, pinned["pinned"])
        draws = int(re.search(r"const OTP_WORDS = (\d+);", stub.ACCOUNTS_JS).group(1))
        self.assertEqual(pinned["calls"], draws, "one draw per word, no more")

    def test_a_draw_past_the_cutoff_is_rejected_not_folded(self) -> None:
        """Plain modulo would answer with the wrong words and never notice.

        The sequence is 0xFFFFFFFF (past the cutoff for any list size) then 5, 6,
        7, 8, so the correct answer is the four words at those indices and exactly
        five draws.
        """
        case = self._results()[2]
        self.assertEqual(case["rejected"], case["expected"], case)
        self.assertEqual(case["seqCalls"], 5, case)

    def test_a_vocabulary_too_small_to_be_safe_is_refused(self) -> None:
        guards = self._results()[3]["guards"]
        for label, message in guards.items():
            with self.subTest(guard=label):
                self.assertNotEqual(message, "no error", message)
                self.assertIn("word list", message, message)

    def test_the_vocabulary_clears_the_floor_the_code_demands(self) -> None:
        """The Python floor and the JS floor must describe the same file."""
        minimum = int(re.search(r"const WORDLIST_MIN = (\d+);", stub.ACCOUNTS_JS).group(1))
        self.assertGreaterEqual(len(self.words), minimum, stub.WORDLIST_FILE.name)


class RotateSequenceTests(_NodeHarnessTest):
    """What the shared rotation helper puts on the wire, and how it fails."""

    EXPECT = "rotate"

    def _rotate(self, server_m2: str) -> tuple[list[dict], dict]:
        transport = self.run_node(
            {
                "srpSource": stub.SRP_CLIENT_JS,
                "rotateSource": stub.ROTATE_JS,
                "identity": "Matt",
                "password": "the current one",
                "next": "the next one",
                "salt": "abcd" * 8,
                "B": public_b(),
                "serverM2": server_m2,
            }
        )
        self.assertEqual(len(transport), 1)
        return transport[0]["posts"], transport[0]["outcome"]

    def test_only_a_verifier_leaves_the_browser(self) -> None:
        posts, _outcome = self._rotate("00" * 32)
        self.assertEqual(
            [p["path"] for p in posts],
            ["/_lemonseed/api/srp/challenge", "/_lemonseed/api/srp/rotate"],
        )
        challenge, rotate = posts
        self.assertEqual(set(challenge["body"]), {"username"})
        self.assertEqual(set(rotate["body"]), {"cid", "A", "M1", "salt", "verifier"})
        # The assertion this milestone exists for, taken from behaviour rather
        # than from the text: neither body has anywhere a password could be put.
        for post in posts:
            self.assertNotIn("password", json.dumps(post["body"]).lower())

    def test_a_server_that_cannot_prove_itself_reports_no_change(self) -> None:
        """The failure direction. A bad M2 must never read as success."""
        _posts, outcome = self._rotate("11" * 32)
        self.assertFalse(outcome["resolved"])
        self.assertIn("Nothing was changed", outcome["message"])

    def test_the_challenge_is_asked_for_before_the_rotation(self) -> None:
        """No cid means the rotate request could not be tied to anything."""
        posts, _outcome = self._rotate("00" * 32)
        self.assertEqual(posts[1]["body"]["cid"], "cid-1")
        self.assertEqual(len(posts), 2, "the sequence ran more than once")


class PasswordNeverSerialisedTests(unittest.TestCase):
    """Text-level on purpose: it covers the *callers*, which the harness cannot.

    ``LemonseedRotate.change`` receives plaintext arguments because it must, and
    so does the login form. The regression to catch is a caller that starts
    posting them, which no handshake test would notice.
    """

    def test_no_client_script_has_a_password_key(self) -> None:
        for name in ("ACCOUNTS_JS", "PROFILE_JS", "SRP_LOGIN_JS", "SRP_ENROLL_JS", "ROTATE_JS",
                     "DIALOG_JS", "SETTINGS_JS", "POWER_JS", "OWUI_CONFIG_JS"):
            with self.subTest(source=name):
                source = getattr(stub, name)
                self.assertNotIn("password:", source)
                self.assertNotIn("FormData", source)


    def test_the_panel_posts_a_verifier_and_never_the_code_it_came_from(self) -> None:
        """D-10 inside the account panel.

        The admin reads a four-word code aloud; the appliance stores only what
        that code proves. Text-level on purpose: the payload is assembled inside a
        closure the JS harness cannot call without a DOM, and the regression to
        catch is a new key appearing in one of these two posts.
        """
        create = stub.ACCOUNTS_JS.split("users/create", 1)[1].split("})", 1)[0]
        reset = stub.ACCOUNTS_JS.split("users/reset-password", 1)[1].split("})", 1)[0]
        for name, body in (("create", create), ("reset", reset)):
            with self.subTest(post=name):
                self.assertIn("salt: pair.salt", body)
                self.assertIn("verifier: pair.verifier", body)
        # The same rule, scanned across every post the panel can make, so a third
        # route cannot grow a code key later and keep this test green.
        posts = re.findall(r"post\('/_lemonseed/api/[^']+',\s*(\{.*?\})\);",
                           stub.ACCOUNTS_JS, re.S)
        self.assertGreaterEqual(len(posts), 5, "the post scan matched nothing")
        for body in posts:
            self.assertNotRegex(body, r"\b(otp|code|password|secret)\w*:")


class ProfilePageTests(unittest.TestCase):
    """The page itself, as rendered."""

    @staticmethod
    def _profile_html() -> str:
        with contextlib.ExitStack() as stack:
            for target, attr, value in (
                (stub, "brand_head_links", ""),
                (stub, "setup_complete", True),
                (stub.auth, "admin_bootstrap_required", False),
                (stub.auth, "reset_marker_present", False),
            ):
                kwargs = {"return_value": value}
                if callable(value):
                    kwargs = {"side_effect": value}
                stack.enter_context(mock.patch.object(target, attr, **kwargs))
            return stub.page_profile({"version": "test", "modules": {}}).decode()

    def test_every_signed_in_page_carries_the_account_popover(self) -> None:
        """Who you are, how you sign out, and where your profile is."""
        html = self._profile_html()
        for marker in (
            'id="nav-account"',
            'id="account-pop"',
            'id="nav-logout"',
            'href="/profile/"',
        ):
            self.assertIn(marker, html)

    def test_the_bar_is_one_function_not_two_copies(self) -> None:
        with (
            mock.patch.object(stub, "brand_head_links", return_value=""),
            mock.patch.object(stub, "setup_complete", return_value=True),
            mock.patch.object(stub.auth, "admin_bootstrap_required", return_value=False),
            mock.patch.object(stub.auth, "reset_marker_present", return_value=False),
        ):
            shell = stub.page_shell({"version": "test", "modules": {}}).decode()
        self.assertIn('id="nav-account"', shell)
        self.assertEqual(shell.count('<header class="app-bar">'), 1)
        # The popover's log-out is the only one, so there is only one handler.
        self.assertEqual(shell.count("nav-logout').addEventListener"), 1)

    def test_the_profile_page_loads_the_client_math_it_needs(self) -> None:
        html = self._profile_html()
        self.assertIn("LemonseedSRP", html)
        self.assertIn("LemonseedRotate", html)
        self.assertIn("portalFetch", html)
        # The KAT gate is what makes a broken digest refuse to submit.
        self.assertIn("LemonseedSRP.ok", html)

    def test_a_member_can_read_the_model_but_not_change_it(self) -> None:
        """The model is one choice for the whole household.

        A member is entitled to know what they are talking to; they are not
        entitled to change it for everyone else, so the profile page shows the
        name and offers no control that would write it.
        """
        html = self._profile_html()
        self.assertIn('id="profile-model"', html)
        self.assertIn("Chat model", html)
        self.assertNotIn('id="model-picker"', html)
        self.assertNotIn("/_lemonseed/api/models", html)

    def test_the_status_note_starts_hidden_and_the_username_is_a_value(self) -> None:
        """Bare-metal review: an empty status pill drew a mystery box nobody
        asked for, and the username field stayed blank because the page set
        textContent on an input, which does nothing."""
        html = self._profile_html()
        self.assertIn('<p class="owui-status" id="profile-note" hidden></p>', html)
        self.assertIn("getElementById('profile-user').value", stub.PROFILE_JS)
        self.assertNotIn("getElementById('profile-user').textContent", stub.PROFILE_JS)

    def test_nothing_on_the_page_is_rendered_with_innerhtml(self) -> None:
        """A display name is free text somebody typed; treating it as markup is
        an injected script in the person's own session.

        Checked as a property access so the prose in the shipped comments, which
        explains the rule, does not trip its own guard.
        """
        for name in ("PROFILE_JS", "ACCOUNTS_JS"):
            with self.subTest(source=name):
                self.assertNotIn(".innerHTML", getattr(stub, name))


class ShippedSourceTests(unittest.TestCase):
    """Text-level pins for behaviors the node harness cannot reach without a
    full fake clipboard: the HTTP-safe copy path and the CSV column names."""

    def test_the_copy_button_falls_back_when_the_clipboard_api_is_absent(self) -> None:
        # navigator.clipboard exists only in a secure context, and the portal is
        # plain HTTP on a LAN address, so the execCommand path is the one that
        # actually runs in the field.
        self.assertIn("execCommand('copy')", stub.ACCOUNTS_JS)

    def test_the_audit_csv_columns_match_their_names_in_the_ui(self) -> None:
        self.assertIn("timestamp,actor,target,action,detail", stub.ACCOUNTS_JS)
        self.assertNotIn("'at,", stub.ACCOUNTS_JS)


if __name__ == "__main__":
    unittest.main()
