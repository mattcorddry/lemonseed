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

  # Single source of truth for image references, shared with the control plane
  # (control-plane/image_pins.py) and the QEMU/flake image tarballs. Digest is
  # what we run; the tag is only a fallback for images that arrive without
  # RepoDigests (pullImage tarballs) and the human-facing name.
  imagePins = builtins.fromJSON (builtins.readFile ../../control-plane/container-images.json);
  postgresPin = imagePins.postgres;
  valkeyPin = imagePins.valkey;
  postgresImage = "${postgresPin.name}:${postgresPin.tag}";
  postgresRunRef = "${postgresPin.name}@${postgresPin.digest}";
  valkeyImage = "${valkeyPin.name}:${valkeyPin.tag}";
  valkeyRunRef = "${valkeyPin.name}@${valkeyPin.digest}";

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

  containerNeedsRecreate = ''
    image_present() {
      docker image inspect "$1" >/dev/null 2>&1
    }

    # Digest first (immutable, and what CI validated); tag only when the digest
    # is not on disk. Returns non-zero when neither is local.
    resolve_image() {
      if image_present "$1"; then
        echo "$1"
        return 0
      fi
      if image_present "$2"; then
        echo "$2"
        return 0
      fi
      return 1
    }

    # Reference a container is already running, for the log line on the paths
    # where this unit does not start one itself. Same rule as needs_recreate:
    # compare/report the reference string, never the image ID.
    container_image() {
      local have
      have="$(docker inspect -f '{{.Config.Image}}' "$1" 2>/dev/null || true)"
      if [[ -n "$have" ]]; then
        echo "$have"
      else
        echo "$2"
      fi
    }

    # Recreate when not on lemon, when the host publish is missing or is not
    # loopback-only, or when the container is running an image other than the
    # one we now pin (a pin bump from lemonseed-update had no effect before
    # M6.6 — nothing compared the image at all). Compare the *reference*
    # string, never the image ID: the classic image store reports the config
    # digest while the containerd store reports the manifest digest, so ID
    # compares can disagree with what we just ran and loop on every boot.
    needs_recreate() {
      local name="$1"
      local host_port="$2"
      local want_image="$3"
      if ! docker ps -a --format '{{.Names}}' | grep -qx "$name"; then
        return 1
      fi
      local mode ports have_image
      mode="$(docker inspect -f '{{.HostConfig.NetworkMode}}' "$name" 2>/dev/null || true)"
      ports="$(docker inspect -f '{{json .HostConfig.PortBindings}}' "$name" 2>/dev/null || true)"
      have_image="$(docker inspect -f '{{.Config.Image}}' "$name" 2>/dev/null || true)"
      if [[ "$mode" != "${lemonNetwork}" ]]; then
        return 0
      fi
      if [[ -n "$want_image" && "$have_image" != "$want_image" ]]; then
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
      run_image="$(resolve_image ${postgresRunRef} ${postgresImage} || true)"
      if needs_recreate ${postgresContainer} 5432 "$run_image"; then
        docker rm -f ${postgresContainer} >/dev/null
      elif docker ps --format '{{.Names}}' | grep -qx ${postgresContainer}; then
        # The containers use --restart unless-stopped, so dockerd usually has
        # them up again long before this oneshot unit runs. Announce the
        # reference on that path too: "which image is in play" is the first
        # question after a pin bump, and the QEMU smoke test asserts on it.
        echo "lemonseed-postgres: image $(container_image ${postgresContainer} "$run_image") (reusing the running container)"
        exit 0
      elif docker ps -a --format '{{.Names}}' | grep -qx ${postgresContainer}; then
        echo "lemonseed-postgres: image $(container_image ${postgresContainer} "$run_image") (starting the existing container)"
        exec docker start ${postgresContainer}
      fi
      if [[ -z "$run_image" ]]; then
        # Nothing local to run, and pulls no longer belong on the boot path.
        # The CP supervisor pulls off the critical path and reports progress
        # via /api/data, so boot continues to the interstitial instead of
        # stalling here on a slow WAN link.
        echo "lemonseed-postgres: image unknown; the control plane will pull it" >&2
        exit 0
      fi
      # Which reference we actually ran is the first question after a pin bump,
      # and on QEMU (images loaded from pullImage tarballs) it is the tag, not
      # the digest. The QEMU smoke test asserts on this line.
      echo "lemonseed-postgres: image $run_image (created)"
      exec docker run -d \
        --name ${postgresContainer} \
        --restart unless-stopped \
        --network ${lemonNetwork} \
        -p 127.0.0.1:5432:5432 \
        -v ${postgresData}:/var/lib/postgresql/data \
        --env-file ${postgresEnv} \
        "$run_image"
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
      run_image="$(resolve_image ${valkeyRunRef} ${valkeyImage} || true)"
      if needs_recreate ${valkeyContainer} 6379 "$run_image"; then
        docker rm -f ${valkeyContainer} >/dev/null
      elif docker ps --format '{{.Names}}' | grep -qx ${valkeyContainer}; then
        echo "lemonseed-valkey: image $(container_image ${valkeyContainer} "$run_image") (reusing the running container)"
        exit 0
      elif docker ps -a --format '{{.Names}}' | grep -qx ${valkeyContainer}; then
        echo "lemonseed-valkey: image $(container_image ${valkeyContainer} "$run_image") (starting the existing container)"
        exec docker start ${valkeyContainer}
      fi
      if [[ -z "$run_image" ]]; then
        echo "lemonseed-valkey: image unknown; the control plane will pull it" >&2
        exit 0
      fi
      echo "lemonseed-valkey: image $run_image (created)"
      exec docker run -d \
        --name ${valkeyContainer} \
        --restart unless-stopped \
        --network ${lemonNetwork} \
        -p 127.0.0.1:6379:6379 \
        -v ${valkeyData}:/data \
        "$run_image" \
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
        # No ExecStartPre pull (M6.6 S5.3): an unbounded `docker pull` here put
        # a ~160 MB transfer on the boot path with no progress signal, and on a
        # slow link it failed anyway while holding the unit in "starting". The
        # start script runs whatever is already local and hands the pull to the
        # CP supervisor.
        ExecStart = "${postgresStart}/bin/lemonseed-postgres-start";
        ExecStop = "${postgresStop}/bin/lemonseed-postgres-stop";
        # Local-only work now: docker inspect + run/start. Anything slower is
        # a stuck daemon, and failing fast lets the supervisor take over.
        TimeoutStartSec = "120";
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
