# Shared PostgreSQL (+ pgvector) and Valkey for M6 module state.
# All Lemonseed containers join the user-defined Docker network "lemon".
# Host-facing ports bind only to 127.0.0.1 (never 0.0.0.0 / LAN NICs).

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
  lemonNetwork = "lemon";

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

  ensureNetwork = pkgs.writeShellApplication {
    name = "lemonseed-docker-network";
    runtimeInputs = with pkgs; [ coreutils docker ];
    text = ''
      set -euo pipefail
      if docker network inspect ${lemonNetwork} >/dev/null 2>&1; then
        exit 0
      fi
      docker network create --driver bridge ${lemonNetwork}
    '';
  };

  # Recreate when not on lemon, or host publish is missing / not loopback-only.
  containerNeedsRecreate = ''
    needs_recreate() {
      local name="$1"
      local host_port="$2"
      if ! docker ps -a --format '{{.Names}}' | grep -qx "$name"; then
        return 1
      fi
      local mode ports
      mode="$(docker inspect -f '{{.HostConfig.NetworkMode}}' "$name" 2>/dev/null || true)"
      ports="$(docker inspect -f '{{json .HostConfig.PortBindings}}' "$name" 2>/dev/null || true)"
      if [[ "$mode" != "${lemonNetwork}" ]]; then
        return 0
      fi
      # Require an explicit 127.0.0.1 publish (never 0.0.0.0 / empty HostIp).
      if ! grep -Fq '"HostIp":"127.0.0.1"' <<<"$ports"; then
        return 0
      fi
      if ! grep -Fq "\"HostPort\":\"$host_port\"" <<<"$ports"; then
        return 0
      fi
      return 1
    }
  '';

  postgresStart = pkgs.writeShellApplication {
    name = "lemonseed-postgres-start";
    runtimeInputs = with pkgs; [ coreutils docker gnugrep ];
    text = ''
      set -euo pipefail
      ${containerNeedsRecreate}
      # Inside the container Postgres may listen on *; exposure to the host is
      # only via -p 127.0.0.1:5432:5432 (never 0.0.0.0 on the host).
      if needs_recreate ${postgresContainer} 5432; then
        docker rm -f ${postgresContainer} >/dev/null
      elif docker ps --format '{{.Names}}' | grep -qx ${postgresContainer}; then
        exit 0
      elif docker ps -a --format '{{.Names}}' | grep -qx ${postgresContainer}; then
        exec docker start ${postgresContainer}
      fi
      exec docker run -d \
        --name ${postgresContainer} \
        --restart unless-stopped \
        --network ${lemonNetwork} \
        -p 127.0.0.1:5432:5432 \
        -v ${postgresData}:/var/lib/postgresql/data \
        --env-file ${postgresEnv} \
        ${postgresImage}
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
    runtimeInputs = with pkgs; [ coreutils docker gnugrep ];
    text = ''
      set -euo pipefail
      ${containerNeedsRecreate}
      # Bind all interfaces *inside* the container so peers on "lemon" can
      # connect; host publish stays 127.0.0.1-only.
      if needs_recreate ${valkeyContainer} 6379; then
        docker rm -f ${valkeyContainer} >/dev/null
      elif docker ps --format '{{.Names}}' | grep -qx ${valkeyContainer}; then
        exit 0
      elif docker ps -a --format '{{.Names}}' | grep -qx ${valkeyContainer}; then
        exec docker start ${valkeyContainer}
      fi
      exec docker run -d \
        --name ${valkeyContainer} \
        --restart unless-stopped \
        --network ${lemonNetwork} \
        -p 127.0.0.1:6379:6379 \
        -v ${valkeyData}:/data \
        ${valkeyImage} \
        valkey-server --save 60 1 --loglevel warning
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
      ensureNetwork
      postgresStart
      postgresStop
      valkeyStart
      valkeyStop
    ];

    systemd.services.lemonseed-data-init = {
      description = "Initialize Lemonseed data service directories and secrets";
      wantedBy = [ "multi-user.target" ];
      before = [
        "lemonseed-docker-network.service"
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

    systemd.services.lemonseed-docker-network = {
      description = "Ensure Lemonseed Docker network (lemon)";
      wantedBy = [ "multi-user.target" ];
      after = [
        "docker.service"
        "lemonseed-data-init.service"
      ];
      wants = [ "docker.service" ];
      before = [
        "lemonseed-postgres.service"
        "lemonseed-valkey.service"
      ];
      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
        ExecStart = "${ensureNetwork}/bin/lemonseed-docker-network";
      };
    };

    systemd.services.lemonseed-postgres = {
      description = "Lemonseed PostgreSQL (+ pgvector)";
      wantedBy = [ "multi-user.target" ];
      after = [
        "network-online.target"
        "docker.service"
        "lemonseed-docker-network.service"
      ];
      wants = [ "network-online.target" "docker.service" "lemonseed-docker-network.service" ];
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
        "lemonseed-docker-network.service"
      ];
      wants = [ "network-online.target" "docker.service" "lemonseed-docker-network.service" ];
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
