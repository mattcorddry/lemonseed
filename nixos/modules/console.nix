# Local display: status splash on tty4; root login prompt on tty2.

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

  # M8 P7 (D-E4): the console explains itself, because a keyboard is only
  # attached when something is already wrong. agetty prints this before the
  # login prompt on every TTY and serial line.
  issueText = ''
    Lemonseed appliance console

    This console is for recovery and maintenance. Sign in as `root` with the
    Hardware password — visible in the web portal (http://<appliance-name>.local)
    under Settings → Advanced. Regular account passwords do not work here;
    accounts are managed and used through the web portal.
  '';

  # Upstream systemd auto-generates serial-getty@ttyS0 because console=ttyS0
  # is in boot.kernelParams (the template has WantedBy=getty.target). On
  # hardware without a serial port that getty crash-loops — console=ttyS0
  # makes the kernel create /dev/ttyS0 even with no UART behind it, so a
  # device-node test is not enough (round-4 bare metal: agetty failed
  # "not a tty" on a node nothing backed). The guard therefore requires the
  # port's /proc/tty/driver/serial line to name a real UART. QEMU's q35
  # machine always exposes a 16550A, so interactive serial debugging
  # (make run-qemu) still works there.
  #
  # The test rides ExecCondition rather than ExecStartPre: conditions cannot
  # read /proc, and a failing ExecStartPre marks the unit failed, while an
  # ExecCondition exit of 1-254 skips the remaining commands WITHOUT failing
  # the unit — exactly the absent-port case.
  serialGettyGuard =
    port: index:
    pkgs.writeShellApplication {
      name = "lemonseed-serial-getty-guard-${port}";
      runtimeInputs = with pkgs; [
        coreutils
        gnugrep
      ];
      text = ''
        test -c /dev/${port} || exit 101
        grep "^${index}: uart:" /proc/tty/driver/serial 2>/dev/null \
          | grep -qv 'uart:unknown' || exit 101
      '';
    };
in
{
  services.getty.autologinUser = lib.mkForce null;

  systemd.services."serial-getty@ttyS0" = {
    enable = true;
    serviceConfig = {
      # Leading "" resets any inherited ExecCondition (NixOS idiom).
      ExecCondition = [
        ""
        "${serialGettyGuard "ttyS0" "0"}/bin/lemonseed-serial-getty-guard-ttyS0"
      ];
    };
  };

  # Physical serial ports get login prompts when the firmware marks them
  # consoles (D-E4); the guard skips absent/unbacked ports instead of
  # crash-looping.
  systemd.services."serial-getty@ttyS1" = {
    enable = true;
    serviceConfig = {
      ExecCondition = [
        ""
        "${serialGettyGuard "ttyS1" "1"}/bin/lemonseed-serial-getty-guard-ttyS1"
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

  # M8 P7 (D25/D-E4): no autologin anywhere. tty2 becomes an ordinary login
  # prompt; the root account is reachable only with the Hardware password.
  systemd.services."getty@tty2" = {
    enable = true;
    wantedBy = [ "multi-user.target" ];
  };

  environment.etc."issue".text = issueText;

  # Root ships locked; the control plane materializes the Hardware-password
  # hash into this file at first-boot adoption. hashedPasswordFile is read on
  # every activation (users.mutableUsers is false, so this is the only way a
  # runtime-set hash survives), and a missing file degrades to "!" (locked),
  # never to passwordless root. The CP also applies the same hash to
  # /etc/shadow directly on adoption so boot 1's console works without a
  # second activation.
  users.users.root.hashedPasswordFile =
    lib.mkForce "/var/lib/lemonseed/root-password-hash";
}
