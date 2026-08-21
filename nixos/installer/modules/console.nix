# Local display: status splash on tty4; root debug shell on tty2; serial for lab.
#
# Avoid tty1 — NixOS/logind still treat it as the primary getty VT, which raced
# our redraw loop (flicker / weird geometry). tty4 is outside the usual login set.

{ pkgs, lib, ... }:

let
  consoleStatus = pkgs.writeShellApplication {
    name = "lemonseed-installer-console";
    runtimeInputs = with pkgs; [
      coreutils
      iproute2
      gnused
      gawk
      kbd # chvt
    ];
    text = builtins.readFile ../../../scripts/installer-console-status.sh;
  };
in
{
  # No global autologin — that was logging into whatever getty won on tty1.
  services.getty.autologinUser = lib.mkForce null;

  systemd.services."getty@tty1".enable = false;
  systemd.services."getty@tty4".enable = false;
  # logind still spawns autovt@tty4 when we chvt 4 for the splash screen.
  systemd.services."autovt@tty4".enable = lib.mkForce false;

  systemd.services.lemonseed-installer-console = {
    description = "Lemonseed installer console status (tty4)";
    wantedBy = [ "multi-user.target" ];
    after = [
      "multi-user-getty.target"
      "network.target"
      "avahi-daemon.service"
    ];
    serviceConfig = {
      Type = "simple";
      # Switch the foreground VT, then own tty4 exclusively.
      ExecStartPre = "${pkgs.kbd}/bin/chvt 4";
      ExecStart = "${consoleStatus}/bin/lemonseed-installer-console /dev/tty4";
      Restart = "always";
      RestartSec = "2s";
      StandardInput = "tty";
      StandardOutput = "tty";
      TTYPath = "/dev/tty4";
      TTYReset = "yes";
      TTYVHangup = "yes";
      # Do not disallocate — that fought other VT users and broke geometry.
    };
  };

  # tty2: root debug shell (empty root password on the live installer).
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

  # Serial remains available for lab debugging (Proxmox serial console, etc.).
  systemd.services."serial-getty@ttyS0" = {
    enable = true;
    wantedBy = [ "multi-user.target" ];
    serviceConfig = {
      ExecStart = [
        ""
        "${pkgs.util-linux}/bin/agetty --autologin root --keep-baud 115200,38400,9600 ttyS0 vt220"
      ];
    };
  };
}
