# Appliance NixOS configuration (M2: storage + control-plane stub).

{ config, pkgs, ... }:

{
  imports = [
    ./modules/version.nix
    ./modules/artifacts.nix
    ./modules/storage.nix
    ./modules/nvidia.nix
    ./modules/fhs-compat.nix
    ./modules/lemond.nix
    ./modules/data-services.nix
    ./modules/control-plane.nix
    ./modules/authelia.nix
    ./modules/gateway.nix
    ./modules/console.nix
    ./modules/metadata.nix
    ./modules/ab-root.nix
    ./modules/ab-boot.nix
    ./modules/update.nix
  ];

  # Setup owns the hostname, so NixOS must not: a non-empty networking.hostName
  # makes /etc/hostname a read-only store symlink and pins host-name= in
  # avahi-daemon.conf, which would override the name the user chose.
  # lemonseed-identity seeds /etc/hostname on @etc (default "lemonseed").
  networking.hostName = "";
  # The configured name is authoritative; never adopt one from the DHCP server.
  # dhcpcd still sends ours (its generated config enables `hostname`), so DHCP
  # and DDNS records track the portal URL from the next lease onward.
  networking.dhcpcd.setHostname = false;
  # Keep a meaningful closure name now that networking.hostName is empty.
  system.name = "lemonseed";

  # Keep the base image small and read-only oriented.
  # Console module enables empty-password root on tty2 for bring-up/debug.
  users.mutableUsers = false;
  users.allowNoPasswordLogin = true;

  # Default LTS-class kernel for NVIDIA open-module compatibility.
  # Avoid linuxPackages_latest (currently 7.x) — NVIDIA modules lag new kernels.
  boot.kernelPackages = pkgs.linuxPackages;

  # Soft goal: avoid initrd when practical. NixOS still builds one by default;
  # artifacts include it for practical boot until a no-initrd spike succeeds.
  # SquashFS root device is selected in ab-root.nix from lemonseed.slot=.

  # SquashFS /tmp is not writable (nginx, mktemp, systemd PrivateTmp, …).
  # Size-capped tmpfs: available before /var, cannot grow into model RAM.
  # Persistent scratch belongs on /var — do not symlink /tmp there.
  boot.tmp.useTmpfs = true;
  boot.tmp.tmpfsSize = "128M";

  # Initrd emergency shell when stage-1 fails (stage-2 root stays locked).
  boot.initrd.systemd.emergencyAccess = true;

  # init=/init is required by systemd-stage-1 find-nixos-closure.
  # Also set in boot/grub.cfg for the installed system.
  boot.kernelParams = [
    "console=ttyS0,115200n8"
    "console=tty0"
    "init=/init"
  ];

  boot.initrd.availableKernelModules = [
    "virtio_pci"
    "virtio_blk"
    "virtio_scsi"
    "sd_mod"
    "nvme"
    "ahci"
    "xhci_pci"
    "squashfs"
    "dm_mod"
    "btrfs"
  ];

  virtualisation.docker.enable = true;
  virtualisation.docker.enableOnBoot = true;

  lemonseed.dataServices.enable = true;
  lemonseed.authelia.enable = true;
  networking.useDHCP = true;
  networking.firewall.enable = true;

  # No Nix daemon / rebuild on the appliance image.
  nix.enable = false;

  environment.systemPackages = with pkgs; [
    coreutils
    util-linux
    iproute2
    iptables
    curl
    jq
  ];

  system.stateVersion = "26.05";
}
