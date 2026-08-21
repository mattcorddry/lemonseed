# LVM user-data provisioning + btrfs mounts for /etc and /var.
#
# NixOS treats /etc and /var as needed-for-boot, so they are mounted in the
# initrd. The provisioner therefore runs in systemd-initrd before those mounts.

{
  lib,
  pkgs,
  ...
}:

let
  # Strip shebang; writeShellApplication supplies its own.
  scriptBody =
    let
      raw = builtins.readFile ../../scripts/appliance-storage-provision.sh;
      noShebang = lib.removePrefix "#!/usr/bin/env bash\n" raw;
    in
    noShebang;

  provisionScript = pkgs.writeShellApplication {
    name = "appliance-storage-provision";
    runtimeInputs = with pkgs; [
      coreutils
      util-linux
      lvm2
      btrfs-progs
    ];
    text = scriptBody;
  };

  # VG "appliance" + LV "user-data" → mapper name with escaped hyphen.
  dataDevice = "/dev/mapper/appliance-user--data";

  mountOptionsCommon = [
    "rw"
    "nodev"
    "nosuid"
    # Wait for provisioner to create the LV, not only udev to notice a
    # pre-existing device (first boot has no LV until we make it).
    "x-systemd.requires=appliance-storage-provision.service"
    "x-systemd.after=appliance-storage-provision.service"
    "x-systemd.device-timeout=180"
  ];
in
{
  services.lvm.enable = true;

  boot.initrd.systemd.enable = true;

  boot.initrd.kernelModules = [
    "dm-mod"
    "btrfs"
  ];
  boot.kernelModules = [
    "dm-mod"
    "btrfs"
  ];

  boot.initrd.systemd.storePaths = [ provisionScript ];

  boot.initrd.systemd.services.appliance-storage-provision = {
    description = "Provision appliance LVM/btrfs user-data if missing";
    wantedBy = [ "initrd.target" ];
    before = [
      "sysroot-etc.mount"
      "sysroot-var.mount"
      "initrd-fs.target"
    ];
    after = [
      "sysroot.mount"
      "systemd-modules-load.service"
      "systemd-udev-trigger.service"
      "systemd-udev-settle.service"
    ];
    # Mounts pull us in via x-systemd.requires=; also require ourselves for
    # initrd-fs so a skip/failure cannot be silently ignored.
    requiredBy = [
      "sysroot-etc.mount"
      "sysroot-var.mount"
    ];
    unitConfig = {
      DefaultDependencies = false;
      RequiresMountsFor = [ "/sysroot" ];
    };
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
      ExecStart = "${provisionScript}/bin/appliance-storage-provision";
      StandardOutput = "journal+console";
      StandardError = "journal+console";
      # Fail the boot if provisioning fails (do not exit-0-skip in initrd).
      Environment = [ "APPLIANCE_STORAGE_REQUIRED=1" ];
      TimeoutStartSec = "180s";
    };
  };

  environment.systemPackages = with pkgs; [
    lvm2
    btrfs-progs
    provisionScript
  ];

  systemd.services.appliance-storage-provision = {
    description = "Provision appliance LVM/btrfs user-data if missing";
    wantedBy = [ "local-fs-pre.target" ];
    before = [
      "local-fs-pre.target"
      "etc.mount"
      "var.mount"
    ];
    after = [
      "systemd-udev-settle.service"
      "systemd-modules-load.service"
    ];
    unitConfig = {
      DefaultDependencies = false;
    };
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
      ExecStart = "${provisionScript}/bin/appliance-storage-provision";
      Environment = [ "APPLIANCE_STORAGE_REQUIRED=1" ];
    };
  };

  fileSystems."/etc" = {
    device = dataDevice;
    fsType = "btrfs";
    options = mountOptionsCommon ++ [
      "subvol=@etc"
      "noexec"
    ];
  };

  fileSystems."/var" = {
    device = dataDevice;
    fsType = "btrfs";
    options = mountOptionsCommon ++ [
      "subvol=@var"
    ];
  };
}
