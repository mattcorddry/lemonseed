# Load the in-tree MCP gateway image into Docker once at boot (M9 P3).
#
# The image is built from this tree (pkgs/mcp-gateway), so no registry serves
# it — the data plane's _pull_image would sit in WAITING forever on a fresh
# install. Loading the tar by tag satisfies the pin's `_image_present(tag)`
# short-circuit, exactly like the pullImage tarballs the QEMU profile bakes
# in; the pin's digest documents the image config ID rather than a registry
# manifest, which `docker load` tars never carry.
#
# Imported into both the appliance and the qemu profile: production needs it
# for the same reason (there is no registry anywhere), and under TCG a pull
# attempt would burn minutes before falling back.

{
  lib,
  pkgs,
  ...
}:

let
  mcpGatewayImage = pkgs.callPackage ../pkgs/mcp-gateway { };
in

{
  systemd.services.lemonseed-mcp-image-load = {
    description = "Load the Lemonseed MCP gateway image into Docker";
    wantedBy = [ "multi-user.target" ];
    before = [ "appliance-control-plane.service" ];
    after = [ "docker.service" ];
    requires = [ "docker.service" ];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
      ExecStart = "${pkgs.docker}/bin/docker load -i ${mcpGatewayImage}";
      StandardOutput = "journal+console";
      StandardError = "journal+console";
    };
  };

  # The squashfs carries the whole system closure, so referencing the image
  # here is what ships it; make that explicit for anyone auditing size.
  environment.etc."lemonseed/mcp-gateway-image".text =
    "${mcpGatewayImage}\n";
}
