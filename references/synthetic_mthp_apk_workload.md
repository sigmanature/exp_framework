# Synthetic mTHP Android APK Workload

Use this when Cuttlefish cannot run enough real third-party x86_64 apps and the experiment needs controlled ART/Scudo/dlopen/VMA/fork-COW pressure.

## Build

Run from the AOSP root, without invoking Soong:

```bash
AOSP_ROOT=$PWD \
  ~/.agents/skills/exp_framework/scripts/build_mthp_synth_apks.py \
  --out-dir .worklog/synthetic-mthp-apk/out-$(date +%Y%m%d-%H%M%S)-final \
  --max-pads 64 \
  --pad-rodata-kb 256 \
  --pad-data-kb 64
```

Required host inputs:

- `~/android-sdk/ndk/android-ndk-r27d`
- AOSP host tools: `aapt2`, `d8`, `apksigner`, `zipalign`, `javac`, `jar`
- AOSP `prebuilts/sdk/current/public/android.jar`
- AOSP test signing key under `build/make/target/product/security/`

## Install

Install with `--no-incremental` so adb does not first try incremental install and emit misleading parse failures:

```bash
OUT=.worklog/synthetic-mthp-apk/out-YYYYmmdd-HHMMSS-final
for apk in "$OUT"/apks/*.apk; do
  adb -s 127.0.0.1:16521 install --no-incremental -r -g "$apk" </dev/null || break
done
```

For A/B CVD, run the same loop against both profile serials. In `while read` loops, every nested `adb` must use `</dev/null` so it cannot consume the manifest stream.

For the maintained A/B installer, prefer the bundled idempotent script. It disables package verification, installs all synthetic APKs with `--no-incremental -r -g`, writes success/fail/skip TSVs, and verifies the final `com.zzhao.mthp.synth` package count on each profile. The current full matrix has 60 APKs; p00/p14/p21 remain the stable smoke anchors from the original 24-APK matrix.

The builder writes a sampler-compatible `packages.txt` next to `profiles.tsv`; pass it to `run_memstress_and_collect_logs.py --package-file` for Cuttlefish long runs.

## Runtime Pressure Scaling

Build and install the full 60-APK profile once. After that, scale-only retuning should be done at launch time, not by rebuilding APKs. The sampler passes the runtime values to the synthetic APK through `am start --ef ...`, and the app applies them before creating anonymous VMAs or fork-COW children.

For the first lower-pressure profile, keep anonymous VMA count and embedded `.so` pressure stable while reducing anonymous/COW pressure and shrinking explicit filemap length:

```bash
python3 ~/.agents/skills/exp_framework/scripts/run_memstress_and_collect_logs.py \
  --serial <SERIAL> \
  --out-dir <OUT_DIR> \
  --package-file /home/nzzhao/learn_os/android17/.worklog/synthetic-mthp-apk/out-YYYYmmdd-HHMMSS-full/packages.txt \
  --burst-size 4 \
  --hold-ms 15 \
  --launch-gap-ms 15 \
  --cycle-sleep-ms 1000 \
  --seed 20260617 \
  --synthetic-vma-count-scale 1.0 \
  --synthetic-anon-vma-size-scale 0.5 \
  --synthetic-cow-pages-scale 0.5 \
  --synthetic-filemap-size-scale 0.65 \
  --synthetic-dlopen-lib-count-scale 1.0 \
  --no-use-su \
  --no-network-check
```

For the A/B runner, use environment variables:

```bash
SYNTHETIC_ANON_VMA_SIZE_SCALE=0.5 \
SYNTHETIC_COW_PAGES_SCALE=0.5 \
SYNTHETIC_FILEMAP_SIZE_SCALE=0.65 \
RUN_ROOT=$PWD/.worklog/thp-ab-runs/defrag-always-60-anonhalf-cow60-$(date +%Y%m%d-%H%M%S) \
WORKLOAD_PACKAGE_FILE=<FULL_APK_OUT>/packages.txt \
WORKLOAD_APK_OUT=<FULL_APK_OUT> \
WORKLOAD_EXPECTED_COUNT=60 \
THP_DEFRAG_MODE=always \
MAX_CYCLES=120 \
.worklog/scripts/thp_ab_4k16k_runner.sh start
```

Expected aggregate runtime profile changes versus the full 60-APK profile:

- anonymous VMA count unchanged;
- anonymous VMA size / full-fault pages about half, rounded to 16KB granularity;
- COW pages about half; exact ratio can be lower if small VMAs hit the 16KB minimum and cap `cow_pages_per_child`;
- explicit filemap size about 65%, while filemap thread/VMA count is unchanged;
- embedded `.so` size/count unchanged.

If evidence shows the fixed startup/VMA/dlopen component still dominates, add the second-stage runtime knobs without rebuilding APKs:

```bash
SYNTHETIC_VMA_COUNT_SCALE=0.5 \
SYNTHETIC_ANON_VMA_SIZE_SCALE=0.1 \
SYNTHETIC_COW_PAGES_SCALE=0.1 \
SYNTHETIC_FILEMAP_SIZE_SCALE=0.3 \
SYNTHETIC_DLOPEN_LIB_COUNT_SCALE=0.5 \
RUN_CELLS=B-all4K \
RUN_ROOT=$PWD/.worklog/thp-ab-runs/pilot-B-all4K-runtime-scale-count-dlopen-$(date +%Y%m%d-%H%M%S) \
WORKLOAD_PACKAGE_FILE=<FULL_APK_OUT>/packages.txt \
WORKLOAD_APK_OUT=<FULL_APK_OUT> \
WORKLOAD_EXPECTED_COUNT=60 \
THP_DEFRAG_MODE=always \
MAX_CYCLES=120 \
.worklog/scripts/thp_ab_4k16k_runner.sh start
```

This should lower logcat `regions=` and `dlopen_ok=` markers. Keep package count, burst, hold, launch gap, cycle sleep, and seed fixed unless the experiment is explicitly about orchestration cost.

Verify runtime scaling with `run_manifest.json` and logcat:

```bash
grep -n 'synthetic_.*_scale' <OUT_DIR>/run_manifest.json
adb -s <SERIAL> logcat -d -v threadtime -s ZZMthpSynth ZZMthpSynthNative | grep runtime_scales
```

## Build-Time Pressure Scaling

Build-time scaling remains available for offline profile inspection, but it is no longer the preferred way to retune long runs because every value change requires rebuilding and reinstalling APKs:

```bash
AOSP_ROOT=$PWD \
  ~/.agents/skills/exp_framework/scripts/build_mthp_synth_apks.py \
  --out-dir .worklog/synthetic-mthp-apk/out-$(date +%Y%m%d-%H%M%S)-anonhalf-cow60 \
  --max-pads 64 \
  --pad-rodata-kb 256 \
  --pad-data-kb 64 \
  --anon-vma-size-scale 0.5 \
  --cow-pages-scale 0.6
```

Current workload semantics are intentionally simple and resident-pressure oriented:

- anonymous synthetic VMAs are fully write-faulted at startup, one write per guest page;
- embedded pad `.so` libraries are fully read-faulted through `mthp_pad_touch(page_size, 0)`, so the workload models normal library/code reads and does not dirty `.so` data;
- filemap workers map files `PROT_READ` and read every page;
- `parent_touch_pages` in manifests is a compatibility alias for the main process full anonymous page count; use `anon_fault_mode`, `anon_full_fault_pages`, and `anon_full_fault_mb` for new analysis.

For VMA-alignment A/B runs, inspect `vmstat_derived.csv` deltas for `anon_mthp_vma_unsuitable_order2` and `cow_mthp_vma_unsuitable_order2`. These counters isolate order-2 mTHP opportunities that are enabled by THP policy but rejected by VMA suitability/aligned-window checks, so pristine 4KB-aligned userspace should generally report more hits than the 16KB-aligned B image when `all16K` is enabled.

Do not pass only `--package-file` for synthetic long runs. The script's built-in defaults are lower pressure (`burst_size=1`, `hold_ms=200`, `launch_gap_ms=350`). Preserve the skill/default-manifest pressure profile explicitly:

```bash
python3 ~/.agents/skills/exp_framework/scripts/run_memstress_and_collect_logs.py \
  --serial <SERIAL> \
  --out-dir <OUT_DIR> \
  --max-cycles 120 \
  --interval-s 60 \
  --vmstat-interval-s 10 \
  --buddyinfo-interval-s 0 \
  --package-file /home/nzzhao/learn_os/android17/.worklog/synthetic-mthp-apk/out-YYYYmmdd-HHMMSS-60/packages.txt \
  --burst-size 4 \
  --hold-ms 15 \
  --launch-gap-ms 15 \
  --cycle-sleep-ms 1000 \
  --seed 20260617 \
  --no-use-su \
  --no-network-check
```

```bash
APK_OUT=/home/nzzhao/learn_os/android17/.worklog/synthetic-mthp-apk/out-YYYYmmdd-HHMMSS-final \
  ~/.agents/skills/exp_framework/scripts/install_mthp_synth_apks_ab.sh run
```

## Validation Profiles

- `p00_java_s`: light smoke. Expected `regions=800`, `anon_pages_written=6400`, `dlopen_ok=4`, `mthp_vma=800`.
- `p14_cow_l`: COW smoke. Expected `regions=6000`, `anon_pages_written=24000`, `dlopen_ok=4`, `fork_round=1 children=4 cow_pages_target=65536`.
- `p21_monster_multiproc`: mixed heavy profile. Expected one main process and three `:wN` services; main `mthp_vma=6000`, workers around `mthp_vma=2000`, `dlopen_ok=64`, and fork-round logs.

Useful checks:

```bash
adb -s <SERIAL> shell 'cmd package list packages com.zzhao.mthp.synth | wc -l'
adb -s <SERIAL> logcat -d -v threadtime -s ZZMthpSynth ZZMthpSynthNative AndroidRuntime linker
adb -s <SERIAL> shell 'ps -A -o PID,NAME | grep com.zzhao.mthp.synth.p21'
adb -s <SERIAL> shell "su 0 sh -c 'grep -c mthp_vma /proc/<PID>/maps; grep -c base.apk /proc/<PID>/maps'"
```

## Pitfalls

| Symptom | Cause | Fix |
|---|---|---|
| `dlopen failed ... .dynamic section header was not found` | `extractNativeLibs=true` lets package install extract/strip native libs; section headers can be zeroed and Android linker rejects the result. | Keep native libs embedded: `android:extractNativeLibs="false"`, store `.so` entries uncompressed, and run `zipalign -P 16 4`. |
| `libc++_shared.so not found` | JNI library was linked against shared libc++ but the APK did not package it. | Link `libmthpwork.so` with `-static-libstdc++`. |
| pad `dlopen` fails under embedded libs | Code uses `ApplicationInfo.nativeLibraryDir`, which points at an extraction directory that does not contain embedded libs. | Pass `sourceDir + "!/lib/" + Build.SUPPORTED_ABIS[0]` to native and `dlopen()` that zip path. |
| p21 main process Java `OutOfMemoryError` | Requested Java live heap exceeds default CVD app heap growth limit. | Cap Java live bytes to `Runtime.maxMemory() * 3 / 4` and downgrade OOM to a workload-thread stop. |
| maps collection sees only first process | Nested `adb` consumes the `while read` input stream. | Add `</dev/null` to every `adb` call inside the loop. |
