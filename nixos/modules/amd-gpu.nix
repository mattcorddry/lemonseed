# Host AMD GPU stack for M6.4 (Vulkan).
#
# The kernel `amdgpu` module ships in the base kernel (no module needed here).
# This module only adds the Vulkan userspace so lemond's Vulkan llama.cpp
# backend can see the GPU. No display server is started.
#
# AMD's standalone Vulkan driver (AMDVLK) was removed from nixpkgs in favour of
# Mesa's RADV, which is the default Vulkan ICD for AMD GPUs. `hardware.graphics`
# therefore defaults to `pkgs.mesa` (RADV) — that is the ICD we rely on.
#
# ROCm/HIP and the HRX backend are intentionally NOT pulled in here (backlog:
# "AMD GPU driver optimization"). Vulkan is the broad, lightweight default that
# covers AMD dGPUs (RDNA2/3/4) and Strix iGPUs with a single driver.

{ config, lib, pkgs, ... }:

let
  cfg = config.hardware.amd-gpu;

  amdgpuKernelParams = pkgs.writeShellApplication {
    name = "lemonseed-amdgpu-kernel-params";
    runtimeInputs = with pkgs; [
      coreutils      # cat, mkdir, dirname
      gnugrep        # grep (iGPU detection)
      util-linux     # mount, umount, mountpoint
      systemd        # systemctl reboot
      vulkan-tools   # vulkaninfo
      lshw           # read physical memory from DMI/SMBIOS
      jq             # parse lshw JSON
    ];
    text = builtins.readFile ../../scripts/amdgpu-kernel-params.sh;
  };
in
{
  options.hardware.amd-gpu = {
    enable = lib.mkEnableOption "AMD GPU Vulkan stack (lemond Vulkan llama.cpp backend)";

    # Manual override for APU iGPU shared memory (GB). When set, the
    # amdgpu-kernel-params service uses this value instead of auto-calculating
    # from physical DIMM size. Leave null for auto-detection (total - 16GB).
    sharedMemoryGb = lib.mkOption {
      type = lib.types.nullOr lib.types.int;
      default = null;
      example = 48;
      description = ''
        Manual override: gigabytes of system memory to allocate to the iGPU.
        When null (default), the amdgpu-kernel-params service auto-calculates
        (physical RAM - 16GB) on first boot and reboots to apply.
      '';
    };
  };

  config = lib.mkIf cfg.enable {
    # Vulkan ICD (Mesa/RADV) + loader. Headless compute — no X/Wayland.
    hardware.graphics.enable = true;
    hardware.graphics.enable32Bit = false;
    # vulkaninfo for bring-up and /api/hardware debugging (lists Vulkan devices).
    environment.systemPackages = [ pkgs.vulkan-tools ];

    # The amdgpu kernel module requires firmware blobs (PSP, GC, SDMA, VCN,
    # DMCUB, …) to initialize the GPU. Without these the module fails with
    # "Fatal error during GPU init" and no /dev/dri/renderD* is created,
    # leaving Vulkan with only the llvmpipe CPU fallback.
    hardware.firmware = [ pkgs.linux-firmware ];

    # Only load GPU modules when the hardware is present (bare-metal review:
    # the Strix Halo box booted without amdgpu and spent boot probing NVIDIA
    # instead). udev's built-in modalias coldplug missed the device on an
    # updated image (simple-framebuffer kept the display, Vulkan fell back to
    # llvmpipe) — a manual `modprobe amdgpu` bound the device instantly, so the
    # module and params are fine and only the trigger was missing. Fire the
    # load explicitly: PCI class 03 (display/3D) with vendor 1002 = AMD.
    services.udev.extraRules = ''
      ACTION=="add", SUBSYSTEM=="pci", ENV{MODALIAS}=="pci:v00001002d*bc03sc*i*", RUN+="${pkgs.kmod}/bin/modprobe amdgpu"
    '';

    # APU iGPU shared memory split: the amdgpu-kernel-params service reads
    # physical memory from DMI (via lshw), calculates ttm.pages_limit /
    # page_pool_size, and writes them to the ESP (/grub/kernel-params).
    # GRUB includes that file in the kernel command line.
    # First boot: no file → default split → service calculates → writes →
    # reboots. Subsequent boots: correct split from the start.
    #
    # If sharedMemoryGb is set manually, the service uses that value instead
    # of auto-calculating (still writes to the ESP for persistence).
    systemd.services.lemonseed-amdgpu-kernel-params = {
      description = "Lemonseed AMD iGPU shared memory kernel params";
      wantedBy = [ "multi-user.target" ];
      # Run after lock-in so the new slot is already boot_default. If we
      # reboot to apply kernel params, we come back to the new slot (not
      # the old one).
      after = [ "local-fs.target" "lemonseed-ab-lock-in.service" ];
      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
        # Only the appliance service reboots to apply new params; the
        # installer runs the same script without this and just writes the ESP.
        Environment = [ "LEMONSEED_APPLY_REBOOT=1" ];
        ExecStart = "${amdgpuKernelParams}/bin/lemonseed-amdgpu-kernel-params";
        # The service may reboot the system; don't treat that as a failure.
        SuccessExitStatus = [ "0" "1" ];
      };
      environment = lib.optionalAttrs (cfg.sharedMemoryGb != null) {
        LEMONSEED_AMDGPU_SHARED_MEM_GB = toString cfg.sharedMemoryGb;
      };
    };

    # lemond already has video+render groups (see lemond.nix); the Vulkan ICD
    # reads /dev/dri/* through the `render` group.
  };
}
