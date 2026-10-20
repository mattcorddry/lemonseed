# The baked release trust set (M8 P1): release-keys.json plus the key files it
# lists, copied explicitly. The JSON is the authority and is never expanded
# with a glob — the same rule the runtime verifier applies (SIGNING.md §2/§5).
#
# The resulting store path is pointed at by the updater and installer wrappers
# (LEMONSEED_RELEASE_KEYS_DIR), so the trust set ships inside the SquashFS /
# installer ISO closure: baked into the image, nothing fetched at runtime.
#
# Parameterised so the QEMU smoke profile can bake its own test trust set
# (keys/qemu, tracked *including* its private key, QEMU-only) while the
# appliance and installer bake the ceremony key set from keys/release-keys.json.

{
  pkgs,
  lib,
  keysDir,
  trustFile ? "release-keys.json",
}:

let
  trust = builtins.fromJSON (builtins.readFile (keysDir + "/${trustFile}"));
  # Key entries resolve relative to the trust manifest's own directory — the
  # same rule the runtime verifier applies (it reads the baked release-keys.json
  # and opens listed files beside it). keysHome is '.' for the production set
  # (keys/release-keys.json) and 'qemu' for the QEMU smoke set.
  # Key entries resolve relative to the trust manifest's own directory — the
  # same rule the runtime verifier applies (it reads the baked release-keys.json
  # and opens listed files beside it). The production set lives at keysDir root;
  # the QEMU smoke set sits under keys/qemu/.
  keysHome =
    if dirOf trustFile == "." then keysDir else keysDir + "/${dirOf trustFile}";
  copyKeys = lib.concatMapStringsSep "\n" (
    k: ''cp ${(keysHome + "/${k.file}")} "$out"/${k.file}''
  ) trust.keys;
in
pkgs.runCommand "lemonseed-release-keys" { }
  ''
    mkdir -p "$out"
    cp ${keysDir}/${trustFile} "$out"/${builtins.baseNameOf trustFile}
    ${copyKeys}
  ''
