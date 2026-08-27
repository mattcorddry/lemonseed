# QEMU smoke profile: bake pinned Postgres + Valkey images into the guest so
# `make test-qemu` does not pull data-plane containers under TCG.
# Open WebUI is loaded from a host virtio-fs export (see qemu-api-smoke-test.sh)
# to avoid bloating squashfs and loading ~5GB before Postgres can start.
# Production appliance builds must NOT import this module.

{ lib, pkgs, ... }:

let
  inherit (pkgs.dockerTools) pullImage;

  stripShebang = content: lib.removePrefix "#!/usr/bin/env bash\n" content;

  postgresTar = pullImage {
    imageName = "pgvector/pgvector";
    imageDigest = "sha256:ccc6e83d6e35e931dc7c5def2022729d5a6c370318d099181995567ff1fb4d6b";
    finalImageTag = "pg16";
    hash = "sha256-U6UYjiGamk4h4XhBcg2uT7KsabAiL2huXshsE2mKiKc=";
  };
  valkeyTar = pullImage {
    imageName = "valkey/valkey";
    imageDigest = "sha256:e0eb7c480958d32bdc4357a74bdd70653ae15f2f9b4c93c4a5a9fad1dc471c84";
    finalImageTag = "8-alpine";
    hash = "sha256-cooDV26PtwfximfR/1zOReap5b7MTsIvZNxrcmIvYAM=";
  };

  loadDataImages = pkgs.writeShellApplication {
    name = "lemonseed-qemu-docker-load-data";
    runtimeInputs = [ pkgs.coreutils pkgs.docker ];
    text =
      let
        raw = stripShebang (builtins.readFile ../../scripts/qemu-docker-load-data.sh);
      in
      lib.replaceStrings
        [ "@POSTGRES_TAR@" "@VALKEY_TAR@" ]
        [ "${postgresTar}" "${valkeyTar}" ]
        raw;
  };

  loadOwuiImage = pkgs.writeShellApplication {
    name = "lemonseed-qemu-docker-load-owui";
    runtimeInputs = [ pkgs.coreutils pkgs.docker pkgs.findutils ];
    text = stripShebang (builtins.readFile ../../scripts/qemu-docker-load-owui.sh);
  };
in
{
  fileSystems."/run/lemonseed-qemu-owui" = {
    device = "lemonseedowui";
    fsType = "9p";
    options = [
      "trans=virtio"
      "version=9p2000.L"
      "cache=loose"
      "ro"
      "msize=1048576"
      "nofail"
    ];
  };

  systemd.services.lemonseed-qemu-docker-load-data = {
    description = "Load pre-fetched Postgres + Valkey images (QEMU smoke profile)";
    wantedBy = [ "multi-user.target" ];
    before = [
      "lemonseed-postgres.service"
      "lemonseed-valkey.service"
    ];
    after = [ "docker.service" ];
    requires = [ "docker.service" ];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
      ExecStart = "${loadDataImages}/bin/lemonseed-qemu-docker-load-data";
    };
  };

  boot.kernelModules = [
    "9p"
    "9pnet_virtio"
  ];

  systemd.services.lemonseed-qemu-docker-load-owui = {
    description = "Load Open WebUI image from host virtio export (QEMU smoke profile)";
    wantedBy = [ "multi-user.target" ];
    after = [
      "docker.service"
      "local-fs.target"
    ];
    requires = [ "docker.service" ];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
      ExecStart = "${loadOwuiImage}/bin/lemonseed-qemu-docker-load-owui";
      # Large image import under TCG can take a long time; do not block Postgres.
      TimeoutStartSec = "7200";
      StandardOutput = "journal+console";
      StandardError = "journal+console";
    };
  };

  # Let the module worker wait for virtio docker-load instead of failing DNS pulls.
  systemd.services.appliance-control-plane.environment = {
    LEMONSEED_OWUI_WAIT_LOCAL_SECONDS = "7200";
    # OWUI first boot under TCG easily exceeds the default 240s probe window.
    LEMONSEED_OWUI_READY_SECONDS = "3600";
  };
}
