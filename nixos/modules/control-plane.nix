# Control plane on localhost:8080 (public entry is nginx :80).

{ config, lib, pkgs, lemonadePackage, ... }:

let
  cpSrc = ../../control-plane;
  cpRoot = pkgs.runCommand "lemonseed-control-plane-src" { } ''
    mkdir -p "$out/catalog"
    cp ${cpSrc}/stub.py ${cpSrc}/hardware.py ${cpSrc}/catalog.py ${cpSrc}/chat_ready.py "$out/"
    cp ${cpSrc}/catalog/models.json "$out/catalog/"
  '';

  controlPlane = pkgs.writeShellApplication {
    name = "appliance-control-plane";
    runtimeInputs = [
      pkgs.python3
      pkgs.systemd
      pkgs.coreutils
      pkgs.docker
      pkgs.avahi
      pkgs.iproute2
      pkgs.util-linux
      pkgs.pciutils
      lemonadePackage
    ]
    ++ lib.optionals (builtins.elem "nvidia" config.services.xserver.videoDrivers) [
      config.hardware.nvidia.package.bin
    ];
    text = ''
      export PYTHONPATH=${cpRoot}
      export LEMONSEED_CATALOG=${cpRoot}/catalog/models.json
      exec ${pkgs.python3}/bin/python3 ${cpRoot}/stub.py
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
      # Localhost only — nginx is the public face on :80.
      Environment = [
        "APPLIANCE_CP_HOST=127.0.0.1"
        "APPLIANCE_CP_PORT=8080"
        "LEMONSEED_CATALOG=${cpRoot}/catalog/models.json"
        "PYTHONPATH=${cpRoot}"
      ];
      # Model pull/load scratch and chat-ready state live on @var.
      ReadWritePaths = [
        "/var/lib/lemonseed"
        "/etc"
        "/run/appliance"
        "/run/lemonseed"
      ];
    };
  };

  # /run is tmpfs; these must exist before ReadWritePaths mount setup.
  systemd.tmpfiles.rules = [
    "d /run/appliance 0755 root root -"
    "d /run/lemonseed 0755 root root -"
    "d /var/lib/lemonseed 0755 root root -"
  ];
}
