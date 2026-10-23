# Installer HTTP service on :80.

{ pkgs, lib, ... }:

let
  installerRoot = ../../../installer;
  # The verifier ships beside the vendored pipeline on the ISO: both dirs go on
  # PYTHONPATH so the installer and the in-place updater share one verifier
  # (SIGNING.md §5 — one implementation, two callers, never two).
  scriptsRoot = ../../../scripts;
  # The trust set baked into the ISO (M8 P1): release-keys.json plus the key
  # files it lists. Nothing is fetched at runtime; the ISO carries it.
  releaseKeys = import ../../packages/release-keys.nix { inherit pkgs lib; keysDir = ../../../keys; };
  pythonEnv = pkgs.python3;

  lemonseedInstaller = pkgs.writeShellApplication {
    name = "lemonseed-installer";
    runtimeInputs = with pkgs; [
      pythonEnv
      openssl
      iproute2
      util-linux
      systemd
      curl
      coreutils
      gnused
      gawk
      dosfstools
      gptfdisk
      lvm2
      btrfs-progs
      bash
      grub2_efi
      efibootmgr
      cacert
    ];
    text = ''
      export PYTHONPATH=${installerRoot}:${scriptsRoot}
      export LEMONSEED_ROOT=/etc/lemonseed
      export LEMONSEED_REPART_DIR=/var/lib/lemonseed/repart.d
      export LEMONSEED_RELEASE_KEYS_DIR=${releaseKeys}
      export SSL_CERT_FILE=${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt
      cd ${installerRoot}
      exec ${pythonEnv}/bin/python3 ${installerRoot}/server.py
    '';
  };
in
{
  environment.systemPackages = [ lemonseedInstaller ];

  systemd.tmpfiles.rules = [
    "d /var/lib/lemonseed 0755 root root -"
    "d /var/lib/lemonseed/repart.d 0755 root root -"
    "d /var/lib/lemonseed/download 0755 root root -"
  ];

  systemd.services.lemonseed-installer = {
    description = "Lemonseed web installer";
    wantedBy = [ "multi-user.target" ];
    after = [
      "network-online.target"
      "avahi-daemon.service"
    ];
    wants = [
      "network-online.target"
      "avahi-daemon.service"
    ];
    path = with pkgs; [
      bash
      pythonEnv
      openssl
      systemd
      util-linux
      dosfstools
      gptfdisk
      lvm2
      btrfs-progs
      curl
      iproute2
      coreutils
      grub2_efi
      efibootmgr
      gnused
      gawk
    ];
    serviceConfig = {
      Type = "simple";
      ExecStart = "${lemonseedInstaller}/bin/lemonseed-installer";
      Restart = "on-failure";
      RestartSec = "2s";
      User = "root";
      Environment = [
        "LEMONSEED_INSTALLER_HOST=0.0.0.0"
        "LEMONSEED_INSTALLER_PORT=80"
        "LEMONSEED_ROOT=/etc/lemonseed"
        "LEMONSEED_REPART_DIR=/var/lib/lemonseed/repart.d"
        "LEMONSEED_RELEASE_KEYS_DIR=${releaseKeys}"
        "SSL_CERT_FILE=${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt"
      ];
    };
  };
}
