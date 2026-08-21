# Appliance metadata paths:
#   /run/appliance/version     — image version from the running SquashFS/NixOS closure
#                                (not under @etc; always matches the booted image)
#   /etc/appliance-*           — mutable metadata on the user-data @etc subvolume
#   /etc/appliance-first-boot  — ISO-8601 UTC timestamp written once after time sync

{
  config,
  pkgs,
  ...
}:

let
  versionFile = pkgs.writeText "appliance-version" "${config.appliance.version}\n";

  firstBootScript = pkgs.writeShellScript "appliance-first-boot-marker" ''
    set -euo pipefail
    marker=/etc/appliance-first-boot
    if [[ -e "$marker" ]]; then
      echo "appliance-first-boot: already present"
      exit 0
    fi

    # Prefer a real NTP sync, but do not block forever (e.g. offline bring-up).
    synced=0
    n=0
    while [[ "$n" -lt 60 ]]; do
      if timedatectl show -p NTPSynchronized --value 2>/dev/null | grep -qx yes; then
        synced=1
        break
      fi
      sleep 1
      n=$((n + 1))
    done
    if [[ "$synced" -eq 1 ]]; then
      echo "appliance-first-boot: NTP synchronized"
    else
      echo "appliance-first-boot: proceeding without NTP sync (best-effort clock)"
    fi

    ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf '%s\n' "$ts" > "$marker.tmp"
    mv "$marker.tmp" "$marker"
    echo "appliance-first-boot: recorded $ts"
  '';
in
{
  services.timesyncd.enable = true;

  # Image version lives outside @etc so it is not tied to mutable user-data.
  systemd.tmpfiles.rules = [
    "d /run/appliance 0755 root root -"
    "L+ /run/appliance/version - - - - ${versionFile}"
  ];

  systemd.services.appliance-first-boot-marker = {
    description = "Record /etc/appliance-first-boot after clock sync";
    wantedBy = [ "multi-user.target" ];
    after = [
      "local-fs.target"
      "systemd-tmpfiles-setup.service"
      "systemd-timesyncd.service"
    ];
    wants = [ "systemd-timesyncd.service" ];
    path = [
      pkgs.coreutils
      pkgs.util-linux
      pkgs.gnugrep
      pkgs.systemd
    ];
    unitConfig = {
      ConditionPathExists = "!/etc/appliance-first-boot";
    };
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
      ExecStart = firstBootScript;
      StandardOutput = "journal+console";
      StandardError = "journal+console";
    };
  };
}