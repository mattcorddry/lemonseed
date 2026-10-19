# In-place A/B image updater for lab and production iteration.
#
# lemonseed-update writes the inactive SquashFS + ESP kernels, arms GRUB
# boot_once, and leaves user-data untouched. Lock-in (ab-boot.nix) promotes the
# new slot only after a healthy boot. The updater itself is packaged once in
# packages/lemonseed-update.nix — the control plane spawns the same binary for
# the SYSTEM panel's update job and failed-update detector.

{
  pkgs,
  lib,
  ...
}:

let
  updater = import ../packages/lemonseed-update.nix { inherit pkgs lib; };
in
{
  environment.systemPackages = [ updater ];

  systemd.tmpfiles.rules = [
    "d /var/lib/lemonseed 0755 root root -"
    "d /var/lib/lemonseed/download 0755 root root -"
  ];

  # Weekly update check, fuzzed so appliances do not synchronously hammer the
  # release endpoint. It only *records* the finding (latest-update.json, read
  # by the control plane's snapshot); nothing is ever downloaded or installed
  # unattended — the SYSTEM panel's buttons are the only writers.
  systemd.services.lemonseed-update-check = {
    description = "Lemonseed update check (records latest available version)";
    after = [ "network-online.target" ];
    wants = [ "network-online.target" ];
    serviceConfig = {
      Type = "oneshot";
      ExecStart = "${updater}/bin/lemonseed-update check --record";
    };
  };

  # Boot-time check: the panel's "latest" is otherwise as stale as the last
  # weekly run — which after an update is *older than the running version* and
  # would offer a downgrade (bare-metal review). Once per boot, network
  # permitting; the weekly timer keeps covering long-lived boxes.
  systemd.services.lemonseed-update-check-boot = {
    description = "Lemonseed update check at boot (records latest available version)";
    after = [ "network-online.target" ];
    wants = [ "network-online.target" ];
    wantedBy = [ "multi-user.target" ];
    serviceConfig = {
      Type = "oneshot";
      ExecStart = "${updater}/bin/lemonseed-update check --record";
    };
  };

  systemd.timers.lemonseed-update-check = {
    wantedBy = [ "timers.target" ];
    timerConfig = {
      OnCalendar = "weekly";
      # The fuzz: up to a day of jitter, so a fleet of boxes spreads out.
      RandomizedDelaySec = "86400";
      Persistent = true;
    };
  };
}
