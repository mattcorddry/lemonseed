# Control plane on localhost:8080 (public entry is nginx :80).

{ config, lib, pkgs, lemonadePackage, lemonadePythonSdk, ... }:

let
  cpSrc = ../../control-plane;
  bootSrc = ../../boot;
  # M9 P2: the in-tree modules directory — the module.yaml specs and the
  # host-side hooks. They ship beside the CP code so the runtime loader can
  # verify each spec's digest against the image that carries them (§4).
  modulesSrc = ../../modules;
  scriptsSrc = ../../scripts;
  # The SYSTEM panel's update job and failed-update detector spawn the same
  # updater the CLI and the weekly timer use (packaged once, see packages/).
  lemonseedUpdate = import ../packages/lemonseed-update.nix { inherit pkgs lib; };

  # M9 P2: the merged module catalog is a build artifact, not a checked-in
  # file. The merge step is the one place that validates every spec, so it
  # runs at build time with the strict reader — one invalid module fails the
  # build, because a catalog that half-works is worse than none (the installer
  # would offer a module the box cannot actually run).
  buildModulesCatalog = pkgs.writeShellApplication {
    name = "build-modules-catalog";
    runtimeInputs = [ (pkgs.python3.withPackages (ps: [ ps.pyyaml ])) ];
    text = ''
      exec "${pkgs.python3.withPackages (ps: [ ps.pyyaml ])}/bin/python3" \
        "${scriptsSrc}/build-modules-catalog.py" "$@"
    '';
  };

  cpRoot = pkgs.runCommand "lemonseed-control-plane-src" { } ''
    mkdir -p "$out/catalog" "$out/static/brand"
    cp ${cpSrc}/stub.py ${cpSrc}/hardware.py ${cpSrc}/catalog.py ${cpSrc}/chat_ready.py \
       ${cpSrc}/db.py ${cpSrc}/data_services.py ${cpSrc}/modules.py ${cpSrc}/auth.py \
       ${cpSrc}/sessions.py ${cpSrc}/srp.py ${cpSrc}/audit.py ${cpSrc}/usermgmt.py \
       ${cpSrc}/model_store.py ${cpSrc}/lemond_client.py ${cpSrc}/updates.py \
       ${cpSrc}/settings.py ${cpSrc}/journal.py ${cpSrc}/hardware_password.py \
       ${cpSrc}/owui_client.py \
       ${cpSrc}/owui_policy.py ${cpSrc}/owui_portal.py ${cpSrc}/owui-policy.json \
       ${cpSrc}/image_pins.py ${cpSrc}/container-images.json \
       ${cpSrc}/modules_catalog.py ${cpSrc}/module_manager.py \
       ${cpSrc}/mcp_proxy.py \
       ${cpSrc}/litellm.py \
       ${cpSrc}/words-en_US.txt "$out/"
    cp ${cpSrc}/catalog/models.json "$out/catalog/"
    cp -r ${cpSrc}/static/brand/. "$out/static/brand/"
    # M9 P2: build the merged module catalog into the image, then copy the
    # modules tree beside it. The loader reads the catalog from here (and
    # seeds a writable copy from it at boot), and re-hashes every module.yaml
    # against the image's own bytes.
    LEMONSEED_CP_DIR="$out" ${buildModulesCatalog}/bin/build-modules-catalog \
      --modules "${modulesSrc}" --out "$out/modules-catalog.json"
    mkdir -p "$out/modules"
    cp -r ${modulesSrc}/. "$out/modules/"
    # GRUB template for the A/B updater to refresh on the ESP (the installed
    # system keeps no other copy of boot/grub.cfg in SquashFS).
    mkdir -p "$out/lemonseed"
    cp ${bootSrc}/grub.cfg "$out/lemonseed/grub.cfg"
  '';

  # `redis` is the Valkey client for the M6.6 session store (sessions.py). Keep
  # this list runtime-only; test dependencies belong in scripts/run-unit-tests.sh.
  # There is deliberately no `bcrypt`: sign-in is SRP-6a (srp.py), so the CP
  # neither stores nor verifies a password hash (ARCHITECTURE.md §3.3).
  pythonEnv = pkgs.python3.withPackages (ps: [ ps.psycopg2 ps.requests ps.redis lemonadePythonSdk ]);

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
      lemonseedUpdate
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
      # Suppress kernel console spam (e.g. NVRM "No NVIDIA GPU found") below
      # crit level. Journald still captures everything; the console is not the
      # debug channel.
      ExecStartPre = [ "${pkgs.util-linux}/bin/dmesg -n crit" ];
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
