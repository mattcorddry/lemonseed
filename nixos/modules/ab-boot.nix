# A/B boot lock-in: after a successful boot following boot_once, set boot_default.
# Success means the platform services for this slot are healthy (not model load).

{
  pkgs,
  lib,
  ...
}:

let
  abHelper =
    pkgs.runCommand "lemonseed-ab"
      {
        nativeBuildInputs = [ pkgs.makeWrapper ];
      }
      ''
        mkdir -p "$out/bin"
        cp ${../../scripts/lemonseed-ab.sh} "$out/bin/lemonseed-ab"
        chmod 0755 "$out/bin/lemonseed-ab"
        wrapProgram "$out/bin/lemonseed-ab" \
          --prefix PATH : ${
            lib.makeBinPath [
              pkgs.coreutils
              pkgs.util-linux
              pkgs.gnugrep
              pkgs.gnused
              pkgs.grub2_efi
            ]
          }
      '';

  # Soft readiness probes used before promoting boot_default. Failure leaves
  # boot_attempted set so the next cold boot falls back to the previous slot.
  lockIn = pkgs.writeShellScript "lemonseed-ab-lock-in" ''
    set -euo pipefail

    if ! out="$(${abHelper}/bin/lemonseed-ab status 2>/dev/null)"; then
      echo "lemonseed-ab-lock-in: no ESP/grubenv yet; skipping"
      exit 0
    fi
    attempted="$(printf '%s\n' "$out" | sed -n 's/^boot_attempted=//p' | head -1)"
    if [[ -z "$attempted" ]]; then
      echo "lemonseed-ab-lock-in: no boot_attempted; skipping"
      exit 0
    fi

    current="$(${abHelper}/bin/lemonseed-ab current-slot 2>/dev/null || true)"
    if [[ -n "$current" && "$current" != "$attempted" ]]; then
      echo "lemonseed-ab-lock-in: running slot ($current) != attempted ($attempted); refusing lock-in" >&2
      exit 1
    fi

    require_active() {
      local unit="$1"
      local state
      state="$(systemctl is-active "$unit" 2>/dev/null || true)"
      if [[ "$state" != "active" ]]; then
        echo "lemonseed-ab-lock-in: $unit is '$state' (want active)" >&2
        return 1
      fi
    }

    require_active network-online.target
    require_active nginx.service
    require_active appliance-control-plane.service
    require_active avahi-daemon.service

    if ! docker info >/dev/null 2>&1; then
      echo "lemonseed-ab-lock-in: docker daemon not ready" >&2
      exit 1
    fi

    if ! curl -fsS --max-time 3 http://127.0.0.1:8080/healthz >/dev/null; then
      echo "lemonseed-ab-lock-in: control plane /healthz failed" >&2
      exit 1
    fi

    ${abHelper}/bin/lemonseed-ab lock-in "$attempted"
  '';
in
{
  environment.systemPackages = [
    abHelper
    pkgs.grub2_efi
  ];

  systemd.services.lemonseed-ab-lock-in = {
    description = "Lock in A/B boot slot after successful startup";
    wantedBy = [ "multi-user.target" ];
    after = [
      "local-fs.target"
      "network-online.target"
      "nginx.service"
      "avahi-daemon.service"
      "docker.service"
      "appliance-control-plane.service"
    ];
    wants = [
      "network-online.target"
      "nginx.service"
      "avahi-daemon.service"
      "docker.service"
      "appliance-control-plane.service"
    ];
    path = [
      pkgs.coreutils
      pkgs.gnused
      pkgs.systemd
      pkgs.curl
      pkgs.docker
      abHelper
    ];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
      # Retry briefly while platform services finish starting after boot_once.
      ExecStart = lockIn;
      Restart = "on-failure";
      RestartSec = "5s";
    };
    unitConfig = {
      StartLimitBurst = 12;
      StartLimitIntervalSec = "180";
    };
  };
}
