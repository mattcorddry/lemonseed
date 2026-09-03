# Edge auth via Authelia (M6.2a): file backend + Valkey sessions, bound to
# 127.0.0.1:9091 and reachable only through nginx /auth/.
#
# Deliberately NOT the nixpkgs services.authelia module: its config must exist
# at build time, but the session secret is generated at first-admin bootstrap
# by the control plane. All Authelia state lives on user-data
# (/var/lib/authelia); the service idles until those files are derived.

{ config, lib, pkgs, ... }:

let
  stateDir = "/var/lib/authelia";
  resetMarker = "/var/lib/lemonseed/reset-admin";

  waitAndRun = pkgs.writeShellApplication {
    name = "lemonseed-authelia-start";
    runtimeInputs = [ pkgs.coreutils pkgs.authelia ];
    text = ''
    # Wait quietly for the control plane to derive the config (first-admin
    # bootstrap). The edge gate is off until then, so a stopped Authelia is
    # harmless pre-setup.
    while [[ ! -s ${stateDir}/config.yml || ! -s ${stateDir}/users.yaml ]]; do
      sleep 30
    done
    exec ${pkgs.authelia}/bin/authelia -c ${stateDir}/config.yml
    '';
  };

  # USB-installer lockout reset: the installer drops a marker on the ESP
  # (physical access only); relocate it to persistent user-data on first boot
  # so /setup can consume it. Must run before the control plane.
  relocateReset = pkgs.writeShellApplication {
    name = "lemonseed-auth-reset-seed";
    runtimeInputs = [ pkgs.coreutils pkgs.util-linux ];
    text = ''
    set -euo pipefail
    esp=/run/lemonseed-esp
    if [[ -e ${resetMarker} ]]; then
      exit 0
    fi
    # QEMU and most boots have no marker (and no ESP partition at all).
    if [[ ! -e /dev/disk/by-partlabel/ESP ]]; then
      exit 0
    fi
    mkdir -p "$${esp}"
    mounted=0
    if ! mountpoint -q "$${esp}"; then
      mount -t vfat -o rw /dev/disk/by-partlabel/ESP "$${esp}" || exit 0
      mounted=1
    fi
    trap '[ "$${mounted}" = "1" ] && umount "$${esp}" || true' EXIT
    if [[ -e "$${esp}/lemonseed-reset-admin" ]]; then
      install -d -m 0755 /var/lib/lemonseed
      install -m 0644 "$${esp}/lemonseed-reset-admin" "${resetMarker}.tmp"
      mv "${resetMarker}.tmp" "${resetMarker}"
      rm -f "$${esp}/lemonseed-reset-admin"
      echo "lemonseed-auth-reset-seed: staged admin reset for /setup"
    fi
    '';
  };
in
{
  options.lemonseed.authelia = {
    enable = lib.mkEnableOption "Authelia edge auth (file backend + Valkey sessions)";
  };

  config = lib.mkIf config.lemonseed.authelia.enable {
    users.groups.authelia = { };

    users.users.authelia = {
      isSystemUser = true;
      group = "authelia";
      description = "Authelia";
    };

    systemd.tmpfiles.rules = [
      # root writes config.yml/users.yaml (0640); the authelia user must write
      # db.sqlite3 + notifications.txt here, so the group needs rwx.
      "d ${stateDir} 0770 root authelia -"
    ];

    systemd.services.authelia = {
      description = "Authelia edge auth (Lemonseed)";
      wantedBy = [ "multi-user.target" ];
      after = [ "local-fs.target" "lemonseed-valkey.service" ];
      wants = [ "lemonseed-valkey.service" ];
      serviceConfig = {
        Type = "simple";
        User = "authelia";
        ExecStart = "${waitAndRun}/bin/lemonseed-authelia-start";
        # Restart loops while Valkey is down are transient: the data services
        # are resilient and the gate tolerates a down verifier pre-setup.
        Restart = "on-failure";
        RestartSec = "10s";
        StandardOutput = "journal+console";
        StandardError = "journal+console";
      };
    };

    systemd.services.lemonseed-auth-reset-seed = {
      description = "Stage USB-installer admin reset marker on user-data";
      wantedBy = [ "local-fs.target" ];
      after = [ "local-fs.target" ];
      before = [
        "appliance-control-plane.service"
        "nginx.service"
      ];
      path = [ pkgs.coreutils pkgs.util-linux ];
      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
        ExecStart = "${relocateReset}/bin/lemonseed-auth-reset-seed";
      };
    };
  };
}
