# Headless embeddable Lemonade runtime.
#
# The lemond package is immutable in the SquashFS. Its configuration, models,
# backend cache and API key live on the durable user-data volume under /var.
# Nothing is exposed on the LAN: the control plane is its only client.

{
  config,
  lib,
  pkgs,
  lemonadePackage,
  ...
}:

let
  cfg = config.lemonseed.lemond;

  stateDir = "/var/lib/lemonseed/lemonade";
  modelsDir = "/var/lib/lemonseed/models";
  envFile = "${stateDir}/lemond.env";
  cacheDir = "${stateDir}/cache";
  # lemond <cache_dir> stores config.json directly in cache_dir (not under
  # XDG_CACHE_HOME). Wrong path here silently no-oped all appliance policy.
  configJson = "${stateDir}/config.json";
  nestedConfigJson = "${cacheDir}/lemonade/config.json";
  tmpDir = "${stateDir}/tmp";

  cpuBackendPath = "/etc/lemonseed/lemond-backends/llamacpp-cpu";
  cudaBackendPath = "/etc/lemonseed/lemond-backends/llamacpp-cuda";

  # Skip the CUDA llama-cpp closure on CPU-only profiles (QEMU smoke).
  enableCuda = cfg.llamacppBackend != "cpu";
  llamaCppCuda = pkgs.llama-cpp.override { cudaSupport = true; };

  sourceApiKey = ''
    if [ "''${EUID:-$(id -u)}" -eq 0 ] && [ -r ${envFile} ]; then
      set -a
      # shellcheck disable=SC1090
      . ${envFile}
      set +a
    fi
  '';

  defaults = (pkgs.formats.json { }).generate "lemonseed-lemond-defaults.json" {
    host = "127.0.0.1";
    port = 13305;
    no_broadcast = true;
    no_fetch_executables = true;
    models_dir = modelsDir;
    max_loaded_models = 1;
    llamacpp = {
      backend = cfg.llamacppBackend;
      prefer_system = false;
      cpu_bin = cpuBackendPath;
    }
    // lib.optionalAttrs enableCuda { cuda_bin = cudaBackendPath; };
  };

  # systemd loads EnvironmentFile= *before* ExecStartPre, so a missing file
  # would fail the unit on first boot (empty @var). Create state + key here,
  # then have ExecStart source the key itself.
  init = pkgs.writeShellApplication {
    name = "lemonseed-lemond-init";
    runtimeInputs = with pkgs; [
      coreutils
      openssl
      jq
    ];
    text = ''
      set -euo pipefail
      install -d -m 0755 -o root -g root /var/lib/lemonseed
      install -d -m 0750 -o lemonseed-lemond -g lemonseed-lemond \
        ${stateDir} ${cacheDir} ${modelsDir} ${tmpDir}
      install -d -m 0750 -o lemonseed-lemond -g lemonseed-lemond \
        ${cacheDir}/lemonade

      if [[ ! -s ${envFile} ]]; then
        umask 077
        # Finite openssl output — avoid `tr | head` SIGPIPE under pipefail.
        key="$(openssl rand -hex 24)"
        printf 'LEMONADE_API_KEY=%s\n' "$key" > ${envFile}.tmp
        chown root:lemonseed-lemond ${envFile}.tmp
        chmod 0640 ${envFile}.tmp
        mv ${envFile}.tmp ${envFile}
      fi

      # Prefer the real lemond cache_dir config; migrate a mistaken nested path
      # from earlier appliance images if present.
      if [[ ! -f ${configJson} && -f ${nestedConfigJson} ]]; then
        cp -a ${nestedConfigJson} ${configJson}.tmp
        chown lemonseed-lemond:lemonseed-lemond ${configJson}.tmp
        mv ${configJson}.tmp ${configJson}
      fi

      # Mutable config.json overrides LEMONADE_DEFAULTS_PATH; re-apply appliance
      # backend policy on every start so image updates win.
      if [[ ! -f ${configJson} ]]; then
        printf '%s\n' '{}' > ${configJson}.tmp
        chown lemonseed-lemond:lemonseed-lemond ${configJson}.tmp
        chmod 0640 ${configJson}.tmp
        mv ${configJson}.tmp ${configJson}
      fi
      jq \
        --arg backend ${lib.escapeShellArg cfg.llamacppBackend} \
        --arg cpu ${lib.escapeShellArg cpuBackendPath} \
        --arg cuda ${lib.escapeShellArg cudaBackendPath} \
        --arg models ${lib.escapeShellArg modelsDir} \
        --argjson enableCuda ${if enableCuda then "true" else "false"} \
        '
          .host = "127.0.0.1"
          | .port = 13305
          | .no_broadcast = true
          | .no_fetch_executables = true
          | .models_dir = $models
          | .llamacpp.backend = $backend
          | .llamacpp.prefer_system = false
          | .llamacpp.cpu_bin = $cpu
          | if $enableCuda then .llamacpp.cuda_bin = $cuda else .
          end
        ' ${configJson} > ${configJson}.tmp
      chown lemonseed-lemond:lemonseed-lemond ${configJson}.tmp
      chmod 0640 ${configJson}.tmp
      mv ${configJson}.tmp ${configJson}
    '';
  };

  # Source the API key after init created it, then exec lemond.
  start = pkgs.writeShellApplication {
    name = "lemonseed-lemond";
    runtimeInputs = [
      lemonadePackage
      pkgs.coreutils
      # Wrapper PATH for lemond probes (lspci / lscpu) — also set on the unit.
      pkgs.pciutils
      pkgs.util-linux
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
      export XDG_CACHE_HOME=${cacheDir}
      export TMPDIR=${tmpDir}
      export LEMONADE_DEFAULTS_PATH=${defaults}
      exec ${lemonadePackage}/bin/lemond ${stateDir} --host 127.0.0.1 --port 13305
    '';
  };
in
{
  options.lemonseed.lemond = {
    llamacppBackend = lib.mkOption {
      type = lib.types.enum [
        "cuda"
        "cpu"
        "auto"
      ];
      default = "cuda";
      description = ''
        Default llama.cpp backend for lemond. Production appliances use CUDA;
        the QEMU smoke profile forces "cpu".
      '';
    };
  };

  config = {
    users.groups.lemonseed-lemond = { };
    users.users.lemonseed-lemond = {
      isSystemUser = true;
      group = "lemonseed-lemond";
      extraGroups = [
        "video"
        "render"
      ];
    };

    environment.systemPackages = [
      lemonadePackage
      # lemond probes PCI (lspci) and CPU (lscpu) for /v1/system-info.
      pkgs.pciutils
      pkgs.util-linux
    ];

    # Create parents early so ProtectSystem/WorkingDirectory do not race an
    # empty @var mount. Ownership is finalized by ExecStartPre.
    systemd.tmpfiles.rules = [
      "d /var/lib/lemonseed 0755 root root -"
      "d ${stateDir} 0750 lemonseed-lemond lemonseed-lemond -"
      "d ${cacheDir} 0750 lemonseed-lemond lemonseed-lemond -"
      "d ${cacheDir}/lemonade 0750 lemonseed-lemond lemonseed-lemond -"
      "d ${tmpDir} 0750 lemonseed-lemond lemonseed-lemond -"
      "d ${modelsDir} 0750 lemonseed-lemond lemonseed-lemond -"
    ];

    # Stable indirection: persisted Lemonade config must not point directly at
    # a garbage-collectable store path. Also path-hint + root shell key source.
    #
    # Lemonade 11.5.x execs *_bin verbatim as the server binary (file path).
    # Upstream docs say "directory"; that causes EISDIR on this build — see
    # lemonade commit 305af50.
    environment.etc =
      {
        "lemonseed/lemond-backends/llamacpp-cpu".source =
          "${pkgs.llama-cpp}/bin/llama-server";
        "lemonseed/lemond.env-path".text = "${envFile}\n";
        "profile.d/lemonseed-lemond.sh".text = sourceApiKey;
      }
      // lib.optionalAttrs enableCuda {
        "lemonseed/lemond-backends/llamacpp-cuda".source =
          "${llamaCppCuda}/bin/llama-server";
      };

    systemd.services.lemond = {
      description = "Lemonseed embedded Lemonade inference API";
      wantedBy = [ "multi-user.target" ];
      after = [
        "local-fs.target"
        "systemd-tmpfiles-setup.service"
        "network-online.target"
      ]
      ++ lib.optionals config.hardware.nvidia.nvidiaPersistenced [
        "nvidia-persistenced.service"
      ];
      wants = [ "network-online.target" ]
        ++ lib.optionals config.hardware.nvidia.nvidiaPersistenced [
          "nvidia-persistenced.service"
        ];
      # Keep probes on PATH; lemond may still reset PATH to /usr/bin:/bin.
      path = [
        pkgs.coreutils
        pkgs.pciutils
        pkgs.util-linux
      ];
      serviceConfig = {
        Type = "simple";
        User = "lemonseed-lemond";
        Group = "lemonseed-lemond";
        SupplementaryGroups = [
          "video"
          "render"
        ];
        # Leading '-' : do not fail if @var was empty before init (tmpfiles race).
        WorkingDirectory = "-${stateDir}";
        # Do NOT use EnvironmentFile= here — systemd loads it before ExecStartPre
        # and fails on first boot when the file does not exist yet.
        Environment = [
          "HOME=${stateDir}"
          "LEMONADE_DEFAULTS_PATH=${defaults}"
          "XDG_CACHE_HOME=${cacheDir}"
          # PrivateTmp isolates /tmp and /var/tmp; keep scratch on @var instead.
          "TMPDIR=${tmpDir}"
        ];
        # '+' runs as root so we can create dirs/key, then drop privileges for start.
        ExecStartPre = "+${init}/bin/lemonseed-lemond-init";
        ExecStart = "${start}/bin/lemonseed-lemond";
        Restart = "on-failure";
        RestartSec = "3s";
        NoNewPrivileges = true;
        # Backend/model scratch must not use the 128M root tmpfs.
        PrivateTmp = false;
        ProtectSystem = "strict";
        ProtectHome = true;
        # CUDA needs writable executable mappings; keep GPU device nodes visible.
        MemoryDenyWriteExecute = false;
        PrivateDevices = false;
        DevicePolicy = "closed";
        DeviceAllow = [
          "char-nvidiactl"
          "char-nvidia-caps"
          "char-nvidia-frontend"
          "char-nvidia-uvm"
          "char-drm"
        ];
        ReadWritePaths = [
          "/var/lib/lemonseed"
          stateDir
          modelsDir
        ];
      };
    };

    # Bring-up convenience: root shells (login + interactive bash) get the API
    # key for the shipped `lemonade` CLI. Non-root shells are unchanged.
    # profile.d is above; interactive bash sources interactiveShellInit.
    environment.interactiveShellInit = lib.mkAfter sourceApiKey;
  };
}
