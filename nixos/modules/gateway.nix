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

  # nginx has no built-in auth_user/auth_email/auth_groups variables: the
  # auth_request_set directives in the gate snippet assign them, but the
  # proxy_set_header directives in the vhost locations reference them at
  # config-parse time. `set` is only allowed in server/location/if scopes
  # (not http), so declare them at server scope via extraConfig.
  authVarDecls = ''
    set $auth_user "";
    set $auth_email "";
    set $auth_groups "";
    set $auth_name "";
    set $auth_role "";
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
      # OWUI is down at boot: start with the flag absent so nginx serves the
      # shell (interstitial) for document requests until the control plane marks
      # OWUI ready (M6.2). Never create it here — only CP creates/removes it.
      rm -f /run/lemonseed/owui-ready
    '';
  };

  # Edge-auth identity headers (M6.2): set ONLY from auth_request_set
  # variables populated by the CP session-verify response. An empty variable
  # makes nginx omit the header, and these directives replace any client-
  # supplied value of the same name — so client X-Forwarded-User/-Email/-Groups
  # are always dropped.
  identityHeaders = ''
    proxy_set_header X-Forwarded-User $auth_user;
    proxy_set_header X-Forwarded-Email $auth_email;
    proxy_set_header X-Forwarded-Groups $auth_groups;
    proxy_set_header X-Forwarded-Name $auth_name;
    proxy_set_header X-Forwarded-Role $auth_role;
  '';

  # Reloadable edge-auth gate (seeded empty before nginx; CP writes it after
  # the first admin exists). Included by every protected location.
  authGate = ''
    include /run/lemonseed/auth-gate.conf;
  '';

  # Admin-group gate for /settings/ and other privileged surfaces (M6.2).
  adminGate = ''
    include /run/lemonseed/auth-admin.conf;
  '';

  # M6.3b Phase 4B: Lemonseed-managed Open WebUI paths, refused at the edge.
  # The control plane renders this file from owui-policy.json; it is seeded
  # empty, so a boot where the CP never ran blocks nothing (fail-open for the
  # admin UI, while intent stays enforced by the reconcile loop).
  # `if` + `return` runs in nginx's rewrite phase — before the auth_request in
  # authGate — which is why an anonymous caller gets 403 rather than a login
  # redirect on a guarded path (decision T4-A).
  owuiGuards = ''
    include /run/lemonseed/owui-guards.conf;
  '';

  cpProxy = ''
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    ${identityHeaders}
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
    ${identityHeaders}
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
    ${identityHeaders}
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection $connection_upgrade;
    proxy_read_timeout 3600s;
    proxy_send_timeout 3600s;
    proxy_buffering off;
    proxy_hide_header X-Frame-Options;
    proxy_hide_header Content-Security-Policy;
  '';

  # Empty gate snippets until the control plane flips them after the first
  # admin exists (same reloadable-file pattern as owui-upstream.conf).
  seedAuthGate = pkgs.writeShellApplication {
    name = "lemonseed-auth-gate-seed";
    runtimeInputs = [ pkgs.coreutils ];
    text = ''
      set -euo pipefail
      install -d -m 0755 /run/lemonseed
      for f in auth-gate.conf auth-admin.conf owui-guards.conf; do
        if [[ ! -e /run/lemonseed/$f ]]; then
          : > /run/lemonseed/$f
          chmod 0644 /run/lemonseed/$f
        fi
      done
    '';
  };
in
{
  networking.firewall.allowedTCPPorts = [ 80 ];
  networking.firewall.allowedUDPPorts = [ 5353 ];

  services.avahi = {
    enable = true;
    nssmdns4 = true;
    # mDNS only on physical LAN links — not loopback, Docker bridges, or
    # veth pairs (the appliance runs containers with host networking).
    denyInterfaces = [ "lo" "docker" "br-" "veth" ];
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

      # Unauthenticated requests on gated locations land here (the CP login
      # page handles ?rd= for the post-login redirect). When OWUI is
      # not yet running, 502/503/504 from the dead upstream are rewritten to
      # the CP shell (which shows a "Getting Ready" interstitial and
      # auto-loads OWUI once it is healthy).
      #
      # The CP shell itself is also the "rebooting" interstitial: when the
      # amdgpu-kernel-params service reboots the box, the browser's in-flight
      # requests die mid-flight. The shell's 5s poll keeps retrying, but the
      # browser only re-displays the page if it actually received a response
      # after the box came back. To guarantee the user sees the "Rebooting…"
      # page (rather than ERR_CONNECTION_REFUSED), we also serve the CP shell
      # on 502/503/504 from the CP upstream — the shell's JS then shows the
      # rebooting state (chat.state == "rebooting") and keeps polling until
      # the box is back and the model is ready.
      extraConfig = ''
        ${authVarDecls}
        location @auth_unauthorized {
          return 302 /login?rd=$request_uri;
        }
        location @cp_interstitial {
          # Named location: proxy_pass may not carry a URI part. Forward the
          # original request URI — the CP routes /, /login, /settings/, and
          # /api/* natively, so the interstitial shows the right page.
          proxy_pass http://127.0.0.1:8080;
          proxy_http_version 1.1;
          proxy_set_header Host $host;
          proxy_set_header X-Real-IP $remote_addr;
          proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
          proxy_set_header X-Forwarded-Proto $scheme;
          ${identityHeaders}
        }
        location @owui_loading {
          proxy_pass http://127.0.0.1:8080;
          proxy_http_version 1.1;
          proxy_set_header Host $host;
          proxy_set_header X-Real-IP $remote_addr;
          proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
          proxy_set_header X-Forwarded-Proto $scheme;
          ${identityHeaders}
        }
      '';

      # Exact root: shell for browsers, OWUI for the app iframe. [gated]
      locations."= /" = {
        extraConfig = ''
          ${authGate}
          error_page 502 503 504 = @owui_loading;
          proxy_pass $lemonseed_root_upstream;
          ${rootProxy}
        '';
      };
      locations."= /index.html" = {
        proxyPass = "http://127.0.0.1:8080";
        extraConfig = "${authGate}\n${cpProxy}";
      };

      # Edge auth (CP-native, M6.2): the login page is open (it IS the sign-in
      # form); logout is gated (only a signed-in user can log out).
      locations."= /login" = {
        proxyPass = "http://127.0.0.1:8080";
        extraConfig = "${cpProxy}\nerror_page 502 503 504 = @cp_interstitial;";
      };
      locations."= /logout" = {
        proxyPass = "http://127.0.0.1:8080";
        extraConfig = "${authGate}\n${cpProxy}\nerror_page 502 503 504 = @cp_interstitial;";
      };
      # Internal auth_request target (never LAN-facing). The CP verifies the
      # signed session cookie and returns 200 + X-Forwarded-* headers (read via
      # auth_request_set in the gate snippet) or 401.
      locations."= /_lemonseed/session-verify" = {
        proxyPass = "http://127.0.0.1:8080";
        extraConfig = ''
          internal;
          proxy_set_header Host $host;
          proxy_set_header X-Real-IP $remote_addr;
          proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
          proxy_set_header X-Forwarded-Proto $scheme;
          proxy_http_version 1.1;
          proxy_set_header Connection "";
          proxy_read_timeout 240;
          proxy_send_timeout 240;
          proxy_connect_timeout 240;
        '';
      };

      # First-boot setup + health probes stay open (pre-admin trust window).
      locations."= /setup" = {
        proxyPass = "http://127.0.0.1:8080";
        extraConfig = "${cpProxy}\nerror_page 502 503 504 = @cp_interstitial;";
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
        # Gated + admin group required (M6.2).
        extraConfig = "${authGate}\n${adminGate}\n${cpProxy}\nerror_page 502 503 504 = @cp_interstitial;";
      };
      locations."= /settings" = {
        return = "302 /settings/";
      };

      # Reserved Lemonseed JSON APIs (must win over OWUI /api/*).
      # /api/boot + /api/status stay open (installer handoff, status probes);
      # the rest are gated (M6.2).
      locations."= /api/boot" = {
        proxyPass = "http://127.0.0.1:8080";
        extraConfig = cpProxy;
      };
      locations."= /api/status" = {
        proxyPass = "http://127.0.0.1:8080";
        extraConfig = "${cpProxy}\nerror_page 502 503 504 = @cp_interstitial;";
      };
      locations."= /api/hardware" = {
        proxyPass = "http://127.0.0.1:8080";
        extraConfig = "${authGate}\n${cpProxy}\nerror_page 502 503 504 = @cp_interstitial;";
      };
      locations."= /api/plan" = {
        proxyPass = "http://127.0.0.1:8080";
        extraConfig = "${authGate}\n${cpProxy}\nerror_page 502 503 504 = @cp_interstitial;";
      };
      # Model picker (M6.5): GET reads families + best-fit; POST records the
      # user's choice and is admin-gated in the CP (like /settings/).
      # Namespaced under /_lemonseed/ (not /api/models) so it does not shadow
      # Open WebUI's own /api/models — the OWUI frontend needs that path for
      # its OpenAI-style model list; the exact-match /api/models location used
      # to win and silently returned the CP's picker payload to OWUI.
      locations."= /_lemonseed/api/models" = {
        proxyPass = "http://127.0.0.1:8080";
        extraConfig = "${authGate}\n${adminGate}\n${cpProxy}\nerror_page 502 503 504 = @cp_interstitial;";
      };
      # Open WebUI config ownership view (M6.3b 4A). Exact match so OWUI still
      # owns every other path; admin-gated here *and* in the CP, because the
      # payload merges Open WebUI's (unredacted) config export.
      locations."= /_lemonseed/api/owui/config" = {
        proxyPass = "http://127.0.0.1:8080";
        extraConfig = "${authGate}\n${adminGate}\n${cpProxy}\nerror_page 502 503 504 = @cp_interstitial;";
      };
      # Phase 4B admin actions (M6.3b): re-apply Lemonseed's intent now, and the
      # config reset window. Same double gating as the ownership view. The reset
      # handler returns 202 immediately and reports through /api/modules, so
      # there is no long-held connection to time out here.
      locations."= /_lemonseed/api/owui/reconcile" = {
        proxyPass = "http://127.0.0.1:8080";
        extraConfig = "${authGate}\n${adminGate}\n${cpProxy}\nerror_page 502 503 504 = @cp_interstitial;";
      };
      locations."= /_lemonseed/api/owui/reset-config" = {
        proxyPass = "http://127.0.0.1:8080";
        extraConfig = "${authGate}\n${adminGate}\n${cpProxy}\nerror_page 502 503 504 = @cp_interstitial;";
      };
      # GC e2e test endpoints (admin-gated): disk filler + cached-model list.
      # Used by scripts/qemu-api-smoke-test.sh (QEMU_E2E_GC=1) to exercise the
      # LRU model-eviction path; fill writes a large file to /var/tmp only.
      locations."= /_lemonseed/api/gc-test/fill" = {
        proxyPass = "http://127.0.0.1:8080";
        extraConfig = "${authGate}\n${adminGate}\n${cpProxy}\nerror_page 502 503 504 = @cp_interstitial;";
      };
      locations."= /_lemonseed/api/gc-test/fill/remove" = {
        proxyPass = "http://127.0.0.1:8080";
        extraConfig = "${authGate}\n${adminGate}\n${cpProxy}\nerror_page 502 503 504 = @cp_interstitial;";
      };
      locations."= /_lemonseed/api/gc-test/models" = {
        proxyPass = "http://127.0.0.1:8080";
        extraConfig = "${authGate}\n${adminGate}\n${cpProxy}\nerror_page 502 503 504 = @cp_interstitial;";
      };
      locations."= /api/data" = {
        proxyPass = "http://127.0.0.1:8080";
        extraConfig = "${authGate}\n${cpProxy}\nerror_page 502 503 504 = @cp_interstitial;";
      };
      locations."= /api/modules" = {
        proxyPass = "http://127.0.0.1:8080";
        extraConfig = "${authGate}\n${cpProxy}\nerror_page 502 503 504 = @cp_interstitial;";
      };
      locations."= /api/chat" = {
        proxyPass = "http://127.0.0.1:8080";
        extraConfig = "${authGate}\n${cpProxy}\nerror_page 502 503 504 = @cp_interstitial;";
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

      # Socket.IO / websockets (auth_request works on the upgrade handshake).
      locations."/ws/" = {
        proxyPass = "http://lemonseed_owui";
        extraConfig = "${authGate}\nerror_page 502 503 504 = @owui_loading;\n${owuiProxy}";
      };

      # Everything else (SPA assets, OWUI /api/v1, /c/…, …) → Open WebUI. [gated]
      #
      # This is the only location that carries the managed-path guards: every
      # Open WebUI REST path (/api/v1, /openai, /ollama) is matched here, so one
      # include covers the whole UI surface and "which requests are guarded?"
      # stays answerable from one line. /ws/ deliberately has none (Socket.IO
      # is the chat data path and mounts at /ws/socket.io).
      #
      # ``default_type`` is set here (not in the guard ``if``, where nginx
      # rejects it) so a guard's JSON denial body is served as
      # application/json and Open WebUI's client can read ``detail``; with the
      # default text/plain it renders its own generic connection error instead
      # of the reason. Nothing else in this location generates its own body —
      # the auth gate 401s to @auth_unauthorized and upstream failures go to
      # @owui_loading, both named locations with their own defaults — so the
      # only responses this relabels are proxied ones that omit Content-Type.
      locations."/" = {
        proxyPass = "http://lemonseed_owui";
        extraConfig = "    default_type application/json;\n${owuiGuards}\n${authGate}\nerror_page 502 503 504 = @owui_loading;\n${owuiProxy}";
      };
    };
  };

  systemd.services.lemonseed-auth-gate-seed = {
    description = "Seed empty edge-auth gate snippets";
    wantedBy = [ "multi-user.target" ];
    before = [ "nginx.service" ];
    after = [ "local-fs.target" ];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
      ExecStart = "${seedAuthGate}/bin/lemonseed-auth-gate-seed";
    };
  };

  systemd.services.nginx = {
    # Do NOT depend on appliance-control-plane: nginx must open :80 as soon as
    # the network is up, even while the CP (and lemond/docker) are still
    # starting. A dead CP upstream is handled by error_page 502 → the CP shell
    # interstitial, so the browser always gets a response instead of
    # ERR_CONNECTION_REFUSED during the boot window.
    after = [
      "network-online.target"
      "lemonseed-owui-upstream-seed.service"
      "lemonseed-auth-gate-seed.service"
    ];
    wants = [
      "network-online.target"
      "lemonseed-owui-upstream-seed.service"
      "lemonseed-auth-gate-seed.service"
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
