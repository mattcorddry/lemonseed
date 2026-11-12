#!/usr/bin/env bash
set -euo pipefail
if ! docker info >/dev/null 2>&1; then
  echo "lemonseed-qemu-docker-load-data: docker daemon not ready" >&2
  exit 1
fi
# @HELLO_TAR@ is the M9 P2 module image (traefik/whoami, the hello-world
# module): the QEMU smoke installs it, and without the preload it would have
# to pull from the registry under TCG. It satisfies the data-plane pull's
# tag fallback (the tarballs carry RepoTags but no RepoDigests), so the
# module install never needs the guest to have a working registry path.
for tar in @POSTGRES_TAR@ @VALKEY_TAR@ @HELLO_TAR@; do
  echo "lemonseed-qemu-docker-load-data: loading $tar"
  docker load -i "$tar"
done
