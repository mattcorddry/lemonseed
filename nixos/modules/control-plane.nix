# Control plane on localhost:8080 (public entry is nginx :80).

{ pkgs, ... }:

let
  stub = ../../control-plane/stub.py;
  controlPlane = pkgs.writeShellApplication {
    name = "appliance-control-plane";
    runtimeInputs = [
      pkgs.python3
      pkgs.systemd
      pkgs.coreutils
      pkgs.docker
      pkgs.avahi
      pkgs.iproute2
    ];
    text = ''
      exec ${pkgs.python3}/bin/python3 ${stub}
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
    ];
    wants = [
      "network-online.target"
      "docker.socket"
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
      ];
    };
  };
}
