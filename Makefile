# Lemonseed (appliance) image build pipeline
#
#   make                     # this help
#   make build-production    # production image → out/latest
#   make build-qemu          # QEMU smoke image → out/latest-qemu
#   make build-installer     # EFI/USB installer ISO → out/latest-installer
#   make run-qemu            # build-qemu if missing, then boot serial console
#   make qemu-clean          # remove QEMU user-data disk image
#   make repart-generate     partitions.yaml → out/repart.d/*.conf
#   make disk-repart DISK=…  apply (or --dry-run) systemd-repart to a disk
#   make refresh-image-pins  rebase container image pins (KEYS=… to limit)
#   make check-image-pins    report pins whose upstream digest has moved
#   make clean
#   make keys
#
#   VERSION=20260810.01 make build-production
#   SIGN=1 make build-production

SHELL := /bin/bash
.DEFAULT_GOAL := help

ROOT := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))

SIGN ?= 0
DISK ?=

.PHONY: help build-production build-qemu build-installer run-qemu qemu-clean clean keys \
	repart-generate disk-repart grub-cfg repart-test update-test test test-qemu \
	test-update-unit check-untracked nix-gc lint refresh-image-pins check-image-pins \
	test-gateway

help:
	@echo "Lemonseed image build pipeline"
	@echo ""
	@echo "Targets:"
	@echo "  make build-production  Build production SquashFS + vmlinuz (+ initrd)"
	@echo "                         → out/appliance-<version>/  (symlink: out/latest)"
	@echo "  make build-qemu        Build QEMU smoke-test image"
	@echo "                         → out/appliance-<version>-qemu/  (symlink: out/latest-qemu)"
	@echo "  make build-installer   Build EFI/USB installer ISO"
	@echo "                         → out/installer/  (symlink: out/latest-installer)"
	@echo "  make run-qemu          Build QEMU image if missing, then boot with serial console"
	@echo "  make qemu-clean        Delete out/qemu-userdata.img only"
	@echo "  make repart-generate   Generate systemd-repart defs from partitions.yaml"
	@echo "  make disk-repart DISK=/dev/sdX   Apply layout (destructive)"
	@echo "  make disk-repart DISK=/dev/sdX DRY_RUN=1"
	@echo "  make grub-cfg          Install boot/grub.cfg into out/grub/grub.cfg"
	@echo "  make repart-test       Loopback systemd-repart smoke test (sudo)"
	@echo "  make update-test       Loopback A/B updater smoke test (sudo)"
	@echo "  make test              Unit tests (no root / no hypervisor)"
	@echo "  make lint              Ruff over control-plane/ scripts/ installer/"
	@echo "  make test-qemu         QEMU boot + HTTP API smoke (needs build-qemu)"
	@echo "  make test-gateway      Live 5xx error contract against a real nginx"
	@echo "  make test-update-unit  Alias for update unit tests only"
	@echo "  make refresh-image-pins  Rebase container image pins to current digests"
	@echo "                         (KEYS=valkey to limit; asks the registry, no pull)"
	@echo "  make check-image-pins    Report pins whose upstream digest has moved"
	@echo "  make check-untracked   Fail if untracked (non-ignored) files exist"
	@echo "  make nix-gc            Soft Nix GC (drop stale build roots; no -d)"
	@echo "  make clean             Remove out/"
	@echo "  make keys              Generate local ed25519 test keys under keys/"
	@echo "  make help              Show this help (default)"
	@echo ""
	@echo "Variables:"
	@echo "  VERSION   Override version (default: UTC date + .build-counter)"
	@echo "  KEYS      Pin keys for refresh-image-pins (default: all)"
	@echo "  SIGN=1    Write detached signatures (requires keys/; see make keys)"
	@echo "  DISK      Target block device for disk-repart"
	@echo "  NIX_GC_THRESHOLD=80  Pre-build GC if / or /nix is at least this % full"
	@echo "  ALLOW_UNTRACKED=1    Bypass check-untracked (flake omission risk)"
	@echo ""
	@echo "Notes:"
	@echo "  A/B helpers: lemonseed-ab / lemonseed-update (on appliance)"
	@echo "  make test-update-unit | make test (unit) | make test-qemu (HTTP boot)"
	@echo "  Flakes omit untracked files — check-untracked runs in test + build-*"
	@echo "  Installer UI: http://lemonseed.local/ (Avahi) on port 80"

build-production:
	SIGN="$(SIGN)" PROFILE=appliance "$(ROOT)/scripts/package.sh"

build-qemu:
	SIGN="$(SIGN)" PROFILE=appliance-qemu "$(ROOT)/scripts/package.sh"

build-installer:
	"$(ROOT)/scripts/package-installer.sh"

run-qemu:
	@if [[ ! -f "$(ROOT)/out/latest-qemu/vmlinuz" \
	   || ! -f "$(ROOT)/out/latest-qemu/root.squashfs" \
	   || ! -f "$(ROOT)/out/latest-qemu/initrd.img" ]]; then \
		echo "==> QEMU artifacts missing; running build-qemu"; \
		$(MAKE) build-qemu SIGN="$(SIGN)"; \
	else \
		echo "==> Using existing QEMU artifacts at out/latest-qemu"; \
	fi
	"$(ROOT)/scripts/run-qemu.sh" "$(ROOT)/out/latest-qemu"

qemu-clean:
	rm -f "$(ROOT)/out/qemu-userdata.img"
	@echo "==> Removed out/qemu-userdata.img (if it existed)"

repart-generate:
	"$(ROOT)/scripts/gen-repart.py" -o "$(ROOT)/out/repart.d"

disk-repart: repart-generate
	@if [[ -z "$(DISK)" ]]; then \
		echo "Set DISK=/dev/sdX (or DISK=/dev/nvme0n1)" >&2; \
		exit 2; \
	fi
	@if [[ "$(DRY_RUN)" = "1" ]]; then \
		"$(ROOT)/scripts/disk-repart.sh" --dry-run "$(DISK)"; \
	else \
		"$(ROOT)/scripts/disk-repart.sh" "$(DISK)"; \
	fi

grub-cfg:
	mkdir -p "$(ROOT)/out/grub"
	cp "$(ROOT)/boot/grub.cfg" "$(ROOT)/out/grub/grub.cfg"
	@echo "==> wrote $(ROOT)/out/grub/grub.cfg"

repart-test:
	sudo "$(ROOT)/scripts/repart-smoke-test.sh"

update-test:
	"$(ROOT)/scripts/update-smoke-test.sh"

test:
	"$(ROOT)/scripts/run-unit-tests.sh"

# Ruff comes from nixpkgs, not from the appliance: the control-plane interpreter
# on the box stays stdlib + psycopg2/requests/lemonade-sdk. Rules live in
# ruff.toml. Deliberately *not* wired into `make test`, which must stay offline
# and hypervisor-free.
lint:
	nix run nixpkgs#ruff -- check --config "$(ROOT)/ruff.toml" control-plane scripts installer

test-qemu:
	"$(ROOT)/scripts/qemu-api-smoke-test.sh"

# Live 5xx contract against a real nginx (no root, no hypervisor). Kept out of
# `make test`, which must stay offline: this needs an nginx from nixpkgs.
test-gateway:
	"$(ROOT)/scripts/gateway-error-probe.sh"

test-update-unit:
	python3 "$(ROOT)/scripts/test_lemonseed_update.py"

check-untracked:
	"$(ROOT)/scripts/check-untracked.sh"

# control-plane/container-images.json is the only image reference in the tree,
# and its digest is what a deployed box runs. Tags move (pg16 and 8-alpine both
# did in 2026-10), so rebase deliberately, right after make test / test-qemu
# validated the candidate — never implicitly via a pull on a customer's LAN.
refresh-image-pins:
	python3 "$(ROOT)/scripts/refresh-image-pins.py" $(KEYS)

# Needs the network, which is why it is not part of make test.
check-image-pins:
	python3 "$(ROOT)/scripts/refresh-image-pins.py" --check

nix-gc:
	"$(ROOT)/scripts/nix-gc.sh"

clean:
	rm -rf "$(ROOT)/out"
	"$(ROOT)/scripts/nix-gc.sh"

keys:
	"$(ROOT)/scripts/gen-keys.sh"
