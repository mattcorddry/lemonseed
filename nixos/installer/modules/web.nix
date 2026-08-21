# Installer HTTP service on :80.

{ pkgs, ... }:

let
  installerRoot = ../../../installer;
  pythonEnv = pkgs.python3;

  lemonseedInstaller = pkgs.writeShellApplication {
    name = "lemonseed-installer";
    runtimeInputs = with pkgs; [
      pythonEnv
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
      export PYTHONPATH=${installerRoot}
      export LEMONSEED_ROOT=/etc/lemonseed
      export LEMONSEED_REPART_DIR=/var/lib/lemonseed/repart.d
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
        "LEMONSEED_IMAGE_BASE_URL=http://test.lemonseed.ai/releases/stable/"
        "LEMONSEED_ROOT=/etc/lemonseed"
        "LEMONSEED_REPART_DIR=/var/lib/lemonseed/repart.d"
        "SSL_CERT_FILE=${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt"
      ];
    };
  };
}
