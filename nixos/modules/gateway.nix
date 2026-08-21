# Public HTTP gateway: nginx :80 → control plane :8080, plus mDNS.

{ pkgs, ... }:

let
  identity = pkgs.writeShellApplication {
    name = "lemonseed-apply-identity";
    runtimeInputs = with pkgs; [
      coreutils
      jq
    ];
    text = ''
      set -euo pipefail

      setup=/etc/appliance-setup.json
      hostname_file=/etc/hostname
      valid='^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$'

      current=""
      if [[ -r "$hostname_file" ]]; then
        current="$(head -n 1 "$hostname_file" | tr -d '[:space:]')"
      fi

      want=""
      if [[ -r "$setup" ]]; then
        want="$(jq -r '.mdns_name // empty' "$setup")"
      fi
      if [[ ! "$want" =~ $valid ]]; then
        want="$current"
      fi
      if [[ ! "$want" =~ $valid ]]; then
        want=lemonseed
      fi

      if [[ "$want" != "$current" ]]; then
        printf '%s\n' "$want" > "$hostname_file.tmp"
        mv "$hostname_file.tmp" "$hostname_file"
      fi

      # Set the live hostname directly: hostnamed/dbus may not be up this early,
      # and both Avahi and dhcpcd read the kernel value.
      printf '%s' "$want" > /proc/sys/kernel/hostname
      echo "lemonseed-identity: hostname $want"
    '';
  };

  avahiAlias = pkgs.writeShellApplication {
    name = "lemonseed-avahi-alias";
    runtimeInputs = [
      (pkgs.python3.withPackages (ps: [ ps.dbus-python ]))
    ];
    text = ''
      exec python3 ${../../scripts/lemonseed-avahi-alias.py}
    '';
  };
in
{
  networking.firewall.allowedTCPPorts = [ 80 ];
  networking.firewall.allowedUDPPorts = [ 5353 ];

  # With networking.hostName empty, avahi-daemon.conf omits host-name= and the
  # daemon tracks the live system hostname that lemonseed-identity applied.
  # Primary advertisement is always <slug>.local. During install/reinstall
  # handoff only, lemonseed-avahi-alias may briefly publish lemonseed.local as a
  # CNAME so the installer can discover a renamed appliance; it releases that
  # name as soon as handoff completes (see /run/lemonseed/mdns-handoff-done).
  services.avahi = {
    enable = true;
    nssmdns4 = true;
    openFirewall = true;
    publish = {
      enable = true;
      addresses = true;
      workstation = true;
      domain = true;
      userServices = true;
    };
    extraServiceFiles = {
      portal = ''
        <?xml version="1.0" standalone='no'?>
        <!DOCTYPE service-group SYSTEM "avahi-service.dtd">
        <service-group>
          <name replace-wildcards="yes">Lemonseed on %h</name>
          <service>
            <type>_http._tcp</type>
            <port>80</port>
            <txt-record>path=/</txt-record>
          </service>
        </service-group>
      '';
    };
  };

  systemd.services.lemonseed-identity = {
    description = "Apply the configured Lemonseed hostname";
    wantedBy = [ "multi-user.target" ];
    after = [ "local-fs.target" ];
    wants = [ "network-pre.target" ];
    before = [
      "network-pre.target"
      "dhcpcd.service"
      "avahi-daemon.service"
    ];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
      ExecStart = "${identity}/bin/lemonseed-apply-identity";
      StandardOutput = "journal+console";
      StandardError = "journal+console";
    };
  };

  # Long-lived: Avahi retracts D-Bus EntryGroup records when the client exits.
  systemd.services.lemonseed-avahi-alias = {
    description = "Temporary lemonseed.local CNAME for installer handoff";
    wantedBy = [ "multi-user.target" ];
    after = [
      "avahi-daemon.service"
      "lemonseed-identity.service"
    ];
    requires = [ "avahi-daemon.service" ];
    serviceConfig = {
      Type = "simple";
      ExecStart = "${avahiAlias}/bin/lemonseed-avahi-alias";
      # Exit 0 when handoff is done or not needed — do not keep claiming lemonseed.local.
      Restart = "on-failure";
      RestartSec = "3s";
      StandardOutput = "journal+console";
      StandardError = "journal+console";
    };
  };

  # Phase 1 gateway: reverse-proxy everything to the control plane.
  # After first-boot setup completes we will swap this for auth + module
  # routing (Open WebUI, etc.) while keeping :80 as the only public entry.
  services.nginx = {
    enable = true;
    recommendedProxySettings = true;
    recommendedGzipSettings = true;
    virtualHosts."lemonseed" = {
      default = true;
      listen = [
        {
          addr = "0.0.0.0";
          port = 80;
        }
      ];
      locations."/" = {
        proxyPass = "http://127.0.0.1:8080";
        extraConfig = ''
          proxy_http_version 1.1;
          proxy_set_header Upgrade $http_upgrade;
          proxy_set_header Connection $connection_upgrade;
          proxy_read_timeout 3600s;
        '';
      };
    };
  };

  # Bring the portal up as soon as the network stack is usable.
  systemd.services.nginx = {
    after = [
      "network-online.target"
      "appliance-control-plane.service"
    ];
    wants = [
      "network-online.target"
      "appliance-control-plane.service"
    ];
  };

  systemd.services.avahi-daemon = {
    after = [ "network-online.target" ];
    wants = [ "network-online.target" ];
  };
}
