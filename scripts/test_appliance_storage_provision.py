"""Unit tests for the appliance storage provisioner's unlock step (M8 P7).

The real script is driven through stubbed binaries (AGENTS.md: no root, no
block devices, no Postgres) with a scenario file the stubs share, covering the
three-way branch of ENCRYPTED-USER-DATA.md §3.4 and the bounded retry (D-E2).
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "appliance-storage-provision.sh"
DEVICE = "/tmp/lemonseed-test-user-data"

# The stubs are one bash library the scenario drives: `state` reads/writes the
# shared JSON, and every stub logs its invocation for the assertions.
STUB_LIB = r"""
STATE_FILE="${STATE_DIR}/state.json"
CALLS_FILE="${STATE_DIR}/calls.log"

scenario() {
  python3 "$STATE_READER_PY" scenario "$STATE_FILE"
}

log_call() {
  echo "$1" >>"$CALLS_FILE"
}

state_get() {
  python3 "$STATE_READER_PY" "$1" "$STATE_FILE"
}

state_set() {
  python3 "$STATE_WRITER_PY" "$1" "$2" "$STATE_FILE"
}
"""

# A jq-free state reader/writer: tiny python, already a provisioner input.
STATE_READER = r"""
import json, sys
key = sys.argv[1]
path = sys.argv[2]
try:
    with open(path) as handle:
        state = json.load(handle)
except Exception:
    state = {}
if key == "scenario":
    print(state.get("scenario", ""))
else:
    print(state.get(key, ""))
"""
STATE_WRITER = r"""
import json, sys
key, value, path = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    with open(path) as handle:
        state = json.load(handle)
except Exception:
    state = {}
state[key] = value
with open(path, "w") as handle:
    json.dump(state, handle)
"""


class ProvisionerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_dir = Path(self._tmp.name)
        self.state_file = self.state_dir / "state.json"
        self.calls_file = self.state_dir / "calls.log"
        self.esp = self.state_dir / "esp"
        self.esp.mkdir()
        self.device = self.state_dir / "userdata.img"
        self.device.write_bytes(b"")

        (self.state_dir / "bin").mkdir()
        self._write_stubs()
        self.env = dict(
            os.environ,
            PATH=f"{self.state_dir / 'bin'}:{os.environ['PATH']}",
            STATE_DIR=str(self.state_dir),
            STATE_READER_PY=self._pytool("reader"),
            STATE_WRITER_PY=self._pytool("writer"),
            APPLIANCE_STORAGE_DEVICE=DEVICE,
            APPLIANCE_ESP_PART=str(self.esp),
            APPLIANCE_UNLOCK_WAIT="0",
            APPLIANCE_STORAGE_REQUIRED="1",
            # The mapper node the wait loop checks: a file under the temp dir.
            APPLIANCE_LV_PATH=str(self.state_dir / "dev" / "user-data"),
            APPLIANCE_MAPPER_PATH=str(
                self.state_dir / "dev" / "appliance-user--data"
            ),
        )

    # -- harness ------------------------------------------------------------

    def _pytool(self, kind: str) -> str:
        body = STATE_READER if kind == "reader" else STATE_WRITER
        path = self.state_dir / f"state-{kind}.py"
        path.write_text(body, encoding="utf-8")
        return str(path)

    def _write_stubs(self) -> None:
        lib = self.state_dir / "stub-lib.sh"
        lib.write_text(STUB_LIB, encoding="utf-8")
        reader = self.state_dir / "state-reader.py"
        reader.write_text(STATE_READER, encoding="utf-8")
        writer = self.state_dir / "state-writer.py"
        writer.write_text(STATE_WRITER, encoding="utf-8")

        stubs = {
            # Tools the real pipeline calls; record and succeed.
            "pvcreate": 'log_call "pvcreate $*"\n',
            "vgcreate": 'log_call "vgcreate $*"\n',
            "vgextend": 'log_call "vgextend $*"\n',
            "lvcreate": (
                'log_call "lvcreate $*"\n'
                '# The LV node appears once the LV exists (the every-boot\n'
                '# short-circuit above must not see it before lvcreate).\n'
                'if [ -n "$APPLIANCE_MAPPER_PATH" ]; then '
                'mkdir -p "$(dirname "$APPLIANCE_MAPPER_PATH")"; '
                'touch "$APPLIANCE_MAPPER_PATH"; fi\n'
            ),
            "lvchange": 'log_call "lvchange $*"\n',
            "pvs": 'log_call "pvs $*"\nexit 1\n',  # no PV yet: fresh provision
            "vgs": 'log_call "vgs $*"\nexit 1\n',
            "lvs": 'log_call "lvs $*"\nexit 1\n',
            "blkid": "exit 1\n",
            "btrfs": 'log_call "btrfs $*"\n',
            "mkfs.btrfs": 'log_call "mkfs.btrfs $*"\n',
            "udevadm": "",
            "findmnt": "exit 1\n",  # /etc not mounted: provisioning proceeds
            "lsblk": "",
            "sync": "",
            "mount": (
                'log_call "mount $*"\n'
                '# The real invocation is "mount -t FSTYPE -o OPTS SRC TGT"; the\n'
                '# source and target are the last two arguments.\n'
                'while [ $# -gt 2 ]; do shift; done\n'
                'mkdir -p "$2"\n'
                'if [ -d "$1" ]; then cp -a "$1"/. "$2"/ 2>/dev/null || true; fi\n'
            ),
            "umount": (
                'log_call "umount $*"\n'
                'if [ -d "$1" ] && [ -d "$APPLIANCE_ESP_PART" ]; then '
                'cp -a "$1"/. "$APPLIANCE_ESP_PART"/ 2>/dev/null || true; fi\n'
                '# A real umount drains the mount; drain too so the script\n'
                '# staging rmdir (set -e) sees an empty dir.\n'
                'if [ -d "$1" ]; then '
                'find "$1" -mindepth 1 -delete 2>/dev/null || true; fi\n'
            ),
            "systemd-ask-password": (
                'log_call "systemd-ask-password $*"\n'
                'echo "$(state_get passphrase)"\n'
            ),
            "systemctl": 'log_call "systemctl $*"\n',
            "reboot": 'log_call "reboot $*"\n',
            "halt": 'log_call "halt $*"\n',
            "vgchange": 'log_call "vgchange $*"\n',
            # cryptsetup carries the unlock scenarios on the shared state.
            "cryptsetup": (
                'log_call "cryptsetup $*"\n'
                "case $1 in\n"
                "  isLuks)\n"
                '    case $(scenario) in luks-*) exit 0;; *) exit 1;; esac\n'
                "    ;;\n"
                "  open)\n"
                '    case "$*" in\n'
                '      *--token-only*)\n'
                '        case $(scenario) in luks-token-ok|luks-open) exit 0;; esac\n'
                "        exit 1\n"
                "        ;;\n"
                "      *)\n"
                '        # passphrase attempt: the stub opens only for the scenario that\n'
                '        # represents the correct password (otherwise any non-empty\n'
                '        # match would trivially succeed, including "wrong").\n'
                '        expected="$(state_get passphrase)"\n'
                '        read -r got\n'
                '        case $(scenario) in\n'
                '          luks-passphrase)\n'
                '            if [ "$got" = "$expected" ] && [ -n "$expected" ]; then exit 0; fi\n'
                '            ;;\n'
                '        esac\n'
                "        exit 1\n"
                "        ;;\n"
                "    esac\n"
                "    ;;\n"
                "esac\n"
                "exit 0\n"
            ),
        }
        for name, body in stubs.items():
            stub = self.state_dir / "bin" / name
            stub.write_text(
                f'#!/bin/sh\n. "{lib}"\n{body}', encoding="utf-8"
            )
            os.chmod(stub, 0o755)

    def _run(self, scenario: str, *, passphrase: str = "") -> str:
        self.state_file.write_text(
            json.dumps({"scenario": scenario, "passphrase": passphrase}),
            encoding="utf-8",
        )
        result = subprocess.run(
            ["bash", str(SCRIPT)],
            check=False,
            capture_output=True,
            text=True,
            env=self.env,
            timeout=60,
        )
        return result.stdout + result.stderr

    def _calls(self) -> list[str]:
        if not self.calls_file.exists():
            return []
        return self.calls_file.read_text().splitlines()

    def _reset_calls(self) -> None:
        self.calls_file.write_text("", encoding="utf-8")

    def _counter(self) -> str:
        return (self.esp / "grub" / "unlock-attempts").read_text().strip()

    # -- the three-way branch (§3.4) ---------------------------------------

    def _contains_call(self, needle: str) -> bool:
        return any(needle in call for call in self._calls())

    def test_plain_volume_provisions_the_partition_itself(self) -> None:
        """Pre-P7 / unencrypted: the PV is the discovered partition, unchanged."""
        out = self._run("plain")
        self.assertEqual(out.count("ok (provisioned"), 1, out)
        self.assertTrue(self._contains_call(f"pvcreate -ff -y {DEVICE}"))
        self.assertFalse(self._contains_call("/dev/mapper/user-data-luks"))

    def test_luks_volume_with_a_working_token_opens_and_uses_the_mapper(self) -> None:
        out = self._run("luks-token-ok")
        self.assertEqual(out.count("ok (provisioned"), 1, out)
        self.assertTrue(self._contains_call("cryptsetup isLuks"))
        # The token path never prompts, and the mapper becomes the PV.
        self.assertFalse(self._contains_call("systemd-ask-password"))
        self.assertTrue(self._contains_call("pvcreate -ff -y /dev/mapper/user-data-luks"))

    def test_a_failing_passphrase_bumps_the_esp_counter_and_reboots(self) -> None:
        out = self._run("luks-wrong-password", passphrase="wrong")
        self.assertIn("boot the USB installer to recover", out)
        self.assertTrue(self._contains_call("systemctl reboot"))
        self.assertEqual(self._counter(), "1")

    def test_the_right_passphrase_opens_and_clears_the_counter(self) -> None:
        out = self._run("luks-passphrase", passphrase="apple-bacon-chard-date")
        self.assertEqual(out.count("ok (provisioned"), 1, out)
        self.assertTrue(self._contains_call("systemd-ask-password"))
        self.assertTrue(self._contains_call("pvcreate -ff -y /dev/mapper/user-data-luks"))
        self.assertFalse((self.esp / "grub" / "unlock-attempts").exists())

    # -- the bounded retry (D-E2) ------------------------------------------

    def test_three_failed_boots_halt_instead_of_looping(self) -> None:
        for attempt in range(3):
            self._reset_calls()
            out = self._run("luks-wrong-password", passphrase="wrong")
            if attempt == 2:
                self.assertTrue(self._contains_call("systemctl halt"))
                self.assertFalse(self._contains_call("systemctl reboot"))
            else:
                self.assertTrue(self._contains_call("systemctl reboot"))
                self.assertIn("boot the USB installer to recover", out)
        self.assertEqual(self._counter(), "3")


if __name__ == "__main__":
    unittest.main()
