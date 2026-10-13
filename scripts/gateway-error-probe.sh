#!/usr/bin/env bash
# Live test of the gateway's 5xx error contract, with a real nginx (M6.6 S5.4).
#
#   make test-gateway
#
# Why this exists when we have unit tests and a QEMU smoke test: the behaviour
# under test is nginx's own error handling, and two of its rules are not what
# you would guess.
#
#   1. `alias` is rejected inside a named location — a config that looks fine
#      and does not parse, which would take the whole gateway down at boot.
#   2. error_page does NOT run inside a location reached *through* error_page
#      (nginx blocks error-page recursion). So the interstitials we used to
#      ship — error_page 502 -> named location -> proxy_pass to the control
#      plane — could never render a friendly page when the control plane was the
#      thing that was down. The browser got nginx's stock page, which is the
#      bug this row set out to fix, and no amount of nesting fixes it.
#   3. proxy_intercept_errors stays off, so a 500 produced *by* the control
#      plane passes through instead of being disguised as "still starting".
#
# None of that is observable from a Python unit test, and reaching the failing
# state in QEMU would mean stopping the control plane on a booted appliance. So
# we reproduce the exact location topology here, on the dev box, with no root
# and no hypervisor, and assert on real status codes and real bodies.
#
# The config below is a faithful copy of nixos/modules/gateway.nix. GUARD checks
# below fail if the module drifts away from what this probe claims to mirror.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GATEWAY="${ROOT}/nixos/modules/gateway.nix"
PAGE="${ROOT}/control-plane/static/cp-down.html"
WORK="$(mktemp -d)"
NGINX_PID=""

cleanup() {
  if [[ -n "${NGINX_PID}" ]] && kill -0 "${NGINX_PID}" 2>/dev/null; then
    kill "${NGINX_PID}" 2>/dev/null || true
    wait "${NGINX_PID}" 2>/dev/null || true
  fi
  rm -rf "${WORK}"
}
trap cleanup EXIT

fail() {
  echo "gateway error probe: $*" >&2
  exit 1
}

[[ -f "${PAGE}" ]] || fail "${PAGE} is missing"

# --- drift guards: the module must still describe the contract we are testing --
guard() {
  grep -qF "$1" "${GATEWAY}" || fail "gateway.nix no longer contains '$1' — update this probe to match the new contract"
}
guard 'error_page 500 502 503 504 =503 /_lemonseed/cp-down;'
guard 'try_files /cp-down.html =503;'
guard 'root ${cpStatic};'
guard 'proxy_pass http://127.0.0.1:8080$request_uri;'
guard 'error_page 500 502 503 504 =503 $lemonseed_error_target;'
guard 'default      /_lemonseed/cp-down;'
# Comments are allowed to name the pattern we removed (the module explains it),
# so this one check runs against live directive text only.
if grep -vE '^\s*#' "${GATEWAY}" | grep -qF '@cp_interstitial'; then
  fail "gateway.nix reintroduced @cp_interstitial (error_page -> proxy to the dead control plane)"
fi

# --- find an nginx ----------------------------------------------------------------
if [[ -n "${NGINX_BIN:-}" && -x "${NGINX_BIN}" ]]; then
  :
elif command -v nginx >/dev/null 2>&1; then
  NGINX_BIN="$(command -v nginx)"
elif command -v nix >/dev/null 2>&1; then
  NGINX_BIN="$(nix build --no-link --print-out-paths 'nixpkgs#nginx' 2>/dev/null || true)/bin/nginx"
fi
[[ -n "${NGINX_BIN:-}" && -x "${NGINX_BIN}" ]] || fail "no nginx found (set NGINX_BIN=/path/to/nginx)"

GATEWAY_PORT=18099
CP_PORT=18098
DEAD_PORT=18097   # nothing listens here; nginx generates the 502 itself
mkdir -p "${WORK}/logs" "${WORK}/client_body" "${WORK}/proxy"

# --- the topology, copied from the module -----------------------------------------
cat >"${WORK}/nginx.conf" <<CONF
pid ${WORK}/logs/nginx.pid;
error_log ${WORK}/logs/error.log warn;
worker_processes 1;
events { worker_connections 64; }
http {
  access_log off;
  client_body_temp_path ${WORK}/client_body;
  proxy_temp_path ${WORK}/proxy;

  map "\$http_sec_fetch_dest:\$arg_embed" \$lemonseed_error_target {
    default      /_lemonseed/cp-down;
    "iframe:"    /_lemonseed/interstitial;
    "iframe:1"   /_lemonseed/interstitial;
    ":1"         /_lemonseed/interstitial;
    "empty:1"    /_lemonseed/interstitial;
    "document:1" /_lemonseed/interstitial;
  }

  server {
    # Fake control plane: answers 200, and answers 500 on one path so we can
    # prove a backend-generated error is not swallowed.
    listen 127.0.0.1:${CP_PORT};
    # Echoes the path it received: an internal redirect rewrites \$uri, so this is
    # how the probe can see whether the original request path survived.
    location = / { return 200 "SHELL \$request_uri\n"; }
    location = /api/boom { return 500; }
    location / { return 200 "SHELL-ANY \$request_uri\n"; }
  }

  server {
    listen 127.0.0.1:${GATEWAY_PORT};

    location @auth_unauthorized { return 302 /login?rd=\$request_uri; }

    location /_lemonseed/cp-down {
      internal;
      root ${ROOT}/control-plane/static;
      try_files /cp-down.html =503;
      default_type text/html;
      add_header Cache-Control "no-store" always;
    }
    location /_lemonseed/interstitial {
      internal;
      proxy_pass http://127.0.0.1:${CP_PORT}\$request_uri;
      proxy_http_version 1.1;
    }
    error_page 500 502 503 504 =503 /_lemonseed/cp-down;

    location = / {
      proxy_pass http://127.0.0.1:${DEAD_PORT};
      error_page 500 502 503 504 =503 \$lemonseed_error_target;
    }
    location = /api/status { proxy_pass http://127.0.0.1:${DEAD_PORT}; }
    location = /api/boom { proxy_pass http://127.0.0.1:${CP_PORT}; }
    location = /ok { return 200 "ok\n"; }
    location = /gated { error_page 401 = @auth_unauthorized; return 401; }
  }
}
CONF

"${NGINX_BIN}" -p "${WORK}" -c nginx.conf -t 2>&1 | grep -v 'could not open error log' || fail "nginx rejected the config"

# --- negative control: the pattern we removed really is invalid -------------------
# `alias` is rejected inside a named location. If nginx ever allows it, the
# comment in gateway.nix should be rewritten, so say so loudly instead of
# silently keeping a stale justification.
cat >"${WORK}/bad.conf" <<BADCONF
pid ${WORK}/logs/nginx.pid;
error_log ${WORK}/logs/error.log warn;
events { worker_connections 8; }
http {
  access_log off;
  client_body_temp_path ${WORK}/client_body;
  server {
    listen 127.0.0.1:${GATEWAY_PORT};
    location @cp_down { alias ${PAGE}; }
    error_page 502 =503 @cp_down;
  }
}
BADCONF
if "${NGINX_BIN}" -p "${WORK}" -c bad.conf -t >"${WORK}/bad.out" 2>&1; then
  fail "nginx accepted alias inside a named location — the probe and the module comment need updating"
fi
grep -qi 'cannot be used' "${WORK}/bad.out" || fail "nginx rejected it for an unexpected reason: $(tr -d '\n' <"${WORK}/bad.out" | tail -c 200)"
echo "OK: nginx rejects alias inside a named location, and the module knows it"

"${NGINX_BIN}" -p "${WORK}" -c nginx.conf
NGINX_PID="$(cat "${WORK}/logs/nginx.pid")"
for _ in 1 2 3 4 5 6 7 8 9 10; do
  curl -s -o /dev/null --max-time 1 "http://127.0.0.1:${GATEWAY_PORT}/ok" && break
  sleep 0.3
done

base="http://127.0.0.1:${GATEWAY_PORT}"
# probe <name> <expected-code> <path> [extra curl args...]
probe() {
  local name="$1" expect="$2" path="$3"
  shift 3
  local body code
  body="$(mktemp)"
  code="$(curl -s -o "${body}" -w '%{http_code}' --max-time 10 "$@" "${base}${path}" 2>/dev/null || echo 000)"
  if [[ "${code}" != "${expect}" ]]; then
    cat "${body}" >&2
    fail "${name}: expected HTTP ${expect}, got ${code} for ${path}"
  fi
  printf '  %-28s %s\n' "${name}" "${code}"
  rm -f "${body}"
}

echo "==> the contract, against a live nginx"
# Control: a healthy location and the auth redirect must be untouched.
probe "healthy location" 200 /ok
probe "401 gate redirects" 302 /gated
# The bug: an unreachable upstream must produce a Lemonseed page, honestly 503.
probe "root, cp down" 503 / -H 'Accept: text/html'
probe "api, cp down" 503 /api/status
# The embed branch keeps the control-plane shell and the original path.
probe "embed, upstream down" 503 "/?embed=1" -H 'Sec-Fetch-Dest: iframe'
# A 500 from the backend is a real answer and must not be disguised.
probe "backend 500 passes through" 500 /api/boom
# And the page itself is never directly reachable from the LAN.
probe "fallback is internal" 404 /_lemonseed/cp-down

# Body checks: the right page landed at the right status.
root_body="$(curl -s --max-time 10 "${base}/")"
grep -q "Lemonseed is getting ready" <<<"${root_body}" || fail "root did not render the not-ready page"
embed_body="$(curl -s -H 'Sec-Fetch-Dest: iframe' --max-time 10 "${base}/?embed=1")"
grep -q "SHELL /?embed=1" <<<"${embed_body}" || fail "embed lost the original request path: ${embed_body}"
headers="$(curl -s -D - -o /dev/null --max-time 10 "${base}/")"
grep -qi 'cache-control: no-store' <<<"${headers}" || fail "the not-ready page is cacheable"
grep -qi 'content-type: text/html' <<<"${headers}" || fail "the not-ready page is not served as html"

echo "OK: 5xx from a dead upstream renders a Lemonseed page at 503, and the shell keeps its path"
