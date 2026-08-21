# Minimal /usr/bin and /bin compat for third-party tools (lemond) that hardcode
# FHS paths or reset PATH to /usr/bin:/bin. Baked into the SquashFS because
# activation cannot create these on a read-only root.

{ config, lib, pkgs, ... }:

{
  options.lemonseed.fhsBinLinks = lib.mkOption {
    type = lib.types.attrsOf lib.types.path;
    default = { };
    description = ''
      Basename → store path symlinks created under /usr/bin in the appliance
      SquashFS (and /bin/sh, /bin/bash when named sh/bash).
    '';
  };

  config.lemonseed.fhsBinLinks = {
    env = "${pkgs.coreutils}/bin/env";
    lscpu = "${pkgs.util-linux}/bin/lscpu";
    lspci = "${pkgs.pciutils}/bin/lspci";
    sh = "${pkgs.bash}/bin/sh";
    bash = "${pkgs.bashInteractive}/bin/bash";
  }
  // lib.optionalAttrs (builtins.elem "nvidia" config.services.xserver.videoDrivers) {
    # lemond hardcodes /usr/bin/nvidia-smi for GPU inventory.
    nvidia-smi = "${config.hardware.nvidia.package.bin}/bin/nvidia-smi";
  };
}
