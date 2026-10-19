#!/usr/bin/env python3
"""The sign-in-code word list: shape, safety, and the route that serves it.

The list is hand-curated data, not code, so the only protection it has is these
invariants. They are the reason the list can stay curated by a human: the rules
that matter are machine-checked, the judgment calls are not.
"""

from __future__ import annotations

import math
import re
import sys
import threading
import unittest
from collections import Counter
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

CP = Path(__file__).resolve().parent
sys.path.insert(0, str(CP))

import stub  # noqa: E402

# A code is read aloud and typed into a login field, often on a phone.
SHAPE = re.compile(r"^[a-z]{2,6}$")

# Words that must never be drawn even though they pass every other rule here:
# obscene, insulting, or a slur in some register. The admin reads this out loud
# to a neighbour, so the vocabulary has to be safe in front of a family.
UNSAID = {
    "ass", "damn", "crap", "dick", "cock", "sex", "anal", "piss", "wank", "boob",
    "anus", "penis", "vagina", "breast", "butt", "nude", "orgy", "rape", "nazi",
    "fag", "fagot", "spic", "cretin", "idiot", "stupid", "dumb", "ugly", "whore",
    "slut", "bitch", "bugger", "gimp", "lame", "retard", "nuts", "crazy", "insane",
    "slave", "master", "hate", "kill", "die", "dead", "gun", "bomb", "drug", "booze",
}

# Four words must carry at least this much entropy. Computed from the file, so
# the number a document quotes cannot drift from the vocabulary it describes.
MIN_BITS = 38.0


def _words() -> list[str]:
    lines = stub.WORDLIST_FILE.read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.startswith("#")]


class WordListFileTests(unittest.TestCase):
    def test_every_word_has_the_right_shape(self) -> None:
        bad = [w for w in _words() if not SHAPE.match(w)]
        self.assertEqual(bad, [], "words must be [a-z], 2-6 letters, one per line")

    def test_file_is_plain_ascii(self) -> None:
        # The client splits the body on newlines; a stray non-ASCII byte would
        # arrive as a word nobody can type.
        raw = stub.WORDLIST_FILE.read_bytes()
        self.assertEqual(raw.count(b"\r"), 0, "CRLF would create words with a tail")
        self.assertTrue(all(byte < 128 for byte in raw), "word list must be ASCII")

    def test_words_are_sorted_and_unique(self) -> None:
        words = _words()
        self.assertGreater(len(words), 0, "word list came back empty")
        duplicates = sorted(word for word, count in Counter(words).items() if count > 1)
        self.assertEqual(duplicates, [], "duplicate words waste entropy and payload")
        self.assertEqual(words, sorted(words), "sorted so a diff shows an addition as one line")

    def test_entropy_clears_the_bar(self) -> None:
        words = _words()
        bits = 4 * math.log2(len(words))
        self.assertGreaterEqual(
            bits, MIN_BITS, f"four words from {len(words)} entries is only {bits:.1f} bits"
        )

    def test_nothing_we_would_not_say_aloud(self) -> None:
        hits = sorted(set(_words()) & UNSAID)
        self.assertEqual(hits, [], f"remove {hits} from {stub.WORDLIST_FILE.name}")

    def test_the_locale_lives_in_the_file_name(self) -> None:
        """D-27: a second locale is a new file, not a code change.

        The name is the only place the locale appears, so a rename that drops it
        would leave the next translator with no idea where words-fr_CA.txt goes.
        """
        self.assertTrue(
            re.match(r"^[a-z]{2,3}_[A-Z]{2}$", stub.WORDLIST_LOCALE),
            f"locale {stub.WORDLIST_LOCALE!r} is not an IETF-style tag",
        )
        self.assertEqual(
            stub.WORDLIST_FILE.name, f"words-{stub.WORDLIST_LOCALE}.txt"
        )
        self.assertTrue(stub.WORDLIST_FILE.is_file(), f"{stub.WORDLIST_FILE} is missing")


class WordListRouteTests(unittest.TestCase):
    """GET /_lemonseed/api/wordlist — admin-only, and byte-for-byte the file.

    The page asks for this only when it is about to build a code, so it must be a
    plain body the client can split on newlines rather than a JSON envelope that
    would add ~15 % payload for nothing.
    """

    ADMIN = {"X-Forwarded-Groups": "admin"}

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer((stub.HOST, 0), stub.Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def _get(self, path: str, headers: dict | None = None) -> tuple[int, bytes, str]:
        req = Request(f"http://{stub.HOST}:{self.port}{path}", headers=headers or {})
        try:
            with urlopen(req, timeout=5) as resp:
                return resp.status, resp.read(), resp.headers.get("Content-Type", "")
        except HTTPError as exc:
            return exc.code, exc.read(), exc.headers.get("Content-Type", "")

    def test_requires_admin(self) -> None:
        code, _body, _ctype = self._get("/_lemonseed/api/wordlist")
        self.assertEqual(code, 403)

    def test_admin_gets_the_file_verbatim(self) -> None:
        code, body, ctype = self._get("/_lemonseed/api/wordlist", headers=self.ADMIN)
        self.assertEqual(code, 200)
        self.assertTrue(ctype.startswith("text/plain"), ctype)
        self.assertEqual(body, stub.WORDLIST_FILE.read_bytes())

    def test_a_missing_file_is_an_error_not_an_empty_list(self) -> None:
        """Silence here would mean makeOtp() throws in the browser instead."""
        original = stub.WORDLIST_FILE
        try:
            stub.WORDLIST_FILE = Path("/nonexistent/words-en_US.txt")
            code, body, _ctype = self._get("/_lemonseed/api/wordlist", headers=self.ADMIN)
        finally:
            stub.WORDLIST_FILE = original
        self.assertEqual(code, 500)
        self.assertIn("word list", body.decode())


if __name__ == "__main__":
    unittest.main()
