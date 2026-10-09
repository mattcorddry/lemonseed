# QEMU smoke-test profile: virtio root + user-data disk (see make run-qemu).
# Production images must NOT use this module (see DESIGN.md).

{ lib, pkgs, ... }:

let
  # Forward the journal to the SERIAL tty so `make test-qemu` captures
  # journald in the serial log (no console access to log in otherwise).
  # TTYPath must be /dev/ttyS0: with console=tty0 last in kernelParams,
  # /dev/console is the virtual terminal, invisible to the smoke harness.
  # Drop-in under etc (a systemd.services ExtraConfig key is not valid).
  journaldConf = pkgs.writeText "journald-console.conf" ''
    [Journal]
    ForwardToConsole=yes
    TTYPath=/dev/ttyS0
    MaxLevelConsole=info
  '';
in
{
  imports = [ ./qemu-docker-cache.nix ];

  # SquashFS presented as the first virtio disk.
  fileSystems."/" = lib.mkForce {
    device = "/dev/vda";
    fsType = "squashfs";
    options = [ "ro" ];
  };

  # Second virtio disk is the LVM PV candidate (whole-disk for QEMU).
  boot.kernelParams = [ "appliance.storage=/dev/vdb" ];

  # No GPU in the smoke VM — keep the image small and skip GPU driver stacks.
  lemonseed.nvidia.enable = lib.mkForce false;
  hardware.amd-gpu.enable = lib.mkForce false;

  # CPU-only llama-server for QEMU (no CUDA/Vulkan closures in the image).
  lemonseed.lemond.llamacppBackend = "cpu";

  # Allow serial login for interactive debugging in QEMU.
  services.getty.autologinUser = "root";
  users.users.root.hashedPassword = lib.mkForce ""; # empty password in qemu only

  # Console logging for the smoke-test serial log (see journaldConf).
  environment.etc."systemd/journald.conf.d/lemonseed-console.conf".source = journaldConf;
}
