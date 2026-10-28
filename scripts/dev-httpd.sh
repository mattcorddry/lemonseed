#!/usr/bin/env bash
# make dev-httpd-start / dev-httpd-stop (M8 P3, D-C3): serve ./out over HTTP
# for local installer/updater testing of --url sets — via Docker, so no
# distro httpd path is depended on. busybox httpd: static files only, no CGI.
# Bound to the primary interface's own address (iproute2-detected), so the
# exposure is exactly the local subnet rather than every interface; set
# DEV_HTTPD_BIND to override, or rely on the 0.0.0.0 fallback (with a
# printed warning) when the address cannot be detected.
set -euo pipefail

NAME="lemonseed-dev-httpd"
IMAGE="${DEV_HTTPD_IMAGE:-busybox:latest}"
PORT="${DEV_HTTPD_PORT:-8083}"

start() {
  if docker container inspect "$NAME" >/dev/null 2>&1; then
    echo "==> dev-httpd already running; stopping the stale one first"
    stop
  fi

  bind="${DEV_HTTPD_BIND:-}"
  if [[ -z "$bind" ]]; then
    # The address the default route uses: the primary interface's own IP.
    # (Cleaner than the subnet-grep dance — binding to that single address
    # IS the "local subnet only" restriction, at the publish level.)
    bind="$(ip -4 route get 1.1.1.1 2>/dev/null | sed -n 's/.*src \([0-9.]*\).*/\1/p' | head -1)"
    if [[ -z "$bind" ]]; then
      bind="0.0.0.0"
      echo "WARNING: could not detect the primary interface address; binding to 0.0.0.0" >&2
    fi
  fi

  if [[ ! -d "out" ]]; then
    echo "No out/ to serve — run a build first." >&2
    exit 1
  fi

  docker run -d --rm --name "$NAME" \
    -p "$bind:$PORT:8083" \
    -v "$(pwd)/out:/srv:ro" \
    "$IMAGE" httpd -f -p 8083 -h /srv >/dev/null

  echo "==> dev-httpd serving ./out on http://$bind:$PORT/ (container $NAME)"
  echo "    e.g. --url http://$bind:$PORT/release-<version>/"
  echo "    stop it with: make dev-httpd-stop"
}

stop() {
  if docker container inspect "$NAME" >/dev/null 2>&1; then
    docker rm -f "$NAME" >/dev/null
    echo "==> dev-httpd stopped"
  else
    echo "==> dev-httpd is not running"
  fi
}

case "${1:-start}" in
  start) start ;;
  stop) stop ;;
  *)
    echo "usage: $0 {start|stop}" >&2
    exit 2
    ;;
esac
