# The A/B updater packaged for the read-only SquashFS appliance.
# Do not ship #!/usr/bin/env — the image has no /usr/bin/env (immutable root).
# Both the control plane (update job, failed-update detector) and the weekly
# check timer spawn this, so it is packaged once and imported by both modules.

{ pkgs, lib }:

let
  abHelper = import ./lemonseed-ab.nix { inherit pkgs lib; };

  # The trust set baked into this image (M8 P1): release-keys.json plus the
  # key files it lists, copied explicitly. The verifier reads it from here;
  # the store path ships inside the SquashFS, so nothing is fetched at runtime.
  releaseKeys = import ./release-keys.nix { inherit pkgs lib; keysDir = ../../keys; };

  # The ESP grub.cfg template the updater refreshes on every update. Staged
  # from the same boot/grub.cfg the control-plane image stages, because no
  # nixos module created either candidate path on the *installed* system —
  # which is why an installed box never refreshed its ESP grub.cfg except by
  # USB reinstall (bare-metal review, PLAN.md P5).
  grubCfgTemplate = pkgs.runCommand "lemonseed-grub-cfg" { } ''
    mkdir -p "$out/lemonseed"
    # ../../boot: this file is under nixos/packages/, and the template is the
    # repo-root boot/grub.cfg — the same one control-plane.nix stages as bootSrc.
    cp ${../../boot/grub.cfg} "$out/lemonseed/grub.cfg"
  '';
in
pkgs.writeShellApplication {
  name = "lemonseed-update";
  runtimeInputs = with pkgs; [
    python3
    openssl
    coreutils
    util-linux
    systemd
    curl
    jq
    grub2_efi
    findutils
    gnugrep
    gnused
    abHelper
  ];
  text = ''
    export PYTHONPATH=${../../scripts}
    export LEMONSEED_AB=${abHelper}/bin/lemonseed-ab
    # The baked trust set: the updater verifies release signatures against
    # exactly these keys before it trusts a manifest hash (M8 P1, D10/D13).
    export LEMONSEED_RELEASE_KEYS_DIR=${releaseKeys}
    export LEMONSEED_DOWNLOAD_DIR="''${LEMONSEED_DOWNLOAD_DIR:-/var/lib/lemonseed/download}"
    # The template path is exported with a fallback so a CP-spawned update
    # (which inherits the CP environment) and a CLI/systemd update (which
    # does not) resolve the same file.
    export LEMONSEED_GRUB_CFG="''${LEMONSEED_GRUB_CFG:-${grubCfgTemplate}/lemonseed/grub.cfg}"
    exec ${pkgs.python3}/bin/python3 ${../../scripts}/lemonseed_update.py "$@"
  '';
}
