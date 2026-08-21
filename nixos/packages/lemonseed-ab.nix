# A/B boot helper packaged for the read-only SquashFS appliance.
# Do not ship #!/usr/bin/env — the image has no /usr/bin/env (immutable root).

{ pkgs, lib }:

pkgs.writeShellApplication {
  name = "lemonseed-ab";
  runtimeInputs = with pkgs; [
    coreutils
    util-linux
    gnugrep
    gnused
    grub2_efi
  ];
  text = lib.removePrefix "#!/usr/bin/env bash\n" (
    builtins.readFile ../../scripts/lemonseed-ab.sh
  );
}
