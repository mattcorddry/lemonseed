# Shared PostgreSQL (+ pgvector) and Valkey for M6 module state.
# Localhost-only; data under /var/lib/lemonseed/data/ on user-data.

{ config, lib, pkgs, ... }:

let
  cfg = config.lemonseed.dataServices;

  dataRoot = "/var/lib/lemonseed/data";
  secretsDir = "${dataRoot}/secrets";
  postgresData = "${dataRoot}/postgres";
  valkeyData = "${dataRoot}/valkey";
  postgresEnv = "${secretsDir}/postgres.env";
  postgresImage = "pgvector/pgvector:pg16";
  valkeyImage = "valkey/valkey:8-alpine";
  postgresContainer = "lemonseed-postgres";
  valkeyContainer = "lemonseed-valkey";

  init = pkgs.writeShellApplication {
    name = "lemonseed-data-init";
    runtimeInputs = with pkgs; [ coreutils openssl ];
    text = ''
      set -euo pipefail
      install -d -m 0750 -o root -g root ${dataRoot}
      install -d -m 0750 -o root -g root ${secretsDir}
      install -d -m 0750 -o root -g root "${secretsDir}/modules"
      install -d -m 0750 -o root -g root ${postgresData}
      install -d -m 0750 -o root -g root ${valkeyData}

      if [[ ! -s ${postgresEnv} ]]; then
        umask 077
        pw="$(openssl rand -hex 24)"
        cat > ${postgresEnv}.tmp <<EOF
POSTGRES_USER=postgres
POSTGRES_PASSWORD=$pw
EOF
        chmod 0640 ${postgresEnv}.tmp
        mv ${postgresEnv}.tmp ${postgresEnv}
      fi
    '';
  };

  postgresStart = pkgs.writeShellApplication {
    name = "lemonseed-postgres-start";
    runtimeInputs = with pkgs; [ coreutils docker ];
    text = ''
      set -euo pipefail
      if docker ps --format '{{.Names}}' | grep -qx ${postgresContainer}; then
        mode="$(docker inspect -f '{{.HostConfig.NetworkMode}}' ${postgresContainer} 2>/dev/null || true)"
        if [[ "$mode" == "host" ]]; then
          exit 0
        fi
        docker rm -f ${postgresContainer} >/dev/null
      elif docker ps -a --format '{{.Names}}' | grep -qx ${postgresContainer}; then
        mode="$(docker inspect -f '{{.HostConfig.NetworkMode}}' ${postgresContainer} 2>/dev/null || true)"
        if [[ "$mode" == "host" ]]; then
          exec docker start ${postgresContainer}
        fi
        docker rm -f ${postgresContainer} >/dev/null
      fi
      # Host networking so CP connections appear as 127.0.0.1 (published ports
      # otherwise show up as Docker bridge 172.17.0.1 and fail pg_hba).
      exec docker run -d \
        --name ${postgresContainer} \
        --restart unless-stopped \
        --network host \
        -v ${postgresData}:/var/lib/postgresql/data \
        --env-file ${postgresEnv} \
        ${postgresImage} \
        -c listen_addresses=127.0.0.1
    '';
  };

  postgresStop = pkgs.writeShellApplication {
    name = "lemonseed-postgres-stop";
    runtimeInputs = [ pkgs.docker ];
    text = ''
      set -euo pipefail
      docker stop ${postgresContainer} 2>/dev/null || true
    '';
  };

  valkeyStart = pkgs.writeShellApplication {
    name = "lemonseed-valkey-start";
    runtimeInputs = with pkgs; [ coreutils docker ];
    text = ''
      set -euo pipefail
      if docker ps --format '{{.Names}}' | grep -qx ${valkeyContainer}; then
        mode="$(docker inspect -f '{{.HostConfig.NetworkMode}}' ${valkeyContainer} 2>/dev/null || true)"
        if [[ "$mode" == "host" ]]; then
          exit 0
        fi
        docker rm -f ${valkeyContainer} >/dev/null
      elif docker ps -a --format '{{.Names}}' | grep -qx ${valkeyContainer}; then
        mode="$(docker inspect -f '{{.HostConfig.NetworkMode}}' ${valkeyContainer} 2>/dev/null || true)"
        if [[ "$mode" == "host" ]]; then
          exec docker start ${valkeyContainer}
        fi
        docker rm -f ${valkeyContainer} >/dev/null
      fi
      exec docker run -d \
        --name ${valkeyContainer} \
        --restart unless-stopped \
        --network host \
        -v ${valkeyData}:/data \
        ${valkeyImage} \
        valkey-server --bind 127.0.0.1 --save 60 1 --loglevel warning
    '';
  };

  valkeyStop = pkgs.writeShellApplication {
    name = "lemonseed-valkey-stop";
    runtimeInputs = [ pkgs.docker ];
    text = ''
      set -euo pipefail
      docker stop ${valkeyContainer} 2>/dev/null || true
    '';
  };
in
{
  options.lemonseed.dataServices = {
    enable = lib.mkEnableOption "shared PostgreSQL and Valkey data services";
  };

  config = lib.mkIf cfg.enable {
    environment.systemPackages = [
      init
      postgresStart
      postgresStop
      valkeyStart
      valkeyStop
    ];

    systemd.services.lemonseed-data-init = {
      description = "Initialize Lemonseed data service directories and secrets";
      wantedBy = [ "multi-user.target" ];
      before = [
        "lemonseed-postgres.service"
        "lemonseed-valkey.service"
      ];
      after = [ "local-fs.target" ];
      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
        ExecStart = "${init}/bin/lemonseed-data-init";
      };
    };

    systemd.services.lemonseed-postgres = {
      description = "Lemonseed PostgreSQL (+ pgvector)";
      wantedBy = [ "multi-user.target" ];
      after = [
        "network-online.target"
        "docker.service"
        "lemonseed-data-init.service"
      ];
      wants = [ "network-online.target" "docker.service" ];
      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
        ExecStartPre = "-${pkgs.docker}/bin/docker pull ${postgresImage}";
        ExecStart = "${postgresStart}/bin/lemonseed-postgres-start";
        ExecStop = "${postgresStop}/bin/lemonseed-postgres-stop";
        TimeoutStartSec = "300";
      };
    };

    systemd.services.lemonseed-valkey = {
      description = "Lemonseed Valkey (Redis-compatible)";
      wantedBy = [ "multi-user.target" ];
      after = [
        "network-online.target"
        "docker.service"
        "lemonseed-data-init.service"
      ];
      wants = [ "network-online.target" "docker.service" ];
      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
        ExecStartPre = "-${pkgs.docker}/bin/docker pull ${valkeyImage}";
        ExecStart = "${valkeyStart}/bin/lemonseed-valkey-start";
        ExecStop = "${valkeyStop}/bin/lemonseed-valkey-stop";
        TimeoutStartSec = "120";
      };
    };

    systemd.tmpfiles.rules = [
      "d ${dataRoot} 0750 root root -"
      "d ${secretsDir} 0750 root root -"
      "d ${secretsDir}/modules 0750 root root -"
      "d ${postgresData} 0750 root root -"
      "d ${valkeyData} 0750 root root -"
    ];
  };
}
