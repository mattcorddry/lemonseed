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
  nix-amd-ai,
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
  vulkanBackendPath = "/etc/lemonseed/lemond-backends/llamacpp-vulkan";
  # The Vulkan closure also provides llama-cli, which the control plane uses
  # for accurate GPU memory reporting (llama-cli --list-devices reports the
  # real GTT/VRAM size; vulkaninfo does not).
  vulkanLlamaCliPath = "/etc/lemonseed/lemond-backends/llamacpp-vulkan-cli";

  # Which llama-cpp closures to ship in the image. "auto" (the production
  # default) ships both CUDA and Vulkan so the control plane can select the
  # backend at runtime based on detected GPU vendor. Single-backend variants
  # keep the image lean for constrained profiles (QEMU = cpu only).
  enableCuda = cfg.llamacppBackend == "cuda" || cfg.llamacppBackend == "auto";
  enableVulkan = cfg.llamacppBackend == "vulkan" || cfg.llamacppBackend == "auto";

  # llama.cpp MUST track the version Lemonade 11.5.2 is built and pinned
  # against (b10273, per the lemonade package's backend_versions.json). An
  # independent older build breaks on new GGUF architectures (first casualty:
  # "eagle3" in newer GPT-OSS GGUFs, absent from b9190) and on multi-slot
  # server bugs (b9190's common_context_seq_rm aborts on lemond's
  # update_slots path — NVIDIA crash loop). These packages are built against
  # nix-amd-ai's pinned nixpkgs (the same one lemonadePackage uses) and carry
  # their patches (e.g. the RDNA3.5 numerics fix from llama.cpp#28211).
  #
  # nix-amd-ai ships CPU (llama-cpp) and Vulkan (llama-cpp-vulkan) builds of
  # the pinned b10273. It does NOT ship a CUDA build (b10236, a separate
  # lemonade-sdk release with sm_120/Blackwell codegen). We therefore build
  # llama-cpp from source with the *same pinned build number* — not from
  # nixpkgs' drifting version (b9190 on 26.05). Bump LLAMA_BUILD_NUMBER
  # together with the nix-amd-ai flake ref when rebasing; never let the CUDA
  # build number drift from the Vulkan/CPU ones.
  nixAmdPkgs = nix-amd-ai.packages.${pkgs.stdenv.hostPlatform.system} or nix-amd-ai.packages.x86_64-linux or nix-amd-ai.packages.aarch64-darwin;
  # Must equal the "cuda" entry in lemonade's backend_versions.json (b10236
  # for Lemonade 11.5.2). nixpkgs' llama-cpp no longer accepts a `buildNumber`
  # override (the argument was removed when the package moved to a single
  # hardcoded version), so we pin `version` + `src` directly to the b10236
  # tag to track Lemonade's curated CUDA release instead of the nixpkgs
  # channel version (b9190 on 26.05).
  LLAMA_BUILD_NUMBER = 10236;
  LLAMA_BUILD_NUMBER_STR = builtins.toString LLAMA_BUILD_NUMBER;
  llamaCppCuda = (pkgs.llama-cpp.overrideAttrs (final: prev: {
    version = LLAMA_BUILD_NUMBER_STR;
    npmDepsHash = "sha256-B7uEynAG70a3xauBKc20RuFa9cnWaWzVBCh+LPLBnIM=";
    src = pkgs.fetchFromGitHub {
      owner = "ggml-org";
      repo = "llama.cpp";
      tag = "b${LLAMA_BUILD_NUMBER_STR}";
      hash = "sha256-iIN2Y4I8pc6pImY8q8fnFZ6jyIzaccZHaAWf34QbAs8=";
      leaveDotGit = true;
      postFetch = ''
        git -C "$out" rev-parse --short HEAD > $out/COMMIT
        find "$out" -name .git -print0 | xargs -0 rm -rf
      '';
    };
  })).override { cudaSupport = true; };
  llamaCppVulkan = nixAmdPkgs.llama-cpp-vulkan;
  llamaCppCpu = nixAmdPkgs.llama-cpp;

  sourceApiKey = ''
    if [ "''${EUID:-$(id -u)}" -eq 0 ] && [ -r ${envFile} ]; then
      set -a
      # shellcheck disable=SC1090
      . ${envFile}
      set +a
    fi
  '';

  # lemond's config.json backend field: "auto" lets lemond accept any
  # --llamacpp flag the control plane passes at load time. Single-backend
  # profiles (QEMU cpu) pin the backend so a misconfigured load fails fast.
  lemondBackend = if cfg.llamacppBackend == "auto" then "auto" else cfg.llamacppBackend;

  defaults = (pkgs.formats.json { }).generate "lemonseed-lemond-defaults.json" {
    host = "127.0.0.1";
    port = 13305;
    no_broadcast = true;
    no_fetch_executables = true;
    models_dir = modelsDir;
    max_loaded_models = 1;
    # Upstream ships auto_check_model_updates=true, which re-fetches a model
    # whenever its upstream revision moves. On an appliance that turns a reboot
    # into a multi-gigabyte download and can fail outright offline; the cache is
    # the product here, so weights are only ever fetched on an admin's choice.
    auto_check_model_updates = false;
    llamacpp = {
      backend = lemondBackend;
      prefer_system = false;
      cpu_bin = cpuBackendPath;
    }
    // lib.optionalAttrs enableCuda { cuda_bin = cudaBackendPath; }
    // lib.optionalAttrs enableVulkan { vulkan_bin = vulkanBackendPath; };
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
        --arg backend ${lib.escapeShellArg lemondBackend} \
        --arg cpu ${lib.escapeShellArg cpuBackendPath} \
        --arg cuda ${lib.escapeShellArg cudaBackendPath} \
        --arg vulkan ${lib.escapeShellArg vulkanBackendPath} \
        --arg models ${lib.escapeShellArg modelsDir} \
        --argjson enableCuda ${if enableCuda then "true" else "false"} \
        --argjson enableVulkan ${if enableVulkan then "true" else "false"} \
        '
          .host = "127.0.0.1"
          | .port = 13305
          | .no_broadcast = true
          | .no_fetch_executables = true
          | .models_dir = $models
          | .auto_check_model_updates = false
          | .llamacpp.backend = $backend
          | .llamacpp.prefer_system = false
          | .llamacpp.cpu_bin = $cpu
          | if $enableCuda then .llamacpp.cuda_bin = $cuda else . end
          | if $enableVulkan then .llamacpp.vulkan_bin = $vulkan else . end
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
        "vulkan"
        "cpu"
        "auto"
      ];
      default = "auto";
      description = ''
        Which llama.cpp backend(s) to ship in the image and use for lemond.

        - "auto" (production default): ships both CUDA and Vulkan; the control
          plane selects the backend at runtime from detected GPU vendor.
        - "cuda": ships only the CUDA build (NVIDIA-only, smaller image).
        - "vulkan": ships only the Vulkan build (AMD-only, smaller image).
        - "cpu": ships only the CPU build (QEMU smoke profile).
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
          "${llamaCppCpu}/bin/llama-server";
        "lemonseed/lemond.env-path".text = "${envFile}\n";
        "profile.d/lemonseed-lemond.sh".text = sourceApiKey;
      }
      // lib.optionalAttrs enableCuda {
        "lemonseed/lemond-backends/llamacpp-cuda".source =
          "${llamaCppCuda}/bin/llama-server";
      }
      // lib.optionalAttrs enableVulkan {
        "lemonseed/lemond-backends/llamacpp-vulkan".source =
          "${llamaCppVulkan}/bin/llama-server";
        "lemonseed/lemond-backends/llamacpp-vulkan-cli".source =
          "${llamaCppVulkan}/bin/llama-cli";
      };

    systemd.services.lemond = {
      description = "Lemonseed embedded Lemonade inference API";
      wantedBy = [ "multi-user.target" ];
      after = [
        "local-fs.target"
        "systemd-tmpfiles-setup.service"
        "network-online.target"
      ]
      ++ lib.optionals (config.lemonseed.nvidia.enable && config.hardware.nvidia.nvidiaPersistenced) [
        "nvidia-persistenced.service"
      ];
      wants = [ "network-online.target" ]
        ++ lib.optionals (config.lemonseed.nvidia.enable && config.hardware.nvidia.nvidiaPersistenced) [
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
        # CUDA/Vulkan need writable executable mappings; keep GPU device nodes
        # visible (NVIDIA /dev/nvidia* and AMD /dev/dri/* + /dev/kfd for later
        # ROCm/HRX backends).
        MemoryDenyWriteExecute = false;
        PrivateDevices = false;
        DevicePolicy = "closed";
        DeviceAllow = [
          "char-nvidiactl"
          "char-nvidia-caps"
          "char-nvidia-frontend"
          "char-nvidia-uvm"
          "char-drm"
          "char-kfd"
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
