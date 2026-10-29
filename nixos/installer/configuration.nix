# Lemonseed USB installer (M4) — live EFI ISO / USB image.
#
# Separate from the appliance SquashFS profile. Boots straight into a
# network-facing web installer (http://lemonseed.local/).

{
  config,
  lib,
  pkgs,
  modulesPath,
  ...
}:

{
  imports = [
    "${modulesPath}/installer/cd-dvd/iso-image.nix"
    ./modules/networking.nix
    ./modules/console.nix
    ./modules/web.nix
  ];

  networking.hostName = "lemonseed";

  image.baseName = lib.mkForce "lemonseed-installer";
  isoImage.edition = lib.mkForce "installer";
  isoImage.makeEfiBootable = true;
  isoImage.makeUsbBootable = true;
  # Plain 80x25 text boot menu: no NixOS splash art, no gfxterm, no odd VGA
  # modes. The menu stays reachable (any key within the timeout) because the
  # web installer's source overrides are kernel command-line parameters
  # (lemonseed.image=, lemonseed.artifacts=) a developer edits via GRUB's 'e'
  # (RELEASE.md §4). The entry reads "Install Lemonseed AI".
  isoImage.forceTextMode = true;
  isoImage.grubTheme = lib.mkForce null;
  isoImage.prependToMenuLabel = "Install ";
  isoImage.appendToMenuLabel = lib.mkForce "";
  system.nixos.distroName = "Lemonseed AI";
  system.nixos.label = lib.mkForce "";
  boot.loader.timeout = lib.mkForce 3;

  # Installer is a mutable live system (not the read-only appliance).
  users.mutableUsers = false;
  users.users.root.initialHashedPassword = "";
  users.allowNoPasswordLogin = true;
  security.sudo.enable = true;

  boot.kernelParams = [
    "console=tty0"
    "console=ttyS0,115200n8"
  ];
  boot.kernelPackages = pkgs.linuxPackages;

  # Hardware bring-up for target PCs + USB ethernet NICs.
  # Prefer redistributable firmware only (keeps the ISO build free of unfree deps).
  hardware.enableRedistributableFirmware = true;

  environment.systemPackages = with pkgs; [
    coreutils
    util-linux
    iproute2
    curl
    jq
    gptfdisk
    dosfstools
    e2fsprogs
    btrfs-progs
    lvm2
    efibootmgr
    grub2_efi
    systemd # systemd-repart
    avahi
    pciutils
    usbutils
    lshw
  ];

  # Do not auto-activate leftover appliance VGs on the target disk.
  # udev + lvm2 would otherwise map appliance-user--data and systemd-repart
  # fails with "Failed to reread partition table: Device or resource busy".
  environment.etc."lvm/lvm.conf".text = lib.mkAfter ''
    activation/auto_activation_volume_list = []
  '';

  # Ship repart generator + defs for the install worker.
  environment.etc."lemonseed/disk-repart.sh".source = ../../scripts/disk-repart.sh;
  environment.etc."lemonseed/disk-release.sh".source = ../../scripts/disk-release.sh;
  environment.etc."lemonseed/gen-repart.py".source = ../../scripts/gen-repart.py;
  environment.etc."lemonseed/partitions.yaml".source = ../../partitions.yaml;
  environment.etc."lemonseed/grub.cfg".source = ../../boot/grub.cfg;
  environment.etc."lemonseed/lemonseed-ab.sh".source = ../../scripts/lemonseed-ab.sh;
  environment.etc."lemonseed/amdgpu-kernel-params.sh".source = ../../scripts/amdgpu-kernel-params.sh;

  # Keep the live image lean: no docker / appliance stack.
  nix.enable = false;
  documentation.enable = false;
  documentation.nixos.enable = false;

  system.stateVersion = "26.05";
}
