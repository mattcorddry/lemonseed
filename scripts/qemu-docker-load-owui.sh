#!/usr/bin/env bash
set -euo pipefail
if ! docker info >/dev/null 2>&1; then
  echo "lemonseed-qemu-docker-load-owui: docker daemon not ready" >&2
  exit 1
fi
mount="/run/lemonseed-qemu-owui"
if [[ ! -d "$mount" ]]; then
  echo "lemonseed-qemu-docker-load-owui: no virtio export at $mount; skipping"
  exit 0
fi
found=0
# Only Open WebUI — the share may be a broad host path (e.g. /nix/store).
while IFS= read -r -d '' tar; do
  found=1
  echo "lemonseed-qemu-docker-load-owui: loading $tar ($(wc -c <"$tar") bytes)"
  docker load -i "$tar"
  echo "lemonseed-qemu-docker-load-owui: loaded $tar"
done < <(find "$mount" -maxdepth 1 -name '*open-webui*.tar' -type f -print0 2>/dev/null || true)
if [[ "$found" -eq 0 ]]; then
  echo "lemonseed-qemu-docker-load-owui: no *open-webui*.tar under $mount (ls follows):"
  ls -la "$mount" || true
  exit 0
fi
