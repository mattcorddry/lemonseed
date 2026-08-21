{
  config,
  lib,
  pkgs,
  ...
}:

let
  version = config.appliance.version;
  toplevel = config.system.build.toplevel;
  kernelFile = config.system.boot.loader.kernelFile;
in
{
  # Bootloader lives on the ESP and is managed by the installer/updater (M3/M4).
  boot.loader.external = {
    enable = true;
    installHook = pkgs.writeShellScript "appliance-no-bootloader-install" ''
      echo "appliance image: bootloader is installed to the ESP separately" >&2
    '';
  };

  system.build.applianceRootSquashfs = pkgs.callPackage ./build-root-squashfs.nix {
    inherit toplevel version;
    usrBinLinks = config.lemonseed.fhsBinLinks;
  };

  system.build.applianceKernel = pkgs.runCommand "vmlinuz-${version}" { } ''
    cp ${config.boot.kernelPackages.kernel}/${kernelFile} "$out"
  '';

  # Included for make run-qemu / practical boot until a no-initrd spike lands.
  system.build.applianceInitrd = pkgs.runCommand "initrd-${version}" { } ''
    cp ${config.system.build.initialRamdisk}/initrd "$out"
  '';

  system.build.applianceArtifacts = pkgs.runCommand "appliance-artifacts-${version}" { } ''
    mkdir -p "$out"
    ln -s ${config.system.build.applianceRootSquashfs} "$out/root.squashfs"
    ln -s ${config.system.build.applianceKernel} "$out/vmlinuz"
    ln -s ${config.system.build.applianceInitrd} "$out/initrd.img"
    printf '%s\n' ${lib.escapeShellArg version} > "$out/version"
  '';
}
