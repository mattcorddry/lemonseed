# P7 — Build cycle time

**Status:** root cause found and fixed; follow-ups in `DESIGN.md` §11.
**Scope:** developer build performance only. No appliance runtime behavior changes.

## The report

`make build-production` takes far too long, and appeared to **recompile CUDA
llama.cpp from source on every run** — meaning unchanged artifacts were not being
reused. The maintainer also observed a reboot on a production box re-downloading
the chat model, which is a separate issue (`DESIGN.md` §11, already addressed).

## Measurement

All numbers below were taken on the development box on 2026-10-09 with read-only
commands (`--dry-run`, `nix path-info`, `nix derivation show`, `nix-store -q`,
`stat`, `df`). No `nix build` was run without `--dry-run`, so **no wall-clock
build times were measured** — that is the one honest gap in this record.

### Flow

`Makefile:70-71` `build-production` → `scripts/package.sh`:

- `:25` `VERSION="$("${ROOT}/scripts/version.sh")"` (bumps `.build-counter`)
- `:28` `scripts/nix-gc.sh --if-needed`
- `:32` `PACKAGE=".#artifacts"` → `:52-55` `APPLIANCE_VERSION="$VERSION" nix build --impure --out-link "$BUILD_DIR/artifacts" "$PACKAGE"`
- `:57-59` `install` of `vmlinuz` / `root.squashfs` / `initrd.img` into `out/appliance-<version>/`
- `:102` `scripts/nix-gc.sh --keep "$BUILD_DIR"` ← **unconditional, after every build**

### Findings

1. **Disk is not the problem.** `/` 26 % used, `/nix` 49 %, `/nix/store` = **12 GB**,
   1.7 TB free. `scripts/nix-gc.sh --if-needed` literally reports
   `/ at 26%, /nix at 49% (threshold 80%); skip`, so the pre-build guard at
   `package.sh:28` never does anything.
   (The real space hog is `out/`: **177 GB across 84 `appliance-*` dirs**, plus a
   34,359,738,368-byte `out/qemu-userdata.img` — and `/home` and `/nix` are the
   same device, so they compete for one pool.)

2. **The version string does *not* invalidate the GPU stack.** With only
   `APPLIANCE_VERSION` differing (`0.0.0-dev` vs `20261009.07`), the CUDA and
   npm-deps derivation paths are **byte-identical**
   (`am54zw2vcqnr26d3ilwc59lirgfwi09b-llama-cpp-10236.drv`,
   `7fqpqq122h7yks393rhcvjdwc792bj7g-llama-cpp-10236-npm-deps.drv`), and
   `grep -c "20261009"` on the CUDA `.drv` → **0**. `llamaCppCuda` is version-pure.

   The version *does* invalidate the image, and unavoidably so: `metadata.nix`
   puts it in a `writeText` consumed by `systemd.tmpfiles.rules` for
   `/run/appliance/version` **inside the toplevel**, and
   `build-root-squashfs.nix:20` takes `toplevel` as an input. Measured cost per
   bump: a production image of **3,195,133,952 bytes** compressed, versus
   **876,077,056 bytes** for the CPU-only QEMU image — so roughly 2.3 GB of
   `zstd -Xcompression-level 10` output per build.

3. **CUDA was not being substituted, and its output was gone.** The plan lists 12
   CUDA/llama entries under *will be built* (`cuda_nvcc`, `cuda_cudart`,
   `cuda_cccl`, `libcublas`, `llama-cpp-10236`, `llama-cpp-10236-npm-deps`), and
   `nix log` on the CUDA drv reports a **local** build log — it really was
   compiled here. But the output path is not realized:

   ```
   $ nix path-info /nix/store/j8mgqgslrs909051d6fkylcy0sddxvgw-llama-cpp-10236
   don't know how to build these paths:
     /nix/store/j8mgqgslrs909051d6fkylcy0sddxvgw-llama-cpp-10236
   ```

   while its `.drv` is still in the store, and `nix-store -q --referrers` on the
   output returns **nothing**. The `llama-cpp-10273` variants from `nix-amd-ai`
   *are* substitutable (one CPU output is realized; its drv has no local build
   log). `cutensor` and `cudnn` do **not** appear in the closure at all.

4. **The build root did not pin its own closure.** `nixos/modules/artifacts.nix`
   built `appliance-artifacts-<version>` containing exactly three symlinks plus a
   version file — 3 refs. The production image reports **113** references and
   **none** of them the toplevel:

   ```
   $ nix-store -q --references .../root-20261009.06.squashfs | grep -c nixos-system
   0
   ```

   The QEMU image happens to report **1** such reference — the same
   `build-root-squashfs.nix` for both profiles, different outcome, which confirms
   this is luck rather than design: nix discovers references by scanning output
   bytes for store-path hashes, and everything inside a zstd image is
   unscannable. The last production build's own artifacts drv contains **zero**
   occurrences of `nixos-system`.

5. **No cache, and no parallelism.** `nix show-config`:
   `substituters = https://cache.nixos.org/` only, `trusted-substituters` empty,
   and `flake.nix` has no `nixConfig`. Also `max-jobs = 1` while `nproc` is 16, so
   the ~108-derivation plan is serialized.

## Root cause

Nix cannot see through the SquashFS, so the image output advertises references that
do not include the system closure. `appliance-artifacts` therefore rooted three
files and nothing else, and **`scripts/package.sh:102` runs a soft garbage
collection after every build** — keeping only the current build's roots. With
nothing referencing the toplevel, `nix-collect-garbage` treats the freshly built
system closure (CUDA, `cuda_nvcc`, `libcublas`, kernel modules, …) as garbage. The
`.drv` survives, the output does not, and the next build has to compile the GPU
stack from source again. That is the entire reported symptom.

Compounding but independent: `--impure` (`package.sh:52-55`) makes `self` the
**dirty git tree** (`9b594d1…-dirty`, 37 changed paths), so any tracked-file edit —
including `proposals/*.md` — changes the toplevel and the image. Measurements taken
on a dirty tree are not repeatable, and this invalidates independently of the
version string.

## Fix

`nixos/modules/artifacts.nix` — the artifacts output now writes the toplevel path
into `$out/closure-root`, so the build root transitively pins the system closure.

Why a text file: nix's reference scanner *does* see symlink targets and store
paths in file contents. Verified with a throwaway probe under the gitignored
`out/`:

```
pkgs.runCommand "refprobe" { } ''
  mkdir -p "$out"
  ln -s ${pkgs.hello} "$out/hello-link"
  printf '%s\n' ${pkgs.hello} > "$out/ref.txt"
''
$ nix-store -q --references <result>
/nix/store/wzr035k31pmpn2caabq8qwv1npg571z9-hello-2.12.3
```

and the modified artifacts drv now embeds the toplevel
(`nix derivation show --impure .#artifacts | grep -o 'nixos-system-lemonseed-[a-z0-9.]*'`
→ both the path and its `.drv`), where the shipped drv had zero.

`closure-root` is **not** an artifact: `package.sh` installs only the three named
files, `manifest.json` describes only those three, and nothing copies the nix
output directory wholesale.

**Expectation, stated honestly:** the *next* build after this change is still
expensive — the CUDA outputs are already gone and must be rebuilt once. The claim
is that the build *after that* no longer lists them, because the closure now has a
root. Proving that requires two timed `make build-production` runs, which was out
of scope for a measurement-only pass.

## Follow-ups (not done here)

Ranked, with the reason each was left out:

1. **Stop embedding the version in the hashed toplevel.** Removes a guaranteed
   ~3.2 GB recompression per build. Needs a decision on how
   `/run/appliance/version` is delivered out-of-band, so it needs its own design
   rather than a build-phase hack.
2. **Prune `out/`.** 177 GB / 84 dirs plus a 32 GiB QEMU disk image, on the same
   filesystem as `/nix`. Deleting previous releases is a user-visible behavior
   change, so it wants its own flag and retention rule.
3. **Retarget `scripts/nix-gc.sh`.** Split "always prune stale `out/.build/*`
   roots" from "collect the store only under space pressure"; today
   `--if-needed` is dead code at 49 %, while the unconditional post-build call is
   precisely what ate the CUDA closure. With the fix in place the unconditional
   call is now safe, but the two concerns should still be separate.
4. **`max-jobs`.** `nix show-config` reports 1 against 16 cores. This is a
   machine-level `nix.conf` choice, not a project file; noted for the developer
   setup docs rather than changed here.
5. **Substituters.** Only `cache.nixos.org` is configured. Whether `nix-amd-ai`
   publishes a usable public cache was **not verified**, and in any case it can
   never cover `llamaCppCuda`, which is defined in-tree in
   `nixos/modules/lemond.nix` — pinning locally is the only protection that works
   for it.
