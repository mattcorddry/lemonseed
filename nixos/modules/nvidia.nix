# Host NVIDIA stack for M5 (open kernel modules + unfree userspace).
#
# GTX 16xx / RTX 20+ (Turing and newer) use the open modules. Older GPUs need
# hardware.nvidia.open = false and possibly branch = "legacy_580".
# Drivers live in the SquashFS base; inference stays on the host (not NCT).

{ config, lib, pkgs, ... }:

{
  # Open modules still pull proprietary userspace (nvidia-x11, etc.).
  nixpkgs.config.allowUnfree = true;

  hardware.graphics.enable = true;

  # Loads the NVIDIA driver stack without enabling a display server.
  services.xserver.enable = false;
  services.xserver.videoDrivers = [ "nvidia" ];

  hardware.nvidia = {
    # Turing+ (GTX 1660, RTX 20/30/40/…). Required choice on driver >= 560.
    open = true;
    modesetting.enable = true;
    nvidiaSettings = false;
    # Keep devices initialized for headless CUDA/Vulkan clients.
    nvidiaPersistenced = true;
    branch = "production";
  };

  # lemond probes PCI and will need /dev/nvidia* once CUDA backends land.
  users.users.lemonseed-lemond.extraGroups = [
    "video"
    "render"
  ];

  environment.systemPackages = [
    # nvidia-smi and related CLI tools for bring-up / /api/hardware later.
    config.hardware.nvidia.package.bin
  ];
}
