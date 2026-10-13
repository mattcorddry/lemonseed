{
  description = "NixOS AI appliance image build pipeline";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";
    # Headless, Nix-native Lemonade build. We consume only the package; its
    # AMD/NPU NixOS module is intentionally not imported into the appliance.
    nix-amd-ai.url = "github:noamsto/nix-amd-ai";
  };

  outputs =
    {
      self,
      nixpkgs,
      nix-amd-ai,
    }:
    let
      system = "x86_64-linux";
      pkgs = nixpkgs.legacyPackages.${system};
      lemonadePackage = nix-amd-ai.packages.${system}.lemonade.override {
        withWebApp = false;
        withDesktopApp = false;
      };

      # Unofficial Python SDK for the lemond HTTP API (not in nixpkgs). The
      # control plane uses it to talk to lemond over HTTP instead of wrapping
      # the `lemonade` CLI — see DESIGN.md "lemond API over CLI".
      lemonadePythonSdk = pkgs.callPackage ./pkgs/lemonade-python-sdk.nix {
        buildPythonPackage = pkgs.python3Packages.buildPythonPackage;
        requests = pkgs.python3Packages.requests;
        setuptools = pkgs.python3Packages.setuptools;
      };

      mkAppliance =
        extraModules:
        nixpkgs.lib.nixosSystem {
          inherit system;
          # nix-amd-ai is passed so lemond.nix can consume its llama.cpp
          # builds (must track the same llama.cpp lemonadePackage is built
          # against — see DESIGN.md "llama.cpp provenance").
          specialArgs = { inherit lemonadePackage nix-amd-ai lemonadePythonSdk; };
          modules = [ ./nixos/configuration.nix ] ++ extraModules;
        };
    in
    {
      nixosConfigurations = {
        appliance = mkAppliance [ ];
        appliance-qemu = mkAppliance [ ./nixos/modules/qemu-test.nix ];
        installer = nixpkgs.lib.nixosSystem {
          inherit system;
          modules = [ ./nixos/installer/configuration.nix ];
        };
      };

      packages.${system} = let
        pkgs = nixpkgs.legacyPackages.${system};
        imagePins = builtins.fromJSON (builtins.readFile ./control-plane/container-images.json);
        qemuOwuiImage = pkgs.dockerTools.pullImage {
          imageName = imagePins.open_webui.name;
          imageDigest = imagePins.open_webui.digest;
          finalImageTag = imagePins.open_webui.tag;
          hash = "sha256-dDz7zdzDbuLB81rQm0yeFQFsn1uhLuinoRMTM5UZExI=";
        };
      in
      {
        default = self.nixosConfigurations.appliance.config.system.build.applianceArtifacts;
        artifacts = self.nixosConfigurations.appliance.config.system.build.applianceArtifacts;
        artifacts-qemu =
          self.nixosConfigurations.appliance-qemu.config.system.build.applianceArtifacts;
        root-squashfs =
          self.nixosConfigurations.appliance.config.system.build.applianceRootSquashfs;
        vmlinuz = self.nixosConfigurations.appliance.config.system.build.applianceKernel;
        initrd = self.nixosConfigurations.appliance.config.system.build.applianceInitrd;
        # Host helper for `make run-qemu` when qemu is not installed system-wide.
        qemu = nixpkgs.legacyPackages.${system}.qemu_kvm;
        # Pinned OWUI image tar for `make test-qemu` virtio-fs export (not in squashfs).
        qemu-owui-image = qemuOwuiImage;
        # EFI/USB-bootable installer ISO (M4).
        installer-iso = self.nixosConfigurations.installer.config.system.build.isoImage;
      };
    };
}
