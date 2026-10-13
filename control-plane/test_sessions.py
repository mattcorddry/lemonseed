#!/usr/bin/env python3
"""Unit tests for sessions.py (M6.6 D2).

The Valkey client is replaced wholesale, so these run with no Valkey, no Docker
and no Postgres (AGENTS.md).
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from typing import Any, Optional
from unittest import mock

CP = Path(__file__).resolve().parent
sys.path.insert(0, str(CP))

import sessions  # noqa: E402


class _FakeValkey:
    """Dict-backed stand-in for the redis client, including TTL bookkeeping.

    ``fail=True`` makes every command raise, which is how the fail-closed
    contract gets asserted.
    """

    def __init__(self, fail: bool = False) -> None:
        self.data: dict[str, str] = {}
        self.ttl: dict[str, int] = {}
        self.fail = fail

    def _check(self) -> None:
        if self.fail:
            raise ConnectionError("valkey is down")

    def set(self, key: str, value: str, ex: Optional[int] = None) -> bool:
        self._check()
        self.data[key] = value
        self.ttl[key] = int(ex or 0)
        return True

    def get(self, key: str) -> Optional[str]:
        self._check()
        return self.data.get(key)

    def expire(self, key: str, seconds: int) -> bool:
        self._check()
        if key not in self.data:
            return False
        self.ttl[key] = seconds
        return True

    def delete(self, key: str) -> int:
        self._check()
        self.ttl.pop(key, None)
        return 1 if self.data.pop(key, None) is not None else 0

    def ping(self) -> bool:
        self._check()
        return True


class SlidingWindowMathTests(unittest.TestCase):
    """The TTL policy as pure functions — no clock mocking, no I/O."""

    def test_fresh_session_gets_the_full_idle_window(self) -> None:
        self.assertEqual(sessions.sliding_ttl(1000.0, 1000.0), sessions.SESSION_INACTIVITY_SECONDS)

    def test_idle_window_is_clipped_by_the_absolute_ceiling(self) -> None:
        near_end = 1000.0 + sessions.SESSION_ABSOLUTE_SECONDS - 60
        self.assertEqual(sessions.sliding_ttl(near_end, 1000.0), 60)

    def test_past_the_ceiling_the_ttl_is_zero(self) -> None:
        past = 1000.0 + sessions.SESSION_ABSOLUTE_SECONDS + 1
        self.assertEqual(sessions.sliding_ttl(past, 1000.0), 0)
        self.assertEqual(sessions.remaining_seconds(past, 1000.0), 0)

    def test_remaining_seconds_counts_down_from_issuance(self) -> None:
        self.assertEqual(
            sessions.remaining_seconds(1000.0 + 3600, 1000.0),
            sessions.SESSION_ABSOLUTE_SECONDS - 3600,
        )


class SessionStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.valkey = _FakeValkey()
        # Patching the cached client also prevents the lazy `import redis`, and
        # pinning the boot id keeps keys deterministic.
        patches = [
            mock.patch.object(sessions, "_client", self.valkey),
            mock.patch.object(sessions, "_boot_id", "boot-abc"),
            mock.patch.object(sessions, "_forget_client"),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.record = {"u": "matt", "e": "matt@localhost", "g": ["admin"], "n": "Matt", "gen": 3, "iat": 1000}

    def test_key_is_namespaced_by_prefix_and_boot(self) -> None:
        self.assertEqual(sessions.key("sid1"), "lemonseed:sess:boot-abc:sid1")

    def test_create_then_read_round_trips_the_record(self) -> None:
        self.assertTrue(sessions.create("sid1", self.record, now=1000.0))
        self.assertEqual(sessions.read("sid1"), self.record)

    def test_create_sets_a_ttl_of_the_idle_window(self) -> None:
        sessions.create("sid1", self.record, now=1000.0)
        self.assertEqual(self.valkey.ttl[sessions.key("sid1")], sessions.SESSION_INACTIVITY_SECONDS)

    def test_create_refuses_a_session_beyond_its_ceiling(self) -> None:
        late = self.record["iat"] + sessions.SESSION_ABSOLUTE_SECONDS + 1
        self.assertFalse(sessions.create("sid1", self.record, now=late))
        self.assertIsNone(sessions.read("sid1"))

    def test_read_of_unknown_or_unparseable_is_no_session(self) -> None:
        self.assertIsNone(sessions.read("nope"))
        self.valkey.data[sessions.key("junk")] = "not json"
        self.assertIsNone(sessions.read("junk"))
        self.valkey.data[sessions.key("list")] = "[1, 2]"
        self.assertIsNone(sessions.read("list"))

    def test_refresh_extends_the_idle_window(self) -> None:
        sessions.create("sid1", self.record, now=1000.0)
        self.assertTrue(sessions.refresh("sid1", 1000.0, now=1500.0))
        self.assertEqual(self.valkey.ttl[sessions.key("sid1")], sessions.SESSION_INACTIVITY_SECONDS)

    def test_refresh_past_the_ceiling_drops_the_session(self) -> None:
        sessions.create("sid1", self.record, now=1000.0)
        late = 1000.0 + sessions.SESSION_ABSOLUTE_SECONDS + 1
        self.assertFalse(sessions.refresh("sid1", 1000.0, now=late))
        self.assertIsNone(sessions.read("sid1"))

    def test_a_reboot_hides_sessions_from_the_previous_boot(self) -> None:
        sessions.create("sid1", self.record, now=1000.0)
        with mock.patch.object(sessions, "_boot_id", "boot-next"):
            self.assertIsNone(sessions.read("sid1"))
            # The old key still exists in the store but is unreachable, and will
            # expire on its own — that is the accepted reboot tradeoff (D2).
            self.assertEqual(len(self.valkey.data), 1)

    def test_unreadable_boot_id_falls_back_to_a_fixed_bucket(self) -> None:
        with mock.patch.object(sessions, "_boot_id", None):
            with mock.patch.object(
                sessions,
                "BOOT_ID_PATH",
                _StubFile(error=OSError("no such file")),
            ):
                self.assertEqual(sessions._boot_namespace(), sessions.UNKNOWN_BOOT_ID)

    def test_readable_boot_id_is_used_verbatim(self) -> None:
        with mock.patch.object(sessions, "_boot_id", None):
            with mock.patch.object(
                sessions,
                "BOOT_ID_PATH",
                _StubFile(text="  b81e6f42-1111-2222-3333-444455556666\n"),
            ):
                self.assertEqual(
                    sessions._boot_namespace(), "b81e6f42-1111-2222-3333-444455556666"
                )


class FailClosedTests(unittest.TestCase):
    """Unreachable Valkey must never look like "no session" (401) or "fine" (200)."""

    def setUp(self) -> None:
        self.valkey = _FakeValkey(fail=True)
        patches = [
            mock.patch.object(sessions, "_client", self.valkey),
            mock.patch.object(sessions, "_boot_id", "boot-abc"),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.record = {"u": "matt", "e": "", "g": [], "n": "Matt", "gen": 0, "iat": 1000}

    def test_create_raises(self) -> None:
        with self.assertRaises(sessions.SessionStoreUnavailable):
            sessions.create("sid1", self.record, now=1000.0)

    def test_read_raises(self) -> None:
        with self.assertRaises(sessions.SessionStoreUnavailable):
            sessions.read("sid1")

    def test_refresh_raises(self) -> None:
        with self.assertRaises(sessions.SessionStoreUnavailable):
            sessions.refresh("sid1", 1000.0, now=1000.0)

    def test_drop_raises(self) -> None:
        with self.assertRaises(sessions.SessionStoreUnavailable):
            sessions.drop("sid1")

    def test_logout_still_succeeds_quietly(self) -> None:
        self.assertFalse(sessions.drop_quietly("sid1"))
        self.assertFalse(sessions.ping())

    def test_store_status_reports_unavailable(self) -> None:
        sessions.reset_status_cache()
        self.addCleanup(sessions.reset_status_cache)
        self.assertEqual(sessions.store_status(), "unavailable")

    def test_a_connection_error_forgets_the_cached_client(self) -> None:
        """Reconnect on next use rather than reusing a poisoned connection."""
        with mock.patch.object(sessions, "_forget_client", autospec=True) as forget:
            with self.assertRaises(sessions.SessionStoreUnavailable):
                sessions.read("sid1")
        forget.assert_called_once()


class _StubFile:
    """Just what ``_boot_namespace`` asks of a path: text out, or an OSError."""

    def __init__(self, text: str = "", error: Optional[OSError] = None) -> None:
        self._text = text
        self._error = error

    def read_text(self, encoding: Optional[str] = None) -> str:
        if self._error is not None:
            raise self._error
        return self._text


class StoreStatusTests(unittest.TestCase):
    """/api/status reads the store through this, so it must not re-probe per poll."""

    def setUp(self) -> None:
        sessions.reset_status_cache()
        self.addCleanup(sessions.reset_status_cache)
        self.valkey = _FakeValkey()
        patches = [
            mock.patch.object(sessions, "_client", self.valkey),
            mock.patch.object(sessions, "_forget_client", autospec=True),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def test_ok_when_the_store_answers(self) -> None:
        self.assertEqual(sessions.store_status(), "ok")

    def test_answer_is_cached_within_the_window(self) -> None:
        self.assertEqual(sessions.store_status(), "ok")
        self.valkey.fail = True  # the store dies, but the cached answer holds
        self.assertEqual(sessions.store_status(cache_sec=60.0), "ok")

    def test_cache_expiry_reprobes(self) -> None:
        self.assertEqual(sessions.store_status(), "ok")
        self.valkey.fail = True
        self.assertEqual(sessions.store_status(cache_sec=0.0), "unavailable")


class ClientConfigTests(unittest.TestCase):
    def test_stored_payload_is_json(self) -> None:
        valkey = _FakeValkey()
        record: dict[str, Any] = {"u": "matt", "e": "", "g": [], "n": "Matt", "gen": 1, "iat": 1000}
        with mock.patch.object(sessions, "_client", valkey):
            with mock.patch.object(sessions, "_boot_id", "boot-abc"):
                sessions.create("sid1", record, now=1000.0)
                # Read the key while the boot id is still pinned.
                raw = valkey.data[sessions.key("sid1")]
        self.assertEqual(json.loads(raw), record)


if __name__ == "__main__":
    raise SystemExit(unittest.main())
