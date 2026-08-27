# Public HTTP gateway: nginx :80 → Lemonseed shell + Open WebUI + CP APIs.

{ pkgs, ... }:

let
  brandAssets = ../../control-plane/static/brand;

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

  # Default blue slot until CP flips after health-check.
  defaultOwuiUpstream = pkgs.writeText "owui-upstream.conf" ''
    upstream lemonseed_owui {
      server 127.0.0.1:18080;
    }
  '';

  seedOwuiUpstream = pkgs.writeShellApplication {
    name = "lemonseed-owui-upstream-seed";
    runtimeInputs = [ pkgs.coreutils ];
    text = ''
      set -euo pipefail
      install -d -m 0755 /run/lemonseed
      if [[ ! -s /run/lemonseed/owui-upstream.conf ]]; then
        install -m 0644 ${defaultOwuiUpstream} /run/lemonseed/owui-upstream.conf
      fi
    '';
  };

  cpProxy = ''
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection $connection_upgrade;
    proxy_read_timeout 3600s;
  '';

  # OWUI serves root-absolute /_app, /api/v1, etc. Catch-all goes to OWUI;
  # Lemonseed keeps exact / (document navigations) + reserved /api/* + /settings + /setup.
  # Iframe navigations to / must hit OWUI at path / — otherwise SvelteKit sees /chat/ and 404s.
  owuiProxy = ''
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection $connection_upgrade;
    proxy_read_timeout 3600s;
    proxy_send_timeout 3600s;
    proxy_buffering off;
    proxy_hide_header X-Frame-Options;
    proxy_hide_header Content-Security-Policy;
  '';

  # Containers reach host-local lemond via host.docker.internal:80 (not :13305).
  # Allow Docker private bridge ranges only — not typical LAN (192.168/16).
  lemondProxy = ''
    allow 127.0.0.0/8;
    allow 172.16.0.0/12;
    deny all;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header Authorization $http_authorization;
    proxy_pass_header Authorization;
    proxy_read_timeout 3600s;
    proxy_buffering off;
  '';

  rootProxy = ''
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection $connection_upgrade;
    proxy_read_timeout 3600s;
    proxy_send_timeout 3600s;
    proxy_buffering off;
    proxy_hide_header X-Frame-Options;
    proxy_hide_header Content-Security-Policy;
  '';
in
{
  networking.firewall.allowedTCPPorts = [ 80 ];
  networking.firewall.allowedUDPPorts = [ 5353 ];

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
      Restart = "on-failure";
      RestartSec = "3s";
      StandardOutput = "journal+console";
      StandardError = "journal+console";
    };
  };

  systemd.services.lemonseed-owui-upstream-seed = {
    description = "Seed Open WebUI nginx upstream file";
    wantedBy = [ "multi-user.target" ];
    before = [ "nginx.service" ];
    after = [ "local-fs.target" ];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
      ExecStart = "${seedOwuiUpstream}/bin/lemonseed-owui-upstream-seed";
    };
  };

  services.nginx = {
    enable = true;
    recommendedProxySettings = true;
    recommendedGzipSettings = true;
    # Errors already default to stderr → journald; keep explicit.
    logError = "stderr";
    appendHttpConfig = ''
      include /run/lemonseed/owui-upstream.conf;
      # Access logs via syslog socket → journald (no /var/log/nginx files).
      access_log syslog:server=unix:/dev/log,nohostname,tag=nginx;

      # Top-level / → Lemonseed shell; iframe (or ?embed=1) → Open WebUI at path /.
      # OWUI is a root SPA — serving it under /chat/ makes the client router 404.
      map "$http_sec_fetch_dest:$arg_embed" $lemonseed_root_upstream {
        default      http://127.0.0.1:8080;
        "iframe:"    http://lemonseed_owui;
        "iframe:1"   http://lemonseed_owui;
        ":1"         http://lemonseed_owui;
        "empty:1"    http://lemonseed_owui;
        "document:1" http://lemonseed_owui;
      }
    '';
    virtualHosts."lemonseed" = {
      default = true;
      listen = [
        {
          addr = "0.0.0.0";
          port = 80;
        }
      ];

      # Exact root: shell for browsers, OWUI for the app iframe.
      locations."= /" = {
        extraConfig = ''
          proxy_pass $lemonseed_root_upstream;
          ${rootProxy}
        '';
      };
      locations."= /index.html" = {
        proxyPass = "http://127.0.0.1:8080";
        extraConfig = cpProxy;
      };
      locations."= /setup" = {
        proxyPass = "http://127.0.0.1:8080";
        extraConfig = cpProxy;
      };
      locations."= /healthz" = {
        proxyPass = "http://127.0.0.1:8080";
        extraConfig = cpProxy;
      };
      locations."= /health" = {
        proxyPass = "http://127.0.0.1:8080";
        extraConfig = cpProxy;
      };
      locations."/settings/" = {
        proxyPass = "http://127.0.0.1:8080";
        extraConfig = cpProxy;
      };
      locations."= /settings" = {
        return = "302 /settings/";
      };

      # Reserved Lemonseed JSON APIs (must win over OWUI /api/*).
      locations."= /api/boot" = {
        proxyPass = "http://127.0.0.1:8080";
        extraConfig = cpProxy;
      };
      locations."= /api/status" = {
        proxyPass = "http://127.0.0.1:8080";
        extraConfig = cpProxy;
      };
      locations."= /api/hardware" = {
        proxyPass = "http://127.0.0.1:8080";
        extraConfig = cpProxy;
      };
      locations."= /api/plan" = {
        proxyPass = "http://127.0.0.1:8080";
        extraConfig = cpProxy;
      };
      locations."= /api/data" = {
        proxyPass = "http://127.0.0.1:8080";
        extraConfig = cpProxy;
      };
      locations."= /api/modules" = {
        proxyPass = "http://127.0.0.1:8080";
        extraConfig = cpProxy;
      };
      locations."= /api/chat" = {
        proxyPass = "http://127.0.0.1:8080";
        extraConfig = cpProxy;
      };

      # Brand icons (shell / setup / favicon) — must not fall through to OWUI.
      locations."/_lemonseed/brand/" = {
        alias = "${brandAssets}/";
        extraConfig = ''
          expires 7d;
          add_header Cache-Control "public";
        '';
      };
      locations."= /favicon.ico" = {
        alias = "${brandAssets}/favicon.ico";
        extraConfig = ''
          expires 7d;
          add_header Cache-Control "public";
        '';
      };

      # Internal lemond proxy for modules on the lemon network (not LAN-facing).
      locations."/_lemonseed/lemond/" = {
        proxyPass = "http://127.0.0.1:13305/";
        extraConfig = lemondProxy;
      };

      # Legacy iframe entry → embed at / (OWUI root SPA path).
      locations."= /chat" = {
        return = "302 /?embed=1";
      };
      locations."= /chat/" = {
        return = "302 /?embed=1";
      };

      # Socket.IO / websockets (explicit; Upgrade headers already in owuiProxy).
      locations."/ws/" = {
        proxyPass = "http://lemonseed_owui";
        extraConfig = owuiProxy;
      };

      # Everything else (SPA assets, OWUI /api/v1, /c/…, …) → Open WebUI.
      locations."/" = {
        proxyPass = "http://lemonseed_owui";
        extraConfig = owuiProxy;
      };
    };
  };

  systemd.services.nginx = {
    after = [
      "network-online.target"
      "appliance-control-plane.service"
      "lemonseed-owui-upstream-seed.service"
    ];
    wants = [
      "network-online.target"
      "appliance-control-plane.service"
      "lemonseed-owui-upstream-seed.service"
    ];
  };

  # No on-disk nginx access/error logs — nothing for logrotate to manage.
  services.logrotate.settings.nginx.enable = false;

  systemd.services.avahi-daemon = {
    after = [ "network-online.target" ];
    wants = [ "network-online.target" ];
  };

  systemd.tmpfiles.rules = [
    "d /run/lemonseed 0755 root root -"
  ];
}
