# QEMU smoke-test profile: virtio root + user-data disk (see make run-qemu).
# Production images must NOT use this module (see DESIGN.md).

{ lib, ... }:

{
  # SquashFS presented as the first virtio disk.
  fileSystems."/" = lib.mkForce {
    device = "/dev/vda";
    fsType = "squashfs";
    options = [ "ro" ];
  };

  # Second virtio disk is the LVM PV candidate (whole-disk for QEMU).
  boot.kernelParams = [ "appliance.storage=/dev/vdb" ];

  # No GPU in the smoke VM — keep the image small and skip NVIDIA modules.
  services.xserver.videoDrivers = lib.mkForce [ ];

  # Allow serial login for interactive debugging in QEMU.
  services.getty.autologinUser = "root";
  users.users.root.hashedPassword = lib.mkForce ""; # empty password in qemu only
}
