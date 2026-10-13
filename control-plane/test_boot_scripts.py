"""S5.9: run the *rendered* data-plane start scripts against a stub docker.

The S5 QEMU assertion was written against module source text and would have
passed while the line it claimed to check never printed. It did not print: the
containers run ``--restart unless-stopped``, so dockerd has them up again long
before the oneshot unit runs, and that reuse path exited without saying which
image was in play.

Pattern-matching Nix source cannot see which branch a real boot takes, so this
module renders the shell out of ``data-services.nix``, substitutes the
interpolations, and executes it. It is the only offline tier that can observe
the reuse path, the create path, and the "nothing local, hand off to the control
plane" path.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODULE = ROOT / "nixos" / "modules" / "data-services.nix"
PINS_FILE = ROOT / "control-plane" / "container-images.json"
PINS = json.loads(PINS_FILE.read_text(encoding="utf-8"))

_SERVICE = {
    "postgres": ("lemonseed-postgres", "5432", "pgdata"),
    "valkey": ("lemonseed-valkey", "6379", "valkeydata"),
}

_STUB_DOCKER = """#!/usr/bin/env bash
# Minimal docker for the start scripts. State comes from the environment:
#   SCENARIO=tag_only|digest_only|no_images  which image reference is on disk
#   STATE=running|stopped|absent             what the container list looks like
#   CONFIG_IMAGE=<ref>                       what .Config.Image reports
set -u
if [[ "$1" == "image" && "$2" == "inspect" ]]; then
  if [[ "${SCENARIO}" == "no_images" ]]; then exit 1; fi
  if [[ "${SCENARIO}" == "digest_only" ]]; then
    if [[ "$3" == *"@sha256:"* ]]; then exit 0; fi
    exit 1
  fi
  # A dockerTools.pullImage tarball has RepoTags but no RepoDigests, so exactly
  # one of the two forms is present.
  if [[ "$3" == *":"* && "$3" != *"@sha256:"* ]]; then exit 0; fi
  exit 1
fi
case "$1" in
  ps)
    if [[ "$2" == "-a" ]]; then
      if [[ "${STATE}" != "absent" ]]; then echo "${NAME}"; fi
    elif [[ "${STATE}" == "running" ]]; then
      echo "${NAME}"
    fi
    ;;
  inspect)
    case "$3" in
      *NetworkMode*) echo "lemon" ;;
      *PortBindings*)
        echo '{"5432/tcp":[{"HostIp":"127.0.0.1","HostPort":"5432"}],"6379/tcp":[{"HostIp":"127.0.0.1","HostPort":"6379"}]}'
        ;;
      *Config.Image*) echo "${CONFIG_IMAGE}" ;;
      *) echo "" ;;
    esac
    ;;
  rm) echo "removed" ;;
  start) echo "started ${NAME}" ;;
  run) echo "created from ${*: -1}" ;;
  *) ;;
esac
"""


class NixStringParseError(Exception):
    """The module no longer has the shape this extractor expects."""


def _indented_string(text: str, marker: str) -> str:
    """Return the dedented body of the first ``''`` string at or after *marker*."""
    if marker not in text:
        raise NixStringParseError(f"marker not found in data-services.nix: {marker!r}")
    open_idx = text.index("''", text.index(marker)) + 2
    lines = text[open_idx:].split("\n")
    body: list[str] = []
    closed = False
    for line in lines[1:]:
        if re.fullmatch(r"\s*''[ ;]*", line):
            closed = True
            break
        body.append(line)
    if not closed or not body:
        raise NixStringParseError(f"unterminated '' string after {marker!r}")
    pad = min(len(line) - len(line.lstrip()) for line in body if line.strip())
    return "\n".join(line[pad:] if line.strip() else "" for line in body) + "\n"


def render(which: str) -> str:
    """Render ``postgresStart``/``valkeyStart`` as standalone shell."""
    if which not in _SERVICE:
        raise ValueError(which)
    text = MODULE.read_text(encoding="utf-8")
    shared = _indented_string(text, "containerNeedsRecreate = ").rstrip("\n")
    # The first '' string after the marker *is* the script body (`text = ''`);
    # name/runtimeInputs between them are double-quoted, so nothing is skipped.
    script = _indented_string(text, f"{which}Start = pkgs.writeShellApplication").rstrip("\n")
    if not script.startswith("set -euo pipefail"):
        raise NixStringParseError(f"{which}Start body does not start with `set -euo pipefail`")
    script = script.replace("${containerNeedsRecreate}", shared)
    pin = PINS[which]
    container, _port, data_dir = _SERVICE[which]
    subs = {
        f"${{{which}RunRef}}": f"{pin['name']}@{pin['digest']}",
        f"${{{which}Image}}": f"{pin['name']}:{pin['tag']}",
        f"${{{which}Container}}": container,
        f"${{{which}Data}}": f"/tmp/{data_dir}",
        "${lemonNetwork}": "lemon",
        "${postgresEnv}": "/tmp/postgres.env",
    }
    for key, value in subs.items():
        script = script.replace(key, value)
    leftover = re.findall(r"\$\{[A-Za-z]", script)
    if leftover:
        raise NixStringParseError(f"unrendered Nix interpolations: {sorted(set(leftover))}")
    return script


class RenderedStartScriptTests(unittest.TestCase):
    """Behaviour of the scripts a boot actually runs, per branch."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        stub = Path(self._tmp.name) / "docker"
        stub.write_text(_STUB_DOCKER, encoding="utf-8")
        stub.chmod(0o755)
        self.env = dict(os.environ, PATH=self._tmp.name + os.pathsep + os.environ["PATH"])

    def _run(self, which: str, *, state: str, scenario: str, config_image: str) -> str:
        script = Path(self._tmp.name) / f"{which}-start.sh"
        script.write_text(render(which), encoding="utf-8")
        result = subprocess.run(
            ["bash", str(script)],
            env=dict(
                self.env,
                NAME=_SERVICE[which][0],
                STATE=state,
                SCENARIO=scenario,
                CONFIG_IMAGE=config_image,
            ),
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, f"start script failed: {result.stderr}")
        return result.stdout + result.stderr

    # -- the four paths, each of which must announce the reference -------------

    def test_reuse_path_announces_the_container_image(self) -> None:
        """The common real-boot branch: dockerd started it, we only report."""
        out = self._run("postgres", state="running", scenario="tag_only",
                        config_image="pgvector/pgvector:pg16")
        self.assertIn("lemonseed-postgres: image pgvector/pgvector:pg16", out)
        self.assertNotIn("created from", out)

    def test_existing_stopped_container_announces_and_starts(self) -> None:
        out = self._run("postgres", state="stopped", scenario="tag_only",
                        config_image="pgvector/pgvector:pg16")
        self.assertIn("lemonseed-postgres: image pgvector/pgvector:pg16", out)
        self.assertIn("started lemonseed-postgres", out)

    def test_missing_container_is_created_from_the_local_tag(self) -> None:
        """QEMU profile: pullImage tarballs have no RepoDigests, so the tag wins."""
        out = self._run("postgres", state="absent", scenario="tag_only", config_image="none")
        self.assertIn("lemonseed-postgres: image pgvector/pgvector:pg16", out)
        self.assertNotIn("@sha256:", out)

    def test_nothing_local_hands_off_without_a_pull(self) -> None:
        out = self._run("postgres", state="absent", scenario="no_images", config_image="none")
        self.assertIn("lemonseed-postgres: image unknown", out)
        self.assertIn("the control plane will pull it", out)

    # -- the two rules the announcements depend on ----------------------------

    def test_digest_is_preferred_when_the_image_has_a_repo_digest(self) -> None:
        out = self._run("postgres", state="absent", scenario="digest_only", config_image="none")
        self.assertIn(f"lemonseed-postgres: image pgvector/pgvector@{PINS['postgres']['digest']}", out)

    def test_a_pin_bump_recreates_the_running_container(self) -> None:
        out = self._run("postgres", state="running", scenario="tag_only",
                        config_image="pgvector/pgvector:pg15")
        self.assertIn("created from pgvector/pgvector:pg16", out)

    def test_both_services_announce_on_every_path(self) -> None:
        """A guard on the wording the QEMU tier greps for."""
        for which, (container, _port, _data) in _SERVICE.items():
            script = render(which)
            self.assertEqual(
                len(re.findall(rf'echo "{container}: image ', script)),
                4,
                f"{container} must announce on reuse, start, create and hand-off",
            )


if __name__ == "__main__":
    unittest.main()
