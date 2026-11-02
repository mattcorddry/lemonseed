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
      cryptsetup
      # systemd-ask-password (Hardware-password fallback, M8 P7) and systemctl
      # (the bounded unlock-retry reboot/halt, D-E2) come from the systemd
      # package, which the initrd and stage 2 already carry.
      systemd
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

  # The LUKS2 `crypt` target needs the cipher/block-mode modules in the
  # initrd as well (aes/xts/…, the same set NixOS's own LUKS support ships);
  # without them modprobe dm-crypt fails and every unlock reports
  # "crypt: unknown target type" regardless of the key (round-3 finding).
  # Names verified against the 6.18.44 kernel modules dir: the modules-shrunk
  # builder fails hard on any root module that has no .ko (no aes.ko or
  # aes_x86_64 in 6.18 — aes comes from aesni_intel; ecb/sha256/sha512 are
  # built-in; blowfish/twofish/serpent only exist as *_generic).
  boot.initrd.availableKernelModules = [
    "dm_mod"
    "dm_crypt"
    "aes"
    "aesni_intel"
    "xts"
    "cryptd"
    "cbc"
    "lrw"
    "af_alg"
    "algif_skcipher"
    "blowfish_generic"
    "twofish_generic"
    "serpent_generic"
  ];
  boot.initrd.kernelModules = [
    "dm-mod"
    "btrfs"
    # LUKS2 under the PV (M8 P7): cryptsetup needs dm-crypt, and the TPM
    # token path needs the TPM frontends loaded before the provisioner's
    # unlock step runs (hardware frontends: crb for the usual firmware
    # interface, tis for the SPI/older ones). The cipher pieces (aes/xts/
    # cryptd) are loaded eagerly too — udev coldplug only loads device
    # drivers, so waiting for them to be requested is not deterministic
    # inside the initrd (round-3 boot-loop finding).
    "dm-crypt"
    "aesni_intel"
    "xts"
    "cryptd"
    "tpm_crb"
    "tpm_tis"
  ];
  boot.kernelModules = [
    "dm-mod"
    "btrfs"
    "dm-crypt"
    "tpm_crb"
    "tpm_tis"
  ];

  boot.initrd.systemd.storePaths = [
    provisionScript
    # The wrapper's runtimeInputs are store paths, not initrd closures —
    # pull the unlock tooling in explicitly (M8 P7; systemd itself is
    # already in the systemd-initrd closure, but list it so the wrapper's
    # PATH always resolves).
    pkgs.cryptsetup
    pkgs.systemd
  ];

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
      # The TPM-token unlock dlopens the systemd token plugin — off cryptsetup's
      # default search path (M8 P7 bare-metal review).
      Environment = [
        "APPLIANCE_STORAGE_REQUIRED=1"
        "LD_LIBRARY_PATH=${pkgs.systemd}/lib/cryptsetup"
      ];
      TimeoutStartSec = "180s";
    };
  };

  environment.systemPackages = with pkgs; [
    lvm2
    btrfs-progs
    cryptsetup
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
      Environment = [
        "APPLIANCE_STORAGE_REQUIRED=1"
        "LD_LIBRARY_PATH=${pkgs.systemd}/lib/cryptsetup"
      ];
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
