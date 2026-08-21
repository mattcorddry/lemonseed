# In-place A/B image updater for lab and production iteration.
#
# lemonseed-update writes the inactive SquashFS + ESP kernels, arms GRUB
# boot_once, and leaves user-data untouched. Lock-in (ab-boot.nix) promotes the
# new slot only after a healthy boot.

{
  pkgs,
  lib,
  ...
}:

let
  scripts = ../../scripts;
  pythonEnv = pkgs.python3;

  abHelper =
    pkgs.runCommand "lemonseed-ab"
      {
        nativeBuildInputs = [ pkgs.makeWrapper ];
      }
      ''
        mkdir -p "$out/bin"
        cp ${scripts}/lemonseed-ab.sh "$out/bin/lemonseed-ab"
        chmod 0755 "$out/bin/lemonseed-ab"
        wrapProgram "$out/bin/lemonseed-ab" \
          --prefix PATH : ${
            lib.makeBinPath [
              pkgs.coreutils
              pkgs.util-linux
              pkgs.gnugrep
              pkgs.gnused
              pkgs.grub2_efi
            ]
          }
      '';

  updater = pkgs.writeShellApplication {
    name = "lemonseed-update";
    runtimeInputs = with pkgs; [
      pythonEnv
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
      export PYTHONPATH=${scripts}
      export LEMONSEED_AB=${abHelper}/bin/lemonseed-ab
      export LEMONSEED_DOWNLOAD_DIR="''${LEMONSEED_DOWNLOAD_DIR:-/var/lib/lemonseed/download}"
      exec ${pythonEnv}/bin/python3 ${scripts}/lemonseed_update.py "$@"
    '';
  };
in
{
  environment.systemPackages = [ updater ];

  systemd.tmpfiles.rules = [
    "d /var/lib/lemonseed 0755 root root -"
    "d /var/lib/lemonseed/download 0755 root root -"
  ];
}
