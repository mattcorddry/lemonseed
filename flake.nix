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
      lemonadePackage = nix-amd-ai.packages.${system}.lemonade.override {
        withWebApp = false;
        withDesktopApp = false;
      };

      mkAppliance =
        extraModules:
        nixpkgs.lib.nixosSystem {
          inherit system;
          specialArgs = { inherit lemonadePackage; };
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
        qemuOwuiImage = pkgs.dockerTools.pullImage {
          imageName = "ghcr.io/open-webui/open-webui";
          imageDigest = "sha256:72c0ba641ba75e7aa52655cb242570906ececd09b1140fb736483038a22b3228";
          finalImageTag = "v0.11.0";
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
