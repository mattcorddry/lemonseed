# QEMU smoke-test profile: virtio root + user-data disk (see make run-qemu).
# Production images must NOT use this module (see DESIGN.md).

{ lib, pkgs, ... }:

let
  # The QEMU-only trust set: its private key is tracked (never a release key),
  # so any build host can sign the signing smoke's artifact sets.
  qemuTrustSet = import ../packages/release-keys.nix {
    inherit pkgs lib;
    keysDir = ../../keys;
    trustFile = "qemu/release-keys.json";
  };

  # M8 P1 signing smoke (asserted by scripts/qemu-api-smoke-test.sh via the
  # serial log, which journald forwards to ttyS0). Three scenarios against
  # host-served artifact sets (10.0.2.2 = the slirp gateway):
  #   good:              signature verifies, artifacts download and hash-verify
  #   tampered-payload:  a flipped payload byte is refused at the hash check
  #   tampered-manifest: an altered manifest is refused at the signature —
  #                      before a single hash inside it is trusted
  signingSmokePy = pkgs.writeText "lemonseed-signing-smoke.py" ''
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.environ["PYTHONPATH"])
import lemonseed_artifacts as artifacts

base = os.environ.get("LEMONSEED_SMOKE_BASE", "http://10.0.2.2:18081").rstrip("/")
dl = Path("/tmp/lemonseed-signing-smoke")


def report(marker):
    print("lemonseed-signing-smoke: " + marker, flush=True)


# Scenario 1: the correctly signed set. Retried while the host server comes
# up (the smoke runs at boot, the server may start seconds later).
version = None
deadline = time.monotonic() + 240
while True:
    try:
        files, version, _ = artifacts.acquire_artifacts(
            download_dir=dl / "good", url=base + "/good"
        )
        break
    except artifacts.ArtifactError as exc:
        if time.monotonic() > deadline:
            report("FAIL signed-set (server unreachable or verifier refused): %s" % str(exc)[:200])
            sys.exit(1)
        time.sleep(5)
    except Exception as exc:  # noqa: BLE001 — service boundary: report, do not crash silently
        report("FAIL signed-set: %s" % str(exc)[:200])
        sys.exit(1)
report("PASS signed-set version=%s" % version)

# Scenario 2: the same signed manifest, one flipped payload byte. The
# download's stream hash check must refuse — the manifest cannot rescue it.
try:
    artifacts.acquire_artifacts(download_dir=dl / "payload", url=base + "/tampered-payload")
    report("FAIL tampered-payload: a flipped payload byte was accepted")
    sys.exit(1)
except artifacts.ArtifactError as exc:
    report("REFUSE tampered-payload: %s" % str(exc)[:200])

# Scenario 3: the signed manifest altered after signing. The signature is
# checked before the manifest is parsed, so no hash inside it is ever trusted.
try:
    artifacts.fetch_manifest(base + "/tampered-manifest/")
    report("FAIL tampered-manifest: an altered manifest was accepted")
    sys.exit(1)
except artifacts.ArtifactError as exc:
    report("REFUSE tampered-manifest: %s" % str(exc)[:200])

report("DONE")
'';

  signingSmoke = pkgs.writeShellApplication {
    name = "lemonseed-signing-smoke";
    runtimeInputs = with pkgs; [ python3 openssl coreutils ];
    text = ''
      export PYTHONPATH=${../../scripts}
      export LEMONSEED_RELEASE_KEYS_DIR=${qemuTrustSet}
      rm -rf /tmp/lemonseed-signing-smoke
      exec ${pkgs.python3}/bin/python3 ${signingSmokePy}
    '';
  };

  # Forward the journal to the SERIAL tty so `make test-qemu` captures
  # journald in the serial log (no console access to log in otherwise).
  # TTYPath must be /dev/ttyS0: with console=tty0 last in kernelParams,
  # /dev/console is the virtual terminal, invisible to the smoke harness.
  # Drop-in under etc (a systemd.services ExtraConfig key is not valid).
  journaldConf = pkgs.writeText "journald-console.conf" ''
    [Journal]
    ForwardToConsole=yes
    TTYPath=/dev/ttyS0
    MaxLevelConsole=info
  '';
in
{
  imports = [ ./qemu-docker-cache.nix ];

  # SquashFS presented as the first virtio disk.
  fileSystems."/" = lib.mkForce {
    device = "/dev/vda";
    fsType = "squashfs";
    options = [ "ro" ];
  };

  # Second virtio disk is the LVM PV candidate (whole-disk for QEMU).
  boot.kernelParams = [ "appliance.storage=/dev/vdb" ];

  # No GPU in the smoke VM — keep the image small and skip GPU driver stacks.
  lemonseed.nvidia.enable = lib.mkForce false;
  hardware.amd-gpu.enable = lib.mkForce false;

  # CPU-only llama-server for QEMU (no CUDA/Vulkan closures in the image).
  lemonseed.lemond.llamacppBackend = "cpu";

  # Allow serial login for interactive debugging in QEMU.
  services.getty.autologinUser = "root";
  users.users.root.hashedPassword = lib.mkForce ""; # empty password in qemu only

  # Console logging for the smoke-test serial log (see journaldConf).
  environment.etc."systemd/journald.conf.d/lemonseed-console.conf".source = journaldConf;

  # M8 P1: run the signing smoke against the host-served release sets. The
  # smoke service is QEMU-profile-only; production never ships it.
  systemd.services.lemonseed-signing-smoke = {
    description = "Lemonseed signing smoke: verify a signed release set + both refuse paths";
    after = [ "network-online.target" ];
    wants = [ "network-online.target" ];
    serviceConfig = {
      Type = "oneshot";
      ExecStart = "${signingSmoke}/bin/lemonseed-signing-smoke";
      # Console so the markers reach ttyS0 (journald forwards it to the log).
      StandardOutput = "journal+console";
      StandardError = "journal+console";
    };
    wantedBy = [ "multi-user.target" ];
  };
}
