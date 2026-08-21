# Slot-aware SquashFS root: GRUB passes lemonseed.slot=a|b.
#
# systemd-fstab-generator runs before any service, so a rewritten fstab is too
# late to create sysroot.mount. A generator emits sysroot.mount from the real
# GPT PARTLABEL root-{slot}. Stage-2 keeps a stable lemonseed-root alias.

{
  lib,
  pkgs,
  config,
  ...
}:

let
  inherit (lib)
    elem
    filter
    attrValues
    concatStringsSep
    unique
    ;

  selectRoot = pkgs.writeShellScript "lemonseed-select-root" ''
    set -euo pipefail
    slot=a
    if [[ -r /proc/cmdline ]]; then
      # shellcheck disable=SC2013
      for arg in $(cat /proc/cmdline); do
        case "$arg" in
          lemonseed.slot=*)
            slot="''${arg#lemonseed.slot=}"
            ;;
        esac
      done
    fi
    case "$slot" in
      a|b) ;;
      *)
        echo "lemonseed-select-root: invalid slot '$slot'; using a" >&2
        slot=a
        ;;
    esac

    target=""
    n=0
    while [[ "$n" -lt 120 ]]; do
      if [[ -e "/dev/disk/by-partlabel/root-$slot" ]]; then
        target="$(readlink -f "/dev/disk/by-partlabel/root-$slot")"
        break
      fi
      sleep 0.25
      n=$((n + 1))
    done
    if [[ -z "$target" || ! -b "$target" ]]; then
      echo "lemonseed-select-root: PARTLABEL=root-$slot not found" >&2
      exit 1
    fi

    mkdir -p /dev/disk/by-partlabel
    ln -sfn "$target" /dev/disk/by-partlabel/lemonseed-root
    echo "lemonseed-select-root: slot=$slot -> $target"
  '';

  # Runs at systemd generator time (before any units). Writes sysroot.mount
  # pointing at the real GPT label so udev can activate the .device unit.
  # No-ops when lemonseed.slot= is absent (QEMU uses root=/dev/vda instead).
  rootGenerator = pkgs.writeShellScript "lemonseed-root-generator" ''
    set -euo pipefail
    dest="''${1:-/run/systemd/generator}"

    slot=""
    if [[ -r /proc/cmdline ]]; then
      # shellcheck disable=SC2013
      for arg in $(cat /proc/cmdline); do
        case "$arg" in
          lemonseed.slot=*)
            slot="''${arg#lemonseed.slot=}"
            ;;
        esac
      done
    fi
    case "$slot" in
      a|b) ;;
      *)
        exit 0
        ;;
    esac

    what="/dev/disk/by-partlabel/root-$slot"
    mkdir -p "$dest/initrd-root-fs.target.requires"
    {
      echo "[Unit]"
      echo "Description=Lemonseed SquashFS root (slot $slot)"
      echo "DefaultDependencies=no"
      echo "After=systemd-modules-load.service systemd-udev-trigger.service"
      echo "Before=initrd-root-fs.target initrd-fs.target"
      echo "Conflicts=umount.target"
      echo
      echo "[Mount]"
      echo "What=$what"
      echo "Where=/sysroot"
      echo "Type=squashfs"
      echo "Options=ro"
    } > "$dest/sysroot.mount"
    ln -sfn ../sysroot.mount "$dest/initrd-root-fs.target.requires/sysroot.mount"
    echo "lemonseed-root-generator: sysroot.mount -> $what" >&2
  '';

  initrdFstabLine =
    fs:
    let
      opts = unique (fs.options ++ [ "x-initrd.mount" ]);
    in
    "${fs.device} ${fs.mountPoint} ${fs.fsType} ${concatStringsSep "," opts} 0 0";
in
{
  config =
    let
      # PID 1 generators read this before any service. Omit / so nothing waits
      # on the fake lemonseed-root PARTLABEL; the generator supplies sysroot.mount.
      initrdFstabNoRoot = pkgs.writeText "initrd-fstab-no-root" (
        concatStringsSep "\n" (
          map initrdFstabLine (
            filter (fs: elem fs.mountPoint [
              "/etc"
              "/var"
            ]) (attrValues config.fileSystems)
          )
        )
        + "\n"
      );
    in
    {
      fileSystems."/" = {
        device = "/dev/disk/by-partlabel/lemonseed-root";
        fsType = "squashfs";
        options = [ "ro" ];
      };

      boot.initrd.systemd.storePaths = [
        rootGenerator
        initrdFstabNoRoot
      ];

      boot.initrd.systemd.contents."/etc/systemd/system-generators/lemonseed-root-generator".source =
        rootGenerator;

      boot.initrd.systemd.managerEnvironment.SYSTEMD_SYSROOT_FSTAB = lib.mkForce initrdFstabNoRoot;

      systemd.services.lemonseed-select-root = {
        description = "Select A/B SquashFS root alias from lemonseed.slot=";
        wantedBy = [ "local-fs-pre.target" ];
        before = [
          "local-fs-pre.target"
          "systemd-remount-fs.service"
        ];
        after = [ "systemd-udev-trigger.service" ];
        unitConfig.DefaultDependencies = false;
        serviceConfig = {
          Type = "oneshot";
          RemainAfterExit = true;
          ExecStart = selectRoot;
          StandardOutput = "journal+console";
          StandardError = "journal+console";
        };
      };
    };
}
