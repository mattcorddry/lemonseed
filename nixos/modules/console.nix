# Local display: status splash on tty4; root debug shell on tty2.

{ pkgs, lib, ... }:

let
  consoleStatus = pkgs.writeShellApplication {
    name = "lemonseed-appliance-console";
    runtimeInputs = with pkgs; [
      coreutils
      iproute2
      gnused
      gawk
      kbd
    ];
    text = builtins.readFile ../../scripts/appliance-console-status.sh;
  };
in
{
  services.getty.autologinUser = lib.mkForce null;

  # Upstream systemd auto-generates serial-getty@ttyS0 because console=ttyS0
  # is in boot.kernelParams (the template has WantedBy=getty.target). On
  # hardware without a ttyS0 that getty crash-loops, so we override it with an
  # ExecStartPre guard that exits cleanly when the device is absent. QEMU's
  # q35 machine always exposes a serial port, so interactive serial debugging
  # (make run-qemu) still works there.
  systemd.services."serial-getty@ttyS0" = {
    enable = true;
    serviceConfig = {
      # Leading "" resets any inherited ExecStartPre (NixOS idiom).
      ExecStartPre = [
        ""
        "/bin/sh -c 'test -c /dev/ttyS0 || exit 101'"
      ];
    };
  };

  systemd.services."getty@tty1".enable = false;
  systemd.services."getty@tty4".enable = false;
  systemd.services."autovt@tty4".enable = lib.mkForce false;

  systemd.services.lemonseed-appliance-console = {
    description = "Lemonseed appliance console status (tty4)";
    wantedBy = [ "multi-user.target" ];
    after = [
      "network.target"
      "avahi-daemon.service"
    ];
    serviceConfig = {
      Type = "simple";
      ExecStartPre = "${pkgs.kbd}/bin/chvt 4";
      ExecStart = "${consoleStatus}/bin/lemonseed-appliance-console /dev/tty4";
      Restart = "always";
      RestartSec = "2s";
      StandardInput = "tty";
      StandardOutput = "tty";
      TTYPath = "/dev/tty4";
      TTYReset = "yes";
      TTYVHangup = "yes";
    };
  };

  # Bring-up / field debug: empty-password root on tty2 only.
  systemd.services."getty@tty2" = {
    enable = true;
    wantedBy = [ "multi-user.target" ];
    serviceConfig = {
      ExecStart = [
        ""
        "${pkgs.util-linux}/bin/agetty --autologin root --noclear --keep-baud tty2 115200,38400,9600 $TERM"
      ];
    };
  };

  users.users.root.hashedPassword = lib.mkForce "";
}
