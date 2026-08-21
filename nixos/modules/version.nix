{ lib, ... }:

{
  options.appliance.version = lib.mkOption {
    type = lib.types.str;
    # Makefile/`scripts/package.sh` set APPLIANCE_VERSION for impure builds.
    default =
      let
        fromEnv = builtins.getEnv "APPLIANCE_VERSION";
      in
      if fromEnv != "" then fromEnv else "0.0.0-dev";
    description = "Appliance version string (YYYYMMDD.NN), embedded in the image and artifacts.";
  };
}
