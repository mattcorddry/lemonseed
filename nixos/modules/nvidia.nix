# Host NVIDIA stack for M5 (open kernel modules + unfree userspace).
#
# GTX 16xx / RTX 20+ (Turing and newer) use the open modules. Older GPUs need
# hardware.nvidia.open = false and possibly branch = "legacy_580".
# Drivers live in the SquashFS base; inference stays on the host (not NCT).

{ config, lib, pkgs, ... }:

let
  cfg = config.lemonseed.nvidia;
in
{
  options.lemonseed.nvidia = {
    enable = lib.mkEnableOption "NVIDIA GPU driver stack (CUDA backend for lemond)";
  };

  config = lib.mkIf cfg.enable {
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
      # nvidia-persistenced is disabled: on boxes without an NVIDIA GPU it
      # fails and systemd restarts it in a loop, spamming "NVRM: No NVIDIA
      # GPU found" to the console. lemond (CUDA) manages its own context.
      nvidiaPersistenced = false;
      branch = "production";
    };

    # Only load the NVIDIA modules when the hardware is present (see the
    # matching AMD rule in amd-gpu.nix): PCI class 03 (display/3D) with
    # vendor 10de = NVIDIA. Without it, boxes with only an AMD GPU load the
    # NVIDIA stack at boot and lemond's probe then loops "NVRM: No NVIDIA
    # GPU found" every second, blowing out the dmesg ring buffer.
    # (nvidia_uvm is pulled in eagerly by the upstream nixpkgs module; a lone
    # boot-time error line on AMD-only boxes is acceptable.)
    services.udev.extraRules = ''
      ACTION=="add", SUBSYSTEM=="pci", ENV{MODALIAS}=="pci:v000010ded*bc03sc*i*", RUN+="${pkgs.kmod}/bin/modprobe nvidia"
    '';

    # Upstream (open driver) loads nvidia_uvm eagerly at boot — headless boxes
    # included — which pulls `nvidia` in on AMD-only machines (kernel taint,
    # "NVRM: No NVIDIA GPU found", and lemond's short probe loop). Our udev
    # rule below loads `nvidia` only when the hardware is present, and
    # upstream's `softdep nvidia post: nvidia-uvm` then brings uvm along, so
    # the eager load only ever hurts. A list option cannot drop one element,
    # so restate the merged list (storage.nix: dm-mod/btrfs; nginx: tls; NixOS
    # defaults: the rest) without it — keep this in step when anything new
    # adds boot.kernelModules.
    boot.kernelModules = lib.mkForce [
      "atkbd"
      "br_netfilter"
      "bridge"
      "btrfs"
      "dm-mod"
      "loop"
      "tls"
      "veth"
      "xt_nat"
    ];

    # lemond probes PCI and will need /dev/nvidia* once CUDA backends land.
    users.users.lemonseed-lemond.extraGroups = [
      "video"
      "render"
    ];

    environment.systemPackages = [
      # nvidia-smi and related CLI tools for bring-up / /api/hardware later.
      config.hardware.nvidia.package.bin
    ];
  };
}
