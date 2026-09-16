# Control plane on localhost:8080 (public entry is nginx :80).

{ config, lib, pkgs, lemonadePackage, ... }:

let
  cpSrc = ../../control-plane;
  bootSrc = ../../boot;
  cpRoot = pkgs.runCommand "lemonseed-control-plane-src" { } ''
    mkdir -p "$out/catalog" "$out/static/brand"
    cp ${cpSrc}/stub.py ${cpSrc}/hardware.py ${cpSrc}/catalog.py ${cpSrc}/chat_ready.py \
       ${cpSrc}/db.py ${cpSrc}/data_services.py ${cpSrc}/modules.py ${cpSrc}/auth.py "$out/"
    cp ${cpSrc}/catalog/models.json "$out/catalog/"
    cp -r ${cpSrc}/static/brand/. "$out/static/brand/"
    # GRUB template for the A/B updater to refresh on the ESP (the installed
    # system keeps no other copy of boot/grub.cfg in SquashFS).
    mkdir -p "$out/lemonseed"
    cp ${bootSrc}/grub.cfg "$out/lemonseed/grub.cfg"
  '';

  pythonEnv = pkgs.python3.withPackages (ps: [ ps.psycopg2 ps.bcrypt ]);

  controlPlane = pkgs.writeShellApplication {
    name = "appliance-control-plane";
    runtimeInputs = [
      pythonEnv
      pkgs.postgresql
      pkgs.systemd
      pkgs.coreutils
      pkgs.docker
      pkgs.nginx
      pkgs.avahi
      pkgs.iproute2
      pkgs.util-linux
      pkgs.pciutils
      lemonadePackage
    ]
    ++ lib.optionals config.lemonseed.nvidia.enable [
      config.hardware.nvidia.package.bin
    ]
    # vulkaninfo for AMD GPU detection in /api/hardware (see hardware.py).
    ++ lib.optionals config.hardware.amd-gpu.enable [ pkgs.vulkan-tools ];
    text = ''
      export PYTHONPATH=${cpRoot}
      export LEMONSEED_CATALOG=${cpRoot}/catalog/models.json
      # Updater source for ESP grub.cfg refresh (see lemonseed_update.py).
      export LEMONSEED_GRUB_CFG=${cpRoot}/lemonseed/grub.cfg
      exec ${pythonEnv}/bin/python3 ${cpRoot}/stub.py
    '';
  };
in
{
  environment.systemPackages = [ controlPlane ];

  systemd.services.appliance-control-plane = {
    description = "Lemonseed control plane";
    wantedBy = [ "multi-user.target" ];
    after = [
      "network-online.target"
      "local-fs.target"
      "docker.socket"
      "lemond.service"
    ];
    wants = [
      "network-online.target"
      "docker.socket"
      "lemond.service"
    ];
    serviceConfig = {
      Type = "simple";
      ExecStart = "${controlPlane}/bin/appliance-control-plane";
      Restart = "on-failure";
      RestartSec = "2s";
      StandardOutput = "journal";
      StandardError = "journal";
      Environment = [
        "APPLIANCE_CP_HOST=127.0.0.1"
        "APPLIANCE_CP_PORT=8080"
        "LEMONSEED_CATALOG=${cpRoot}/catalog/models.json"
        "PYTHONPATH=${cpRoot}"
      ];
      ReadWritePaths = [
        "/var/lib/lemonseed"
        "/etc"
        "/run/appliance"
        "/run/lemonseed"
      ];
    };
  };

  systemd.tmpfiles.rules = [
    "d /run/appliance 0755 root root -"
    "d /run/lemonseed 0755 root root -"
    "d /var/lib/lemonseed 0755 root root -"
  ];
}
