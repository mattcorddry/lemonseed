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

    # Pin the system closure explicitly. mksquashfs output is opaque to nix's
    # reference scanner, so the production image advertises 113 references and
    # *none* of them the toplevel (the qemu image keeps one, by luck). A build
    # root on these artifacts therefore does not pin the system closure, so
    # `scripts/nix-gc.sh` — run at the end of every build by package.sh —
    # collects the toplevel along with the CUDA llama.cpp stack, forcing a
    # from-source GPU recompile on the next build. A plain text store path *is*
    # detected, so this line keeps the closure reachable for as long as this
    # version's build root exists.
    # Not an artifact: scripts/package.sh installs only the three files above.
    printf '%s\n' ${lib.escapeShellArg "${toplevel}"} > "$out/closure-root"
  '';
}
