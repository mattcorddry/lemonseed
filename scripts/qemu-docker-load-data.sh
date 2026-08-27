#!/usr/bin/env bash
set -euo pipefail
if ! docker info >/dev/null 2>&1; then
  echo "lemonseed-qemu-docker-load-data: docker daemon not ready" >&2
  exit 1
fi
for tar in @POSTGRES_TAR@ @VALKEY_TAR@; do
  echo "lemonseed-qemu-docker-load-data: loading $tar"
  docker load -i "$tar"
done
