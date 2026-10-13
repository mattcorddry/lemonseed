#!/usr/bin/env python3
"""Executes ``stub.PORTAL_FETCH_JS`` and asserts the M6.6 status contract.

Why a JS harness and not string assertions over the rendered HTML: every test
that pins a snippet of JavaScript text passes happily while the branch beside it
is broken. This one runs the same source the browser gets (read straight out of
``stub``, so it cannot drift from what ships) in a V8 context with ``fetch`` and
``window`` stubbed, and asserts on what the code *did*: whether it navigated the
top frame, how many times it retried, and what it handed back.

The contract under test:

===========  =========================  =========================================
Status       Meaning                    Required behaviour
===========  =========================  =========================================
401          no valid session           navigate ``window.top`` to ``/login?rd=``
403          signed in, not permitted   return it; caller renders ``detail``
5xx          broken or starting         bounded retries, then surface. Never nav.
===========  =========================  =========================================

Node is a dev-box dependency, not an appliance one. Without it these tests skip,
and ``PortalCallSiteTests`` below still runs, so the shape of the contract keeps
some coverage on a machine with no JS runtime.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import stub

_HARNESS = """
'use strict';
const fs = require('fs');
const vm = require('vm');

const SOURCE = fs.readFileSync(process.argv[2], 'utf8');

function ok(code) {
  return code >= 200 && code < 300;
}

function response(code, body) {
  return {ok: ok(code), status: code, json: async () => body};
}

// A new context per scenario: the source declares top-level consts, so sharing a
// context between scenarios would fail on the second evaluation, not on a bug.
// `topPathname` omitted means "this document is the top frame".
function makeContext(pathname, search, respond, topPathname) {
  const calls = [];
  const own = {pathname: pathname, search: search};
  const sandbox = {
    fetch: async (p, init) => {
      calls.push({path: p, init: init});
      return respond(calls.length);
    },
    // Firing immediately keeps the backoff test honest (the delay really is
    // awaited) without spending three seconds per scenario.
    setTimeout: (fn) => {
      queueMicrotask(fn);
      return 1;
    },
    window: {
      location: own,
      top: {location: topPathname === undefined ? own : {pathname: topPathname, search: ''}},
    },
    console: console,
  };
  vm.createContext(sandbox);
  vm.runInContext(SOURCE, sandbox);
  return {sandbox, calls};
}

// The thing every scenario has to distinguish: a navigation assigns a string to
// window.top.location, so "still the location object" means nobody redirected.
function navigated(sandbox) {
  return typeof sandbox.window.top.location === 'string' ? sandbox.window.top.location : null;
}

const expired = () => response(401, {detail: 'Sign in again to continue.', lemonseed: 'session-expired'});

const scenarios = {
  async expired_session_navigates_the_top_frame() {
    const {sandbox, calls} = makeContext('/settings/', '?tab=models', expired);
    const r = await sandbox.portalFetch('/api/system');
    return {
      navigated: navigated(sandbox),
      ok: r.ok,
      status: r.status,
      calls: calls.length,
      marker: calls[0].init.headers['X-Requested-With'],
      detail: (await r.json()).detail,
    };
  },

  async iframe_signin_returns_to_the_shell_path_not_the_fragment() {
    // The settings panel is an iframe at /settings/content/. rd has to be the
    // shell's /settings/, or sign-in lands on a bare fragment with no nav and no
    // chat frame around it.
    const {sandbox} = makeContext('/settings/content/', '', expired, '/settings/');
    await sandbox.portalFetch('/_lemonseed/api/owui/config');
    return {navigated: navigated(sandbox)};
  },

  async standalone_page_uses_its_own_path() {
    const {sandbox} = makeContext('/settings/', '?tab=models', expired);
    await sandbox.portalFetch('/api/system');
    return {navigated: navigated(sandbox)};
  },

  async forbidden_is_returned_not_redirected() {
    const {sandbox, calls} = makeContext('/settings/', '',
      () => response(403, {detail: 'You are signed in, but this needs an admin account.'}));
    const r = await sandbox.portalFetch('/api/system');
    return {
      navigated: navigated(sandbox),
      ok: r.ok,
      status: r.status,
      calls: calls.length,
      rendered: await sandbox.portalError(r, 'fallback text'),
    };
  },

  async managed_path_denial_renders_its_reason() {
    // A guard denial is also a 403 with a detail, distinguished only by the body
    // marker, so the same call has to surface that text verbatim.
    const {sandbox} = makeContext('/settings/', '',
      () => response(403, {detail: 'Lemonseed manages this setting.', lemonseed: 'managed-by-lemonseed'}));
    const r = await sandbox.portalFetch('/_lemonseed/api/owui/config');
    return {rendered: await sandbox.portalError(r, 'fallback text')};
  },

  async service_error_is_retried_then_surfaced() {
    const {sandbox, calls} = makeContext('/', '', () => response(503, {detail: 'nope'}));
    const r = await sandbox.portalFetch('/api/system');
    return {
      navigated: navigated(sandbox),
      status: r.status,
      ok: r.ok,
      calls: calls.length,
    };
  },

  async service_error_recovers_mid_retry() {
    let n = 0;
    const {sandbox, calls} = makeContext('/', '', () => {
      n += 1;
      return n < 3 ? response(502, {}) : response(200, {telemetry: {}});
    });
    const r = await sandbox.portalFetch('/api/system');
    return {ok: r.ok, status: r.status, calls: calls.length};
  },

  async writes_are_never_retried() {
    // A retry after a lost response can run a reset or a reconcile twice.
    const {sandbox, calls} = makeContext('/', '', () => response(500, {detail: 'boom'}));
    const r = await sandbox.portalFetch('/_lemonseed/api/owui/reset-config', {method: 'POST'});
    return {calls: calls.length, status: r.status};
  },

  async no_answer_at_all_is_not_a_redirect() {
    const {sandbox, calls} = makeContext('/', '', () => {
      throw new TypeError('Failed to fetch');
    });
    const r = await sandbox.portalFetch('/api/system');
    return {
      navigated: navigated(sandbox),
      ok: r.ok,
      status: r.status,
      calls: calls.length,
      detail: (await r.json()).detail,
    };
  },

  async healthy_call_passes_straight_through() {
    const {sandbox, calls} = makeContext('/', '', () => response(200, {status: 'ok'}));
    const r = await sandbox.portalFetch('/api/system');
    const body = await r.json();
    return {
      navigated: navigated(sandbox),
      ok: r.ok,
      body: body.status,
      calls: calls.length,
      cache: calls[0].init.cache,
      marker: calls[0].init.headers['X-Requested-With'],
    };
  },

  async caller_headers_survive_the_marker() {
    const {sandbox, calls} = makeContext('/', '', () => response(200, {}));
    await sandbox.portalFetch('/x', {method: 'POST', headers: {'Content-Type': 'application/json'}});
    return {headers: calls[0].init.headers};
  },

  async error_without_a_readable_body_falls_back() {
    // nginx answers some refusals with HTML; res.json() on that must not kill a panel.
    const {sandbox} = makeContext('/', '', () => ({
      ok: false,
      status: 403,
      json: async () => {
        throw new SyntaxError('Unexpected token <');
      },
    }));
    const r = await sandbox.portalFetch('/x');
    return {rendered: await sandbox.portalError(r, 'plain fallback')};
  },
};

(async () => {
  const out = {};
  for (const [name, fn] of Object.entries(scenarios)) {
    try {
      out[name] = {result: await fn()};
    } catch (err) {
      out[name] = {error: String(err && err.stack ? err.stack : err)};
    }
  }
  process.stdout.write(JSON.stringify(out));
})();
"""


class PortalFetchContractTests(unittest.TestCase):
    """The shipped portalFetch() evaluated against stubbed fetch/window."""

    @classmethod
    def setUpClass(cls) -> None:
        node = shutil.which("node")
        if node is None:
            raise unittest.SkipTest("node is required to execute the portal fetch contract")
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "portal-fetch.js"
            harness = Path(tmp) / "harness.js"
            source.write_text(stub.PORTAL_FETCH_JS, encoding="utf-8")
            harness.write_text(_HARNESS, encoding="utf-8")
            proc = subprocess.run(
                [node, str(harness), str(source)],
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
        if proc.returncode != 0:
            raise AssertionError(f"node harness failed ({proc.returncode}): {proc.stderr}")
        cls.results = json.loads(proc.stdout)

    def _scenario(self, name: str) -> dict:
        entry = self.results.get(name)
        self.assertIsNotNone(entry, f"harness never ran scenario {name}")
        if entry.get("error"):
            self.fail(f"{name} threw in the JS harness: {entry['error']}")
        return entry["result"]

    def test_expired_session_sends_the_top_frame_to_login(self) -> None:
        got = self._scenario("expired_session_navigates_the_top_frame")
        # The portal is an iframe: navigating *this* window renders a login page
        # inside the portal, which is the failure this whole row exists to kill.
        self.assertEqual(got["navigated"], "/login?rd=%2Fsettings%2F%3Ftab%3Dmodels")
        self.assertFalse(got["ok"])
        self.assertEqual(got["status"], 401)
        # Bail out immediately; there is nothing to retry against a dead session.
        self.assertEqual(got["calls"], 1)
        self.assertTrue(got["detail"])

    def test_signin_from_an_iframe_returns_to_the_shell(self) -> None:
        got = self._scenario("iframe_signin_returns_to_the_shell_path_not_the_fragment")
        self.assertEqual(got["navigated"], "/login?rd=%2Fsettings%2F")
        self.assertNotIn("content", got["navigated"])

    def test_signin_from_a_top_level_page_uses_its_own_path(self) -> None:
        got = self._scenario("standalone_page_uses_its_own_path")
        self.assertEqual(got["navigated"], "/login?rd=%2Fsettings%2F%3Ftab%3Dmodels")

    def test_forbidden_is_handed_back_and_never_navigates(self) -> None:
        got = self._scenario("forbidden_is_returned_not_redirected")
        self.assertIsNone(got["navigated"], "403 must not redirect: it is 'not yours', not 'not you'")
        self.assertEqual(got["status"], 403)
        self.assertEqual(got["calls"], 1)
        self.assertIn("admin", got["rendered"])

    def test_a_managed_path_denial_reaches_the_admin_as_written(self) -> None:
        got = self._scenario("managed_path_denial_renders_its_reason")
        self.assertEqual(got["rendered"], "Lemonseed manages this setting.")

    def test_service_errors_retry_a_bounded_number_of_times(self) -> None:
        got = self._scenario("service_error_is_retried_then_surfaced")
        # One try + three backoff retries, then give up. An unbounded loop turns
        # one dead service into a browser that asks forever.
        self.assertEqual(got["calls"], 4)
        self.assertEqual(got["status"], 503)
        self.assertFalse(got["ok"])
        self.assertIsNone(got["navigated"], "a starting appliance is not a sign-in problem")

    def test_a_service_that_recovers_mid_retry_is_not_surfaced(self) -> None:
        got = self._scenario("service_error_recovers_mid_retry")
        self.assertTrue(got["ok"])
        self.assertEqual(got["calls"], 3)

    def test_writes_are_not_retried(self) -> None:
        """Retrying a POST whose response was lost can run a reset twice."""
        got = self._scenario("writes_are_never_retried")
        self.assertEqual(got["calls"], 1)
        self.assertEqual(got["status"], 500)

    def test_no_answer_at_all_is_not_treated_as_a_dead_session(self) -> None:
        got = self._scenario("no_answer_at_all_is_not_a_redirect")
        self.assertIsNone(got["navigated"], "a dropped connection must not sign anyone out")
        self.assertEqual(got["status"], 0)
        self.assertFalse(got["ok"])
        self.assertTrue(got["detail"])

    def test_healthy_call_passes_through_untouched(self) -> None:
        got = self._scenario("healthy_call_passes_straight_through")
        self.assertTrue(got["ok"])
        self.assertEqual(got["body"], "ok")
        self.assertEqual(got["calls"], 1)
        self.assertIsNone(got["navigated"])
        self.assertEqual(got["cache"], "no-store")

    def test_every_request_is_labelled_for_the_edge(self) -> None:
        """Without the marker the gateway answers 302, and fetch() follows it."""
        names = (
            "expired_session_navigates_the_top_frame",
            "healthy_call_passes_straight_through",
        )
        for name in names:
            with self.subTest(scenario=name):
                self.assertEqual(self._scenario(name)["marker"], "fetch")

    def test_caller_headers_are_not_replaced_by_the_marker(self) -> None:
        got = self._scenario("caller_headers_survive_the_marker")
        self.assertEqual(got["headers"]["Content-Type"], "application/json")
        self.assertEqual(got["headers"]["X-Requested-With"], "fetch")

    def test_an_unreadable_error_body_falls_back_instead_of_throwing(self) -> None:
        got = self._scenario("error_without_a_readable_body_falls_back")
        self.assertEqual(got["rendered"], "plain fallback")


class PortalCallSiteTests(unittest.TestCase):
    """No portal call may bypass the contract (runs even without node).

    A closed set over the *rendered* pages: the exceptions are named and argued,
    so adding a hand-written fetch() — the thing that started this — fails here.
    """

    # Each exception is deliberate; the reason is in the JS next to it.
    ALLOWED_RAW_FETCH = {
        # page_setup: pre-auth, and /api/boot is open by design. Sending it
        # through portalFetch would bounce a 401 to /login on the setup page.
        "/api/boot",
        # page_login: a failed sign-in IS a 401, so portalFetch would reload the
        # login page forever.
        "/login",
        # reachability probe: retrying and auth-navigating would hide the very
        # failure it exists to detect.
        "/_app/version.json",
    }
    # The one call inside portalFetch itself.
    IMPLEMENTATION_CALLS = 1

    def _pages(self) -> dict[str, str]:
        snap = {
            "version": "test",
            "chat": {"state": "ready", "display_name": "m"},
            "hardware": {},
            "modules": {},
        }
        with (
            mock.patch.object(stub, "brand_head_links", return_value=""),
            mock.patch.object(stub.auth, "admin_bootstrap_required", return_value=False),
            mock.patch.object(stub.auth, "reset_marker_present", return_value=False),
            mock.patch.object(stub, "setup_complete", return_value=True),
        ):
            pages = {
                "shell": stub.page_shell(dict(snap), view="settings").decode(),
                "settings": stub.page_settings_content(dict(snap)).decode(),
            }
        with (
            mock.patch.object(stub, "brand_head_links", return_value=""),
            mock.patch.object(stub, "load_setup", return_value={"mdns_name": "lemon-test"}),
            mock.patch.object(stub.auth, "admin_bootstrap_required", return_value=False),
            mock.patch.object(stub.auth, "reset_marker_present", return_value=True),
        ):
            pages["setup"] = stub.page_setup(dict(snap)).decode()
        return pages

    def test_every_other_call_goes_through_portal_fetch(self) -> None:
        offenders: list[str] = []
        for page, html in self._pages().items():
            raw = html.count("await fetch(")
            if page == "setup":
                # Pre-auth surface: allowed exactly the one boot poll.
                self.assertEqual(raw, 1, f"{page}: expected only the /api/boot poll")
                continue
            allowed = sum(html.count(f"fetch('{path}") for path in self.ALLOWED_RAW_FETCH)
            if raw - self.IMPLEMENTATION_CALLS > allowed:
                offenders.append(f"{page}: {raw} raw fetch() calls, {allowed} allowed")
            self.assertIn("portalFetch(", html, f"{page} lost the shared helper entirely")
        self.assertEqual(offenders, [])

    def test_the_open_subset_is_not_what_the_portal_polls(self) -> None:
        """/api/status has no chat, module or telemetry state to read.

        Both call shapes are checked: "portalFetch" ends in a capital F, so a
        lowercase-only search would happily miss the one form that matters.
        """
        for page, html in self._pages().items():
            if page == "setup":
                continue
            for call in ("fetch('/api/status'", "portalFetch('/api/status'"):
                self.assertNotIn(call, html, f"{page} polls the open subset")

    def test_logout_retires_the_chat_frame(self) -> None:
        """The OWUI iframe keeps its own session; leaving it loaded leaves a
        signed-in chat on screen after sign-out."""
        shell = self._pages()["shell"]
        # Split at the JS handler, not at the button markup that shares the id.
        anchor = "nav-logout').addEventListener"
        self.assertIn(anchor, shell)
        handler = shell.split(anchor, 1)[1].split("});", 1)[0]
        self.assertIn("retireChatFrame()", handler)
        self.assertIn("portalFetch('/logout'", handler)
        self.assertNotIn("location.href =", handler)

    def test_the_shared_helper_reaches_both_documents(self) -> None:
        """The settings panel is a separate document, not part of the shell."""
        pages = self._pages()
        for page in ("shell", "settings"):
            with self.subTest(page=page):
                self.assertIn("async function portalFetch(", pages[page])
                self.assertIn("async function portalError(", pages[page])


if __name__ == "__main__":
    unittest.main()
