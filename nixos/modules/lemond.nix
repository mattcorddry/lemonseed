# Headless embeddable Lemonade runtime.
#
# The lemond package is immutable in the SquashFS. Its configuration, models,
# backend cache and API key live on the durable user-data volume under /var.
# Nothing is exposed on the LAN: the control plane is its only client.

{
  lib,
  pkgs,
  lemonadePackage,
  ...
}:

let
  stateDir = "/var/lib/lemonseed/lemonade";
  modelsDir = "/var/lib/lemonseed/models";
  envFile = "${stateDir}/lemond.env";
  backendPath = "/etc/lemonseed/lemond-backends/llamacpp-cpu";

  defaults = (pkgs.formats.json { }).generate "lemonseed-lemond-defaults.json" {
    host = "127.0.0.1";
    port = 13305;
    no_broadcast = true;
    models_dir = modelsDir;
    # Deterministic CPU fallback until the NVIDIA/CUDA profile lands.
    llamacpp = {
      backend = "cpu";
      cpu_bin = backendPath;
    };
    max_loaded_models = 1;
  };

  # systemd loads EnvironmentFile= *before* ExecStartPre, so a missing file
  # would fail the unit on first boot (empty @var). Create state + key here,
  # then have ExecStart source the key itself.
  init = pkgs.writeShellApplication {
    name = "lemonseed-lemond-init";
    runtimeInputs = with pkgs; [
      coreutils
      openssl
    ];
    text = ''
      set -euo pipefail
      install -d -m 0755 -o root -g root /var/lib/lemonseed
      install -d -m 0750 -o lemonseed-lemond -g lemonseed-lemond \
        ${stateDir} ${stateDir}/cache ${modelsDir}
      if [[ ! -s ${envFile} ]]; then
        umask 077
        # Finite openssl output — avoid `tr | head` SIGPIPE under pipefail.
        key="$(openssl rand -hex 24)"
        printf 'LEMONADE_API_KEY=%s\n' "$key" > ${envFile}.tmp
        chown root:lemonseed-lemond ${envFile}.tmp
        chmod 0640 ${envFile}.tmp
        mv ${envFile}.tmp ${envFile}
      fi
    '';
  };

  # Source the API key after init created it, then exec lemond.
  start = pkgs.writeShellApplication {
    name = "lemonseed-lemond";
    runtimeInputs = [
      lemonadePackage
      pkgs.coreutils
      # The wrapper sets PATH for lemond, so this must be here (rather than
      # only in systemd.services.lemond.path) for its GPU probe to find lspci.
      pkgs.pciutils
    ];
    text = ''
      set -euo pipefail
      if [[ ! -f ${envFile} ]]; then
        echo "missing ${envFile}" >&2
        exit 1
      fi
      set -a
      # shellcheck disable=SC1091
      source ${envFile}
      set +a
      if [[ -z "''${LEMONADE_API_KEY:-}" ]]; then
        echo "LEMONADE_API_KEY empty in ${envFile}" >&2
        exit 1
      fi
      export HOME=${stateDir}
      export XDG_CACHE_HOME=${stateDir}/cache
      export LEMONADE_DEFAULTS_PATH=${defaults}
      exec ${lemonadePackage}/bin/lemond ${stateDir} --host 127.0.0.1 --port 13305
    '';
  };
in
{
  users.groups.lemonseed-lemond = { };
  users.users.lemonseed-lemond = {
    isSystemUser = true;
    group = "lemonseed-lemond";
  };

  environment.systemPackages = [
    lemonadePackage
    # lemond probes PCI devices with lspci to discover NVIDIA hardware.
    pkgs.pciutils
  ];

  # Create parents early so ProtectSystem/WorkingDirectory do not race an
  # empty @var mount. Ownership is finalized by ExecStartPre.
  systemd.tmpfiles.rules = [
    "d /var/lib/lemonseed 0755 root root -"
    "d ${stateDir} 0750 lemonseed-lemond lemonseed-lemond -"
    "d ${stateDir}/cache 0750 lemonseed-lemond lemonseed-lemond -"
    "d ${modelsDir} 0750 lemonseed-lemond lemonseed-lemond -"
  ];

  # Stable indirection: persisted Lemonade config must not point directly at
  # a garbage-collectable store path.
  environment.etc."lemonseed/lemond-backends/llamacpp-cpu".source =
    "${pkgs.llama-cpp}/bin/llama-server";

  systemd.services.lemond = {
    description = "Lemonseed embedded Lemonade inference API";
    wantedBy = [ "multi-user.target" ];
    after = [
      "local-fs.target"
      "systemd-tmpfiles-setup.service"
      "network-online.target"
    ];
    wants = [ "network-online.target" ];
    # Keep lspci on the service PATH: lemond executes it for GPU discovery.
    path = [ pkgs.coreutils pkgs.pciutils ];
    serviceConfig = {
      Type = "simple";
      User = "lemonseed-lemond";
      Group = "lemonseed-lemond";
      # Leading '-' : do not fail if @var was empty before init (tmpfiles race).
      WorkingDirectory = "-${stateDir}";
      # Do NOT use EnvironmentFile= here — systemd loads it before ExecStartPre
      # and fails on first boot when the file does not exist yet.
      Environment = [
        "HOME=${stateDir}"
        "LEMONADE_DEFAULTS_PATH=${defaults}"
        "XDG_CACHE_HOME=${stateDir}/cache"
      ];
      # '+' runs as root so we can create dirs/key, then drop privileges for start.
      ExecStartPre = "+${init}/bin/lemonseed-lemond-init";
      ExecStart = "${start}/bin/lemonseed-lemond";
      Restart = "on-failure";
      RestartSec = "3s";
      NoNewPrivileges = true;
      PrivateTmp = true;
      ProtectSystem = "strict";
      ReadWritePaths = [
        "/var/lib/lemonseed"
        stateDir
        modelsDir
      ];
      ProtectHome = true;
    };
  };

  # Path hint for the control plane; never expose the key itself via HTTP.
  environment.etc."lemonseed/lemond.env-path".text = "${envFile}\n";

  # Bring-up convenience only: root's interactive shells can use the shipped
  # `lemonade` CLI against local lemond without manually locating its API key.
  # The key remains out of HTTP responses and non-root shell environments.
  environment.etc."profile.d/lemonseed-lemond.sh".text = ''
    if [ "''${EUID:-$(id -u)}" -eq 0 ] && [ -r ${envFile} ]; then
      set -a
      . ${envFile}
      set +a
    fi
  '';
}
