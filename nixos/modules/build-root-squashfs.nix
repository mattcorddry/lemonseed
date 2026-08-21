{
  lib,
  stdenv,
  squashfsTools,
  closureInfo,
  toplevel,
  version,
  comp ? "zstd -Xcompression-level 10",
}:

# Hierarchical SquashFS root: full /nix/store closure + /init, suitable for
# mounting as `/` (no store-at-squashfs-root live-CD layout).
stdenv.mkDerivation {
  name = "root-${version}.squashfs";

  nativeBuildInputs = [ squashfsTools ];

  buildCommand = ''
    closure=${closureInfo { rootPaths = [ toplevel ]; }}

    mkdir -p root/{dev,proc,sys,run,tmp,var,etc,boot,home,root,mnt,nix/store,usr/bin,bin,sbin}
    chmod 1777 root/tmp

    while IFS= read -r path; do
      cp -a "$path" "root$path"
    done < "$closure/store-paths"

    ln -s "${toplevel}/init" root/init
    mkdir -p root/nix/var/nix/gcroots
    ln -s "${toplevel}" root/nix/var/nix/gcroots/current-system

    cp "$closure/registration" root/nix-path-registration

    # shellcheck disable=SC2086
    SOURCE_DATE_EPOCH=0 mksquashfs root "$out" \
      -noappend \
      -all-root \
      -b 1M \
      -comp ${comp} \
      -processors "$NIX_BUILD_CORES" \
      -root-mode 0755
  '';
}
