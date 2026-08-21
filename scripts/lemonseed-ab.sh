#!/usr/bin/env bash
# Lemonseed A/B boot helpers using GRUB environment block (grubenv).
#
# Usage:
#   lemonseed-ab status [--esp /mount/esp]
#   lemonseed-ab current-slot
#   lemonseed-ab inactive-slot
#   lemonseed-ab set-once <a|b> [--esp ...]
#   lemonseed-ab lock-in [a|b] [--esp ...]   # default: boot_attempted or detect
#   lemonseed-ab clear-attempt [--esp ...]
#   lemonseed-ab clear-once [--esp ...]
#   lemonseed-ab init [--esp ...]            # create grubenv with boot_default=a
#
# ESP is mounted on demand when --esp is omitted (PARTLABEL=ESP → /run/lemonseed-esp).
set -euo pipefail

ESP_MOUNT=""
ESP_AUTO=0
ACTION=""
SLOT_ARG=""

usage() {
  cat <<'EOF' >&2
Lemonseed A/B boot helpers using GRUB environment block (grubenv).

Usage:
  lemonseed-ab status [--esp /mount/esp]
  lemonseed-ab current-slot
  lemonseed-ab inactive-slot
  lemonseed-ab set-once <a|b> [--esp ...]
  lemonseed-ab lock-in [a|b] [--esp ...]    default: boot_attempted or detect
  lemonseed-ab clear-attempt [--esp ...]
  lemonseed-ab clear-once [--esp ...]
  lemonseed-ab init [--esp ...]             create grubenv with boot_default=a

ESP is mounted on demand when --esp is omitted (PARTLABEL=ESP → /run/lemonseed-esp).
EOF
  exit 2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    status|current-slot|inactive-slot|set-once|lock-in|clear-attempt|clear-once|init)
      ACTION="$1"
      shift
      ;;
    --esp)
      ESP_MOUNT="${2:-}"
      shift 2
      ;;
    a|b)
      SLOT_ARG="$1"
      shift
      ;;
    -h|--help) usage ;;
    *)
      echo "unknown arg: $1" >&2
      usage
      ;;
  esac
done

[[ -n "${ACTION}" ]] || usage

cleanup() {
  if [[ "${ESP_AUTO}" -eq 1 && -n "${ESP_MOUNT}" ]]; then
    umount "${ESP_MOUNT}" 2>/dev/null || true
    rmdir "${ESP_MOUNT}" 2>/dev/null || true
  fi
}
trap cleanup EXIT

ensure_esp() {
  if [[ -n "${ESP_MOUNT}" ]]; then
    [[ -d "${ESP_MOUNT}" ]] || {
      echo "ESP mountpoint missing: ${ESP_MOUNT}" >&2
      exit 1
    }
    return
  fi
  ESP_MOUNT=/run/lemonseed-esp
  mkdir -p "${ESP_MOUNT}"
  if ! mountpoint -q "${ESP_MOUNT}"; then
    if [[ ! -e /dev/disk/by-partlabel/ESP ]]; then
      echo "ESP partition not found (PARTLABEL=ESP)" >&2
      exit 1
    fi
    mount -t vfat -o rw /dev/disk/by-partlabel/ESP "${ESP_MOUNT}"
    ESP_AUTO=1
  fi
}

grubenv_path() {
  local p
  for p in "${ESP_MOUNT}/grub/grubenv" "${ESP_MOUNT}/EFI/lemonseed/grubenv" "${ESP_MOUNT}/boot/grub/grubenv"; do
    if [[ -e "${p}" ]] || [[ "${ACTION}" == "init" ]]; then
      mkdir -p "$(dirname "${p}")"
      printf '%s\n' "${p}"
      return
    fi
  done
  mkdir -p "${ESP_MOUNT}/grub"
  printf '%s\n' "${ESP_MOUNT}/grub/grubenv"
}

detect_slot_from_cmdline() {
  local arg
  if [[ ! -r /proc/cmdline ]]; then
    return 1
  fi
  # shellcheck disable=SC2013
  for arg in $(cat /proc/cmdline); do
    case "${arg}" in
      lemonseed.slot=a) printf 'a\n'; return 0 ;;
      lemonseed.slot=b) printf 'b\n'; return 0 ;;
      root=PARTLABEL=root-a) printf 'a\n'; return 0 ;;
      root=PARTLABEL=root-b) printf 'b\n'; return 0 ;;
    esac
  done
  return 1
}

current_slot() {
  local slot
  if slot="$(detect_slot_from_cmdline)"; then
    printf '%s\n' "${slot}"
    return 0
  fi
  if [[ -n "${boot_default:-}" && ( "${boot_default}" == "a" || "${boot_default}" == "b" ) ]]; then
    printf '%s\n' "${boot_default}"
    return 0
  fi
  printf 'a\n'
}

read_vars() {
  local env
  env="$(grubenv_path)"
  if [[ ! -f "${env}" ]]; then
    boot_default=""
    boot_once=""
    boot_attempted=""
    return
  fi
  # shellcheck disable=SC2034
  boot_default="$(grub-editenv "${env}" list 2>/dev/null | sed -n 's/^boot_default=//p' | head -1)"
  boot_once="$(grub-editenv "${env}" list 2>/dev/null | sed -n 's/^boot_once=//p' | head -1)"
  boot_attempted="$(grub-editenv "${env}" list 2>/dev/null | sed -n 's/^boot_attempted=//p' | head -1)"
}

# Commands that need the ESP.
case "${ACTION}" in
  current-slot)
    # Prefer cmdline; fall back to grubenv when ESP is available.
    if slot="$(detect_slot_from_cmdline 2>/dev/null)"; then
      printf '%s\n' "${slot}"
      exit 0
    fi
    ensure_esp
    ENV_FILE="$(grubenv_path)"
    read_vars
    current_slot
    exit 0
    ;;
  inactive-slot)
    if slot="$(detect_slot_from_cmdline 2>/dev/null)"; then
      :
    else
      ensure_esp
      ENV_FILE="$(grubenv_path)"
      read_vars
      slot="$(current_slot)"
    fi
    if [[ "${slot}" == "a" ]]; then
      printf 'b\n'
    else
      printf 'a\n'
    fi
    exit 0
    ;;
esac

ensure_esp
ENV_FILE="$(grubenv_path)"

case "${ACTION}" in
  init)
    if [[ ! -f "${ENV_FILE}" ]]; then
      grub-editenv "${ENV_FILE}" create
    fi
    grub-editenv "${ENV_FILE}" set boot_default=a
    grub-editenv "${ENV_FILE}" unset boot_once || true
    grub-editenv "${ENV_FILE}" unset boot_attempted || true
    echo "initialized ${ENV_FILE} (boot_default=a)"
    ;;
  status)
    read_vars
    cur="$(current_slot)"
    echo "grubenv: ${ENV_FILE}"
    echo "current_slot=${cur}"
    if [[ "${cur}" == "a" ]]; then
      echo "inactive_slot=b"
    else
      echo "inactive_slot=a"
    fi
    echo "boot_default=${boot_default:-}"
    echo "boot_once=${boot_once:-}"
    echo "boot_attempted=${boot_attempted:-}"
    ;;
  set-once)
    [[ "${SLOT_ARG}" == "a" || "${SLOT_ARG}" == "b" ]] || {
      echo "set-once requires a|b" >&2
      exit 1
    }
    [[ -f "${ENV_FILE}" ]] || grub-editenv "${ENV_FILE}" create
    grub-editenv "${ENV_FILE}" set "boot_once=${SLOT_ARG}"
    grub-editenv "${ENV_FILE}" unset boot_attempted || true
    echo "set boot_once=${SLOT_ARG}"
    ;;
  clear-attempt)
    [[ -f "${ENV_FILE}" ]] || exit 0
    grub-editenv "${ENV_FILE}" unset boot_attempted || true
    echo "cleared boot_attempted"
    ;;
  clear-once)
    [[ -f "${ENV_FILE}" ]] || exit 0
    grub-editenv "${ENV_FILE}" unset boot_once || true
    grub-editenv "${ENV_FILE}" unset boot_attempted || true
    echo "cleared boot_once and boot_attempted"
    ;;
  lock-in)
    read_vars
    slot="${SLOT_ARG}"
    if [[ -z "${slot}" ]]; then
      slot="${boot_attempted}"
    fi
    if [[ -z "${slot}" ]]; then
      slot="$(detect_slot_from_cmdline || true)"
    fi
    [[ "${slot}" == "a" || "${slot}" == "b" ]] || {
      echo "lock-in: could not determine slot" >&2
      exit 1
    }
    [[ -f "${ENV_FILE}" ]] || grub-editenv "${ENV_FILE}" create
    grub-editenv "${ENV_FILE}" set "boot_default=${slot}"
    grub-editenv "${ENV_FILE}" unset boot_once || true
    grub-editenv "${ENV_FILE}" unset boot_attempted || true
    echo "locked in boot_default=${slot}"
    ;;
esac
