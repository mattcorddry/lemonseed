# Public HTTP gateway: nginx :80 → Lemonseed shell + Open WebUI + CP APIs.

{ pkgs, ... }:

let
  brandAssets = ../../control-plane/static/brand;
  cpStatic = ../../control-plane/static;

  # M6.6 S5.5 — how long nginx is willing to wait for the CP before opening
  # :80 anyway. A boot where the CP comes up normally never shows the not-ready
  # page; a boot where it does not shows that page instead of a raw gateway
  # error. Two numbers only, because a hang must not become a boot stall.
  gatewayWaitSeconds = 20;

  # A real script file, not an inline snippet: systemd expands `$VAR` in an
  # ExecStart line, so arithmetic like $(date +%s) belongs somewhere the shell
  # — not the unit parser — reads it.
  gatewayWait = pkgs.writeShellApplication {
    name = "lemonseed-gateway-wait";
    runtimeInputs = with pkgs; [ coreutils curl ];
    text = ''
      deadline=$(( $(date +%s) + ${toString gatewayWaitSeconds} ))
      while :; do
        if curl -fsS -o /dev/null --max-time 2 http://127.0.0.1:8080/healthz; then
          echo "lemonseed-gateway: control plane is serving; opening :80"
          exit 0
        fi
        if [ "$(date +%s)" -ge "$deadline" ]; then
          echo "lemonseed-gateway: control plane not serving after ${toString gatewayWaitSeconds}s; opening :80 anyway" >&2
          exit 0
        fi
        sleep 0.5
      done
    '';
  };

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

  # Gate snippets for nginx. /run is tmpfs, so the boot seeder restores the
  # last-written state from user-data when it exists: otherwise every reboot
  # began with the edge gate OPEN until the control plane's gate worker waited
  # out Postgres and rewrote it, and a request in that window reached the CP
  # with no forwarded groups at all — an admin's /settings/ answered a bare
  # needs-admin JSON (bare-metal review). owui-guards.conf stays
  # empty-seeded on purpose: the guards are re-rendered from the manifest at
  # every boot reconcile, and a boot where the CP never ran blocks nothing.
  seedAuthGate = pkgs.writeShellApplication {
    name = "lemonseed-auth-gate-seed";
    runtimeInputs = [ pkgs.coreutils ];
    text = ''
      set -euo pipefail
      install -d -m 0755 /run/lemonseed
      install -d -m 0755 /var/lib/lemonseed
      for f in auth-gate.conf auth-admin.conf owui-guards.conf; do
        if [[ ! -e /run/lemonseed/$f ]]; then
          if [[ -s /var/lib/lemonseed/$f ]] && [[ $f != owui-guards.conf ]]; then
            install -m 0644 "/var/lib/lemonseed/$f" "/run/lemonseed/$f"
          else
            : > /run/lemonseed/$f
            chmod 0644 /run/lemonseed/$f
          fi
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
    # Micro-cache for the auth subrequest (BM-4 review): every gated request
    # runs one, so an OWUI page load (~25 API calls) at login or a single open
    # portal tab (boot/status/chat polls) hits the CP several times a second.
    # Lives in /run-backed /var/cache/nginx, gone at reboot.
    proxyCachePath."lemon-auth" = {
      enable = true;
      keysZoneName = "lemon_auth";
      keysZoneSize = "1m";
      inactive = "30s";
      maxSize = "4m";
    };
    appendHttpConfig = ''
      include /run/lemonseed/owui-upstream.conf;
      # Access logs via syslog socket → journald (no /var/log/nginx files).
      access_log syslog:server=unix:/dev/log,nohostname,tag=nginx;
      # The gate's identity headers overflow nginx's compiled default hash
      # (bucket 64), and the parse-time warnings land in the compiled default
      # error log — /var/log/nginx/error.log — not journald. Size for the
      # headers the gate actually carries (BM-3 review).
      proxy_headers_hash_max_size 1024;
      proxy_headers_hash_bucket_size 128;

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

      # Same split, one level down: which not-ready page answers when / fails.
      # A document request's upstream is the CP, so only the static page can
      # render; an embed request's upstream is OWUI, so the CP shell can.
      map "$http_sec_fetch_dest:$arg_embed" $lemonseed_error_target {
        default      /_lemonseed/cp-down;
        "iframe:"    /_lemonseed/interstitial;
        "iframe:1"   /_lemonseed/interstitial;
        ":1"         /_lemonseed/interstitial;
        "empty:1"    /_lemonseed/interstitial;
        "document:1" /_lemonseed/interstitial;
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
      # page handles ?rd= for the post-login redirect).
      #
      # M6.6 S5.4 — the error contract, measured against nginx 1.30 rather than
      # assumed. Three findings shaped it:
      #
      # 1. `alias` is rejected inside a named location, so the static page is
      #    pinned with try_files under an explicit root.
      # 2. nginx does not run error_page inside a location it reached *through*
      #    error_page (it blocks error-page recursion). So the previous pattern
      #    — error_page 502 → @cp_interstitial → proxy_pass to the CP — could
      #    never render a friendly page when the CP was the thing that was
      #    down: the second proxy just failed again and the browser got nginx's
      #    stock page. That is the raw "502"/"500" the user saw on boot, and no
      #    amount of nesting fixes it: the fallback has to be chosen by the
      #    originating location.
      # 3. proxy_intercept_errors is off (and stays off), so a 500 produced *by
      #    the CP* passes through untouched. Only errors nginx generates itself
      #    — connect refused, timeout, no live upstreams — get the friendly
      #    page. A real CP bug therefore stays visible instead of being masked
      #    as "still starting".
      #
      # `=503` keeps the status honest while the body renders. cp-down.html's
      # only subresource is the brand mark, which nginx serves straight from
      # SquashFS, so it cannot fail the same way twice.
      extraConfig = ''
        ${authVarDecls}
        location @auth_unauthorized {
          # fetch() follows a 302 silently and hands back login HTML at HTTP
          # 200, so a client's `if (!res.ok)` can never fire: that is how an
          # expired admin session turned into dead UI (or a bogus "managed by
          # Lemonseed") instead of a sign-in prompt. An XHR therefore gets a
          # real 401 with a body it can render; only navigations redirect.
          # `return` inside `if` is the one sanctioned use of `if` here, and
          # default_type is at location level because it is not valid in `if`.
          default_type application/json;
          if ($http_x_requested_with = "fetch") {
            return 401 '{"detail":"Sign in again to continue.","lemonseed":"session-expired"}';
          }
          return 302 /login?rd=$request_uri;
        }
        location /_lemonseed/cp-down {
          internal;
          root ${cpStatic};
          try_files /cp-down.html =503;
          default_type text/html;
          add_header Cache-Control "no-store" always;
        }
        # CP-shell interstitial: used where the *failing* upstream is not the
        # CP (the OWUI iframe, and the root for embed requests), so proxying to
        # the CP still has something useful to say. Reached by internal
        # redirect, which rewrites $uri, hence $request_uri in proxy_pass.
        location /_lemonseed/interstitial {
          internal;
          proxy_pass http://127.0.0.1:8080$request_uri;
          proxy_http_version 1.1;
          proxy_set_header Host $host;
          proxy_set_header X-Real-IP $remote_addr;
          proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
          proxy_set_header X-Forwarded-Proto $scheme;
          ${identityHeaders}
        }
        # Server-level fallback for locations with no error_page of their own
        # (a gated location always has one, because auth-gate.conf contributes
        # the 401 clause and any error_page at that level replaces this one).
        error_page 500 502 503 504 =503 /_lemonseed/cp-down;
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
      #
      # The error target is a map for the same reason the upstream is one: for a
      # document request the upstream *is* the CP (so a failure needs the static
      # page), while for an embed request it is OWUI (so the CP can still
      # explain what is happening).
      locations."= /" = {
        extraConfig = ''
          ${authGate}
          error_page 500 502 503 504 =503 $lemonseed_error_target;
          proxy_pass $lemonseed_root_upstream;
          ${rootProxy}
        '';
      };
      locations."= /index.html" = {
        proxyPass = "http://127.0.0.1:8080";
        extraConfig = "${authGate}\n${cpProxy}\nerror_page 500 502 503 504 =503 /_lemonseed/cp-down;";
      };

      # Edge auth (CP-native, M6.2): the login page is open (it IS the sign-in
      # form); logout is gated (only a signed-in user can log out).
      locations."= /login" = {
        proxyPass = "http://127.0.0.1:8080";
        extraConfig = "${cpProxy}\nerror_page 500 502 503 504 =503 /_lemonseed/cp-down;";
      };
      locations."= /logout" = {
        proxyPass = "http://127.0.0.1:8080";
        extraConfig = "${authGate}\n${cpProxy}\nerror_page 500 502 503 504 =503 /_lemonseed/cp-down;";
      };
      # M7 P2: SRP-6a handshake phases. Deliberately open, like /login and
      # /setup — these ARE sign-in, so gating them would be circular. Exact-match
      # locations because locations."/" proxies to Open WebUI, not the CP, so a
      # prefix route would 404 from the wrong service.
      locations."= /_lemonseed/api/srp/challenge" = {
        proxyPass = "http://127.0.0.1:8080";
        extraConfig = "${cpProxy}\nerror_page 500 502 503 504 =503 /_lemonseed/cp-down;";
      };
      locations."= /_lemonseed/api/srp/verify" = {
        proxyPass = "http://127.0.0.1:8080";
        extraConfig = "${cpProxy}\nerror_page 500 502 503 504 =503 /_lemonseed/cp-down;";
      };
      # M7 P3: rotate your own password. Open like the handshake above — the
      # proof it demands IS knowledge of the current password, so a session
      # cookie would add nothing (and a forced rotation happens *before* one
      # exists, D-11).
      locations."= /_lemonseed/api/srp/rotate" = {
        proxyPass = "http://127.0.0.1:8080";
        extraConfig = "${cpProxy}\nerror_page 500 502 503 504 =503 /_lemonseed/cp-down;";
      };
      # M7 P3/P4: identity management. /_lemonseed/ is the CP's own namespace
      # (OWUI lives on /api/v1 and friends), so a prefix route here cannot
      # shadow anything the module serves; nginx prefers the longest prefix,
      # which beats locations."/".
      locations."= /_lemonseed/api/profile" = {
        proxyPass = "http://127.0.0.1:8080";
        extraConfig = "${authGate}\n${cpProxy}\nerror_page 500 502 503 504 =503 /_lemonseed/cp-down;";
      };
      locations."= /_lemonseed/api/users" = {
        proxyPass = "http://127.0.0.1:8080";
        extraConfig = "${authGate}\n${adminGate}\n${cpProxy}\nerror_page 500 502 503 504 =503 /_lemonseed/cp-down;";
      };
      locations."/_lemonseed/api/users/" = {
        proxyPass = "http://127.0.0.1:8080";
        extraConfig = "${authGate}\n${adminGate}\n${cpProxy}\nerror_page 500 502 503 504 =503 /_lemonseed/cp-down;";
      };
      locations."/_lemonseed/api/groups/" = {
        proxyPass = "http://127.0.0.1:8080";
        extraConfig = "${authGate}\n${adminGate}\n${cpProxy}\nerror_page 500 502 503 504 =503 /_lemonseed/cp-down;";
      };
      locations."= /_lemonseed/api/audit" = {
        proxyPass = "http://127.0.0.1:8080";
        extraConfig = "${authGate}\n${adminGate}\n${cpProxy}\nerror_page 500 502 503 504 =503 /_lemonseed/cp-down;";
      };
      # Sign-in-code vocabulary (M7 P3). Admin-only, and mirrored in the CP: the
      # gate is the piece a future edit can misconfigure.
      locations."= /_lemonseed/api/wordlist" = {
        proxyPass = "http://127.0.0.1:8080";
        extraConfig = "${authGate}\n${adminGate}\n${cpProxy}\nerror_page 500 502 503 504 =503 /_lemonseed/cp-down;";
      };
      # Reboot / shut down (M7 P5). Admin-only at the edge and in the CP: this is
      # the one button that takes the whole appliance away from everyone else.
      locations."= /_lemonseed/api/power" = {
        proxyPass = "http://127.0.0.1:8080";
        extraConfig = "${authGate}\n${adminGate}\n${cpProxy}\nerror_page 500 502 503 504 =503 /_lemonseed/cp-down;";
      };
      # Update affordance (M7 P5). Admin-only: the snapshot (running vs latest,
      # the failed-update banner), *Check now*, and the install job that reboots
      # the appliance when it finishes (D4). The updater itself is packaged
      # once; the CP spawns it, so no new service or port is exposed.
      locations."= /_lemonseed/api/update" = {
        proxyPass = "http://127.0.0.1:8080";
        extraConfig = "${authGate}\n${adminGate}\n${cpProxy}\nerror_page 500 502 503 504 =503 /_lemonseed/cp-down;";
      };
      locations."= /_lemonseed/api/update/check" = {
        proxyPass = "http://127.0.0.1:8080";
        extraConfig = "${authGate}\n${adminGate}\n${cpProxy}\nerror_page 500 502 503 504 =503 /_lemonseed/cp-down;";
      };
      # Rollback (M8 P6, D30): boots the prior slot. Admin-only, like the rest
      # of the update affordance.
      locations."= /_lemonseed/api/update/rollback" = {
        proxyPass = "http://127.0.0.1:8080";
        extraConfig = "${authGate}\n${adminGate}\n${cpProxy}\nerror_page 500 502 503 504 =503 /_lemonseed/cp-down;";
      };
      # Internal auth_request target (never LAN-facing). The CP verifies the
      # signed session cookie and returns 200 + X-Forwarded-* headers (read via
      # auth_request_set in the gate snippet) or 401.
      #
      # Micro-cached per session cookie (BM-4 review): without it every gated
      # request — each of OWUI's ~25 page-load calls, each portal poll — runs
      # a CP round-trip. A 5s TTL bounds revocation/logout delay to the same;
      # 401s and 503s are never cached, so invalid sessions and a down store
      # stay fail-closed. proxy_cache_lock collapses a page-load burst into
      # one CP verify.
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
          proxy_cache lemon_auth;
          # Keep in step with auth.SESSION_COOKIE (control-plane/auth.py).
          proxy_cache_key $cookie_lemonseed_session;
          proxy_cache_valid 200 5s;
          proxy_cache_lock on;
        '';
      };

      # First-boot setup + health probes stay open (pre-admin trust window).
      locations."= /setup" = {
        proxyPass = "http://127.0.0.1:8080";
        extraConfig = "${cpProxy}\nerror_page 500 502 503 504 =503 /_lemonseed/cp-down;";
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
        extraConfig = "${authGate}\n${adminGate}\n${cpProxy}\nerror_page 500 502 503 504 =503 /_lemonseed/cp-down;";
      };
      locations."= /settings" = {
        return = "302 /settings/";
      };

      # My Profile (M7 P3). Gated but NOT admin-gated, and that difference is the
      # point: a household member who is not an admin has no other way to reach
      # their own details or change their own password.
      locations."/profile/" = {
        proxyPass = "http://127.0.0.1:8080";
        extraConfig = "${authGate}\n${cpProxy}\nerror_page 500 502 503 504 =503 /_lemonseed/cp-down;";
      };
      locations."= /profile" = {
        return = "302 /profile/";
      };

      # Reserved Lemonseed JSON APIs (must win over OWUI /api/*).
      # /api/boot + /api/status stay open: the installer handoff after reboot and
      # the gateway's own not-ready page both have to reach them before anyone
      # can sign in, so they answer with a subset that holds no household facts
      # (M6.6 step 5). /api/system is the full snapshot and is gated.
      locations."= /api/boot" = {
        proxyPass = "http://127.0.0.1:8080";
        extraConfig = cpProxy;
      };
      locations."= /api/status" = {
        proxyPass = "http://127.0.0.1:8080";
        extraConfig = "${cpProxy}\nerror_page 500 502 503 504 =503 /_lemonseed/cp-down;";
      };
      locations."= /api/system" = {
        proxyPass = "http://127.0.0.1:8080";
        extraConfig = "${authGate}\n${cpProxy}\nerror_page 500 502 503 504 =503 /_lemonseed/cp-down;";
      };
      locations."= /api/hardware" = {
        proxyPass = "http://127.0.0.1:8080";
        extraConfig = "${authGate}\n${cpProxy}\nerror_page 500 502 503 504 =503 /_lemonseed/cp-down;";
      };
      locations."= /api/plan" = {
        proxyPass = "http://127.0.0.1:8080";
        extraConfig = "${authGate}\n${cpProxy}\nerror_page 500 502 503 504 =503 /_lemonseed/cp-down;";
      };
      # Model picker (M6.5): GET reads families + best-fit; POST records the
      # user's choice and is admin-gated in the CP (like /settings/).
      # Namespaced under /_lemonseed/ (not /api/models) so it does not shadow
      # Open WebUI's own /api/models — the OWUI frontend needs that path for
      # its OpenAI-style model list; the exact-match /api/models location used
      # to win and silently returned the CP's picker payload to OWUI.
      locations."= /_lemonseed/api/models" = {
        proxyPass = "http://127.0.0.1:8080";
        extraConfig = "${authGate}\n${adminGate}\n${cpProxy}\nerror_page 500 502 503 504 =503 /_lemonseed/cp-down;";
      };
      # Open WebUI config ownership view (M6.3b 4A). Exact match so OWUI still
      # owns every other path; admin-gated here *and* in the CP, because the
      # payload merges Open WebUI's (unredacted) config export.
      locations."= /_lemonseed/api/owui/config" = {
        proxyPass = "http://127.0.0.1:8080";
        extraConfig = "${authGate}\n${adminGate}\n${cpProxy}\nerror_page 500 502 503 504 =503 /_lemonseed/cp-down;";
      };
      # Phase 4B admin actions (M6.3b): re-apply Lemonseed's intent now, and the
      # config reset window. Same double gating as the ownership view. The reset
      # handler returns 202 immediately and reports through /api/modules, so
      # there is no long-held connection to time out here.
      locations."= /_lemonseed/api/owui/reconcile" = {
        proxyPass = "http://127.0.0.1:8080";
        extraConfig = "${authGate}\n${adminGate}\n${cpProxy}\nerror_page 500 502 503 504 =503 /_lemonseed/cp-down;";
      };
      locations."= /_lemonseed/api/owui/reset-config" = {
        proxyPass = "http://127.0.0.1:8080";
        extraConfig = "${authGate}\n${adminGate}\n${cpProxy}\nerror_page 500 502 503 504 =503 /_lemonseed/cp-down;";
      };
      # GC e2e test endpoints (admin-gated): disk filler + cached-model list.
      # Used by scripts/qemu-api-smoke-test.sh (QEMU_E2E_GC=1) to exercise the
      # LRU model-eviction path; fill writes a large file to /var/tmp only.
      locations."= /_lemonseed/api/gc-test/fill" = {
        proxyPass = "http://127.0.0.1:8080";
        extraConfig = "${authGate}\n${adminGate}\n${cpProxy}\nerror_page 500 502 503 504 =503 /_lemonseed/cp-down;";
      };
      locations."= /_lemonseed/api/gc-test/fill/remove" = {
        proxyPass = "http://127.0.0.1:8080";
        extraConfig = "${authGate}\n${adminGate}\n${cpProxy}\nerror_page 500 502 503 504 =503 /_lemonseed/cp-down;";
      };
      locations."= /_lemonseed/api/gc-test/models" = {
        proxyPass = "http://127.0.0.1:8080";
        extraConfig = "${authGate}\n${adminGate}\n${cpProxy}\nerror_page 500 502 503 504 =503 /_lemonseed/cp-down;";
      };
      locations."= /api/data" = {
        proxyPass = "http://127.0.0.1:8080";
        extraConfig = "${authGate}\n${cpProxy}\nerror_page 500 502 503 504 =503 /_lemonseed/cp-down;";
      };
      locations."= /api/modules" = {
        proxyPass = "http://127.0.0.1:8080";
        extraConfig = "${authGate}\n${cpProxy}\nerror_page 500 502 503 504 =503 /_lemonseed/cp-down;";
      };
      locations."= /api/chat" = {
        proxyPass = "http://127.0.0.1:8080";
        extraConfig = "${authGate}\n${cpProxy}\nerror_page 500 502 503 504 =503 /_lemonseed/cp-down;";
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

      # The one stylesheet root (M6.6 S5.8): setup, login, the shell, the settings
      # panel and the gateway's own not-ready page all read this file, so they
      # cannot drift apart. Served from the read-only image, not the control
      # plane, for two reasons: the login and setup pages need it *before* anyone
      # has a session, and cp-down.html needs it while the control plane is down.
      # `root` + `try_files` rather than `alias` because an exact-match location
      # with root would otherwise look for /_lemonseed/lemonseed.css on disk.
      # Revalidated rather than cached for a week: the store path changes when the
      # theme changes, but the URL does not, so a long expiry would pin a stale
      # theme across an upgrade.
      locations."= /_lemonseed/lemonseed.css" = {
        root = "${cpStatic}";
        extraConfig = ''
          try_files /lemonseed.css =404;
          default_type text/css;
          add_header Cache-Control "public, max-age=0, must-revalidate";
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

  # M6.6 S5.5 — open :80 in the right order without *depending* on the CP.
  #
  # A hard after=appliance-control-plane.service would be the wrong edge: the CP
  # is what starts Docker containers, so the dependency would be circular in the
  # limit, and a hung CP would then mean no web UI ever. Instead nginx waits a
  # bounded time for the CP to serve its health endpoint, which removes the
  # error page from the common boot, and opens :80 anyway on timeout, which
  # keeps a slow or broken CP from taking the gateway down with it.
  #
  # The gate is "the CP answers HTTP", not "the appliance is fully ready": the
  # CP renders its own progress UI for anything still starting, so waiting for
  # more here would only delay a page that already tells the truth.
  services.nginx.preStart = "${gatewayWait}/bin/lemonseed-gateway-wait";

  systemd.services.nginx = {
    # Do NOT depend on appliance-control-plane: nginx must open :80 as soon as
    # the network is up, even while the CP (and lemond/docker) are still
    # starting. The bounded preStart poll above covers the normal case, and
    # /_lemonseed/cp-down (error_page 500 502 503 504 =503) covers the rest of
    # the window, so the browser always gets a Lemonseed page instead of
    # ERR_CONNECTION_REFUSED or nginx's stock error.
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
    # Restart is already "always"/RestartSec=10s from the nginx module, which is
    # strictly stronger than the on-failure we asked for, so nothing to set.
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
