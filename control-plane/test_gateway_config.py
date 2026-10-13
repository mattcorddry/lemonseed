#!/usr/bin/env python3
"""Tests for the gateway error contract (M6.6 S5.4/S5.5).

The behaviour under test lives in nginx config, which no Python unit test can
execute — so these are source-text assertions over ``nixos/modules/gateway.nix``
and ``control-plane/static/cp-down.html``, and they exist to stop *specific*
regressions we were burned by, not to restate the file:

* nginx refuses ``alias`` inside a named location (a config that will not even
  parse, which would take the whole gateway down at boot).
* nginx does not run ``error_page`` inside a location reached *through*
  ``error_page``, so "proxy to the CP when the CP is down" can never render a
  friendly page. That pattern is what shipped a raw nginx error page to users.
* the nginx unit may wait for the CP, but only with a bounded deadline, and only
  on the CP's own port — a hard ``after=`` on the CP would be a cycle.

Live behaviour (status codes, which body renders, the 401 gate staying intact)
is proven in the QEMU smoke test, not here.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

CP = Path(__file__).resolve().parent
ROOT = CP.parent
GATEWAY = ROOT / "nixos" / "modules" / "gateway.nix"
CP_DOWN_HTML = CP / "static" / "cp-down.html"

FIVE_XX = r"error_page 500 502 503 504 =503"


def _matched(text: str, open_at: int) -> str:
    """Return the braced body starting at the ``{`` at ``open_at``."""
    depth = 0
    for i in range(open_at, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[open_at + 1 : i]
    raise AssertionError("unbalanced braces")


def _blocks(text: str, prefix: str) -> dict[str, str]:
    """Return ``{location-head: body}`` for literal nginx locations.

    Only the locations written as nginx text (inside ``extraConfig``) appear
    this way; the ones declared as Nix ``locations."..."`` attributes need
    :func:`_nix_location`.
    """
    out: dict[str, str] = {}
    for match in re.finditer(r"^[ \t]*location[ \t]+([^{}\n]+?)[ \t]*\{", text, re.MULTILINE):
        head = match.group(1).strip()
        if not head.startswith(prefix):
            continue
        out[head] = _matched(text, text.index("{", match.start()))
    return out


def _nix_location(text: str, name: str) -> str:
    """Body of ``locations."<name>" = { ... };``."""
    needle = f'locations."{name}" = '
    start = text.index(needle)
    return _matched(text, text.index("{", start))


class CpDownPageTests(unittest.TestCase):
    def setUp(self) -> None:
        raw = CP_DOWN_HTML.read_text(encoding="utf-8")
        # Comments are for the engineer and may name components; the assertions
        # below are about what a person on the LAN is shown.
        without_html_comments = re.sub(r"<!--.*?-->", "", raw, flags=re.DOTALL)
        self.raw = raw
        self.text = "\n".join(
            ln for ln in without_html_comments.splitlines() if not ln.strip().startswith("//")
        )

    def test_page_exists_and_names_no_component(self) -> None:
        """It renders for a dead CP, a dead OWUI and a rebooting box alike."""
        self.assertIn("Lemonseed is getting ready", self.text)
        for wrong in ("control plane", "Open WebUI", "chat", "Postgres", "502"):
            self.assertNotIn(wrong, self.text, f"page copy blames {wrong!r}")

    def test_page_has_no_subresource_that_could_fail_the_same_way(self) -> None:
        """No external hosts, no stylesheets, no fonts, no module scripts.

        The one allowed subresource is the brand mark, which nginx serves out of
        SquashFS rather than from the service that is currently down. Checked on
        the raw text: a commented-out CDN line is an invitation to re-break this.
        """
        self.assertNotIn("<link", self.raw)
        self.assertNotIn("<script src", self.raw)
        self.assertNotIn("http://", self.raw)
        self.assertNotIn("https://", self.raw)
        for src in re.findall(r'<img[^>]*src="([^"]+)"', self.raw):
            self.assertTrue(
                src.startswith("/_lemonseed/brand/"), f"unexpected img source {src}"
            )

    def test_retry_probe_is_the_status_endpoint_and_hands_back_to_the_shell(self) -> None:
        """/api/status is unauthenticated, and readiness is the CP's call.

        Waiting on a finer readiness field here would leave the static page on
        screen while the shell already had something better to show.
        """
        self.assertIn('fetch("/api/status"', self.text)
        self.assertIn('cache: "no-store"', self.text)
        self.assertIn('data.status === "ok"', self.text)
        self.assertIn("window.top.location.reload()", self.text)

    def test_retry_is_jittered_and_self_cancelling(self) -> None:
        self.assertIn("Math.random()", self.text)
        self.assertIn("clearTimeout", self.text)
        # No unbounded synchronous loop, and no interval that outlives success.
        self.assertNotIn("setInterval", self.text)

    def test_no_invented_api_fields(self) -> None:
        """`phase` does not exist on /api/status; do not code against a guess."""
        for invented in ("phase", "startup_ready", "boot."):
            self.assertNotIn(invented, self.text)


class CpDownLocationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = GATEWAY.read_text(encoding="utf-8")
        self.locations = _blocks(self.text, "/_lemonseed/")

    def test_cp_down_location_is_internal_and_static(self) -> None:
        body = self.locations["/_lemonseed/cp-down"]
        self.assertIn("internal;", body)
        self.assertIn("try_files /cp-down.html =503;", body)
        self.assertIn("root ${cpStatic};", body)
        self.assertNotIn("proxy_pass", body)
        self.assertNotIn("alias", body)
        self.assertIn("no-store", body)

    def test_no_alias_in_any_named_location(self) -> None:
        """nginx: 'the "alias" directive cannot be used inside the named location'."""
        for head, body in _blocks(self.text, "@").items():
            self.assertNotIn("alias", body, f"alias inside {head} will not parse")

    def test_interstitial_preserves_the_original_path(self) -> None:
        """An internal redirect rewrites $uri, so proxy_pass must re-add it."""
        body = self.locations["/_lemonseed/interstitial"]
        self.assertIn("internal;", body)
        self.assertIn("http://127.0.0.1:8080$request_uri", body)

    def test_named_interstitial_is_gone(self) -> None:
        heads = set(_blocks(self.text, "@"))
        self.assertNotIn("@cp_interstitial", heads)
        self.assertIn("@owui_loading", heads)


class ErrorPageRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = GATEWAY.read_text(encoding="utf-8")

    def test_server_level_fallback_exists(self) -> None:
        self.assertIn(f"{FIVE_XX} /_lemonseed/cp-down;", self.text)

    def test_every_cp_location_brands_five_xx(self) -> None:
        """A location that sets *any* error_page stops inheriting the server one.

        auth-gate.conf contributes a 401 clause at location level, so relying on
        inheritance would silently leave every gated location unprotected.
        """
        missing = [
            line.strip()[:90]
            for line in self.text.splitlines()
            if "${cpProxy}" in line and "extraConfig" in line and "error_page" not in line
        ]
        self.assertEqual(missing, [], f"CP locations with no 5xx fallback: {missing}")

    def test_no_location_re_proxies_the_dead_control_plane(self) -> None:
        """The bug: error_page -> proxy_pass to the service that just failed."""
        self.assertNotIn("= @cp_interstitial", self.text)

    def test_root_error_target_is_mapped_like_its_upstream(self) -> None:
        """Document requests fall back to the static page; embeds to the shell."""
        http = self.text.split("appendHttpConfig = ''")[1].split("'';")[0]
        self.assertIn("$lemonseed_error_target", http)
        block = http.split("$lemonseed_error_target {")[1].split("}")[0]
        lines = [ln.strip() for ln in block.strip().splitlines() if ln.strip()]
        self.assertEqual(
            [ln for ln in lines if ln.startswith("default")],
            ["default      /_lemonseed/cp-down;"],
        )
        for line in lines:
            self.assertRegex(line, r"^\S+\s+/\S+;$", line)
        root = _nix_location(self.text, "= /")
        self.assertIn(f"{FIVE_XX} $lemonseed_error_target;", root)

    def test_owui_locations_keep_the_cp_shell(self) -> None:
        """For these the failing upstream is OWUI, so the CP still has news."""
        owui = [ln for ln in self.text.splitlines() if "@owui_loading" in ln and "error_page" in ln]
        self.assertEqual(len(owui), 2, owui)


class GatewayOrderingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = GATEWAY.read_text(encoding="utf-8")

    def test_nginx_waits_for_the_cp_on_its_own_port(self) -> None:
        """The poll targets the CP directly, never the gateway it gates."""
        script = self.text.split("gatewayWait = pkgs.writeShellApplication")[1].split("};")[0]
        self.assertIn("http://127.0.0.1:8080/healthz", script)
        # Not :80 — waiting on the port this unit is about to open is a deadlock.
        self.assertNotIn("127.0.0.1:80/", script)
        self.assertIn(
            'services.nginx.preStart = "${gatewayWait}/bin/lemonseed-gateway-wait"',
            self.text,
        )

    def test_the_wait_is_bounded_and_never_fails_the_unit(self) -> None:
        """A hang must not become a boot stall, and a slow CP must not eat :80."""
        script = self.text.split("gatewayWait = pkgs.writeShellApplication")[1].split("};")[0]
        self.assertIn("deadline=", script)
        self.assertIn("exit 0", script)
        self.assertLessEqual(
            int(re.search(r"gatewayWaitSeconds = (\d+)", self.text).group(1)), 30
        )

    def _unit(self) -> str:
        """The nginx unit override, comments stripped.

        Comments here deliberately *name* the units we must not depend on, so the
        assertions below have to look at live text only.
        """
        block = self.text.split("systemd.services.nginx = {")[1].split("\n  };")[0]
        return "\n".join(
            ln for ln in block.splitlines() if not ln.strip().startswith("#")
        )

    def test_nginx_still_does_not_depend_on_the_control_plane(self) -> None:
        unit = self._unit()
        for other in ("appliance-control-plane", "lemonseed-postgres", "docker"):
            self.assertNotIn(other, unit, f"nginx unit must not reference {other}")

    def test_restart_is_not_weakened(self) -> None:
        """The nginx module already sets Restart=always; do not downgrade it."""
        self.assertNotIn("Restart", self._unit())


if __name__ == "__main__":
    unittest.main()
