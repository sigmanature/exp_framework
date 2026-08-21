---
name: android-thp-fallback-sampler
description: automate long-running sampling of android anon 16KB large folio fallback stats via adb; run memstress workload and output raw/derived csv + summary.
---

# Android THP 16KB Anon Fallback Sampler

> **最短复现入口**: 仓库根目录 [`README.md`](../README.md)
> **默认配置模板**: [`config/default_memstress_manifest.json`](../config/default_memstress_manifest.json)

本 skill 只保留一个核心脚本：
- `scripts/run_memstress_and_collect_logs.py`：在已 root 的 Android 设备上运行 memstress，并周期性采样 THP 16KB/32KB/64KB stats，输出 `raw_samples.csv` / `derived.csv` / `summary.md`。

## 什么时候用

- 需要长时间运行一个可控的 app 启停负载，并同时采样 anon large folio 的 fallback 比率。
- 希望复现实验：同样的 manifest + seed 可以跑出相同的包启动顺序。

## 快速开始

见 [`README.md`](../README.md)。最短命令：

```bash
python3 -m pip install -r requirements.txt

python3 scripts/run_memstress_and_collect_logs.py \
  --serial <YOUR_DEVICE_SERIAL> \
  --from-manifest config/default_memstress_manifest.json
```

## 文件说明

- `config/default_memstress_manifest.json`：默认 memstress + THP stats 采样配置模板，已固定 seed / max_cycles / interval_s。
- `scripts/run_memstress_and_collect_logs.py`：主脚本。
- `scripts/derive_metrics.py`：运行结束后由主脚本调用，生成 `derived.csv` 和 `summary.md`。
- `scripts/compute_synthetic_workload_expectations.py`：根据 synthetic `profiles.tsv`、`run_manifest.json` 和可选 `memstress/cycle_log.jsonl`，生成每个 app 的预期 VMA/COW/dlopen/filemap/Scudo/Java 页数，以及 launch-weighted 组件比例。
- `scripts/sample_anon_vma_sizes.py`：从 rooted Android 设备批量采集三方进程 `/proc/<pid>/smaps`，按匿名 VMA kind/process 输出 size/RSS/swap 分位，用来校准 synthetic APK 的 VMA 尺寸和触页强度。
- `scripts/order0_fragment_sampler.py`：与负载并行的独立采样进程，按挂钟周期（`--interval-s`，默认 10s）通过 adb 采集 `/proc/vmstat` + `/proc/zoneinfo` + `/proc/pagetypeinfo` + 关键 sysctl/THP 设置，输出 `samples.jsonl`（原始记录，vmstat 全量）、`order0_vmstat_samples.csv`（仅导出白名单命中的计数器）、`fragmentation_samples.csv`（按 zone × migrate type 的 unsuitable free / fragmentation_score / PCP order-0）。输入：`--adb`、`--serial`、`--out-dir`、`--interval-s`、`--target-order`（默认 2）以及 `--expected-*`（期望的 uffd_mfill_order2 / mthp_cow_order2 / kfragd_enabled / compact_order2_alloc_wake / kswapd_order2_threshold / kswapd_order2_wakeup_threshold，写入 `sampler_manifest.json` 供事后核对实验开关是否生效）。CSV 导出白名单可外部驱动：`--vmstat-key-patterns`（可多次、可逗号分隔）或环境变量 `VMSTAT_KEY_PATTERNS`，`--vmstat-keys-mode append|replace`（默认 append）控制是否保留内置默认白名单；规则语法为 `foo*`=前缀通配、无 `*`=子串匹配。采样器启动时向 stderr 打印最终生效的白名单来源（default/env/cli）和首次采样匹配到的 key 数。附带 `--device-probe deploy|start|watch|stop|pull|verify` 子命令编排设备端 20ms 探针（frag20ms），用于 kfragd 细粒度观测。退出时生成 `vmstat_delta.json` 及 `order0_source_delta.tsv` / `pcp_order0_delta.tsv` / `stall_compaction_delta.tsv`。
- `scripts/utils/`：主脚本依赖的公共模块（adb/su、设备准备、采样、包解析、崩溃检测等）。
- `references/`：与 adb、memstress 策略、包选择、内核补丁相关的参考文档。

## PARAMS.md 表驱动 vmstat 导出白名单（文档约定，仅映射说明）

长测工具链（experiment-standard）中，PARAMS.md 参数表（六列：参数|值|域|自检|责任脚本|状态）是唯一手改点。当用户要求"新增一个内核计数器进 CSV 导出"时，约定如下，**不要求改采样器源码**：

1. **PARAMS.md 加一行**：参数=新计数器名（如 `pgdemote_kswapd`），值=`1`，**域=`guest procfs / counter`**（表示该行是一个内核 /proc/vmstat 计数器的开关标记），自检=`grep pgdemote_kswapd /proc/vmstat`，责任脚本=采样器调用方的长测薄壳，状态=`ok`。
2. **长测薄壳收集 patterns**：长测薄壳（或调用方包装脚本）扫描 PARAMS.md 中"域=guest procfs / counter"的行，把每行的参数名列收集成 `--vmstat-key-patterns` 列表传给 `order0_fragment_sampler.py`：
   - 计数器名精确已知（如 `pgdemote_kswapd`）→ 传 `pgdemote_kswapd`（无星号 = 子串匹配，也覆盖同名前缀族）；
   - 想按族导出（如所有 `pgdemote_` 开头计数）→ 传 `pgdemote_*`（前缀通配）。
3. **采样器侧默认 append**：不传 `--vmstat-keys-mode` 时，用户 patterns 与内置默认白名单（order0 子串 + alloc_stall/alloc_fail/compact_stall/compact_success/pgscan_/pgsteal_/pgoutrun_/pgwake_/kswapd_order2_ 前缀）取并集，旧 CSV 列不受影响；需要精确控制列时用 `--vmstat-keys-mode replace`。
4. **诊断**：采样器启动 stderr 打印 `vmstat whitelist source=cli|env|default mode=... patterns=[...]` 与首次采样匹配 key 数；实验结束后可在运行日志中核对 patterns 是否生效、新计数器是否出现在 `order0_vmstat_samples.csv` 表头。

此约定只涉及长测薄壳与采样器的传参契约；experiment-standard 本身与采样器源码都不需要为新计数器改动。

## 核心指标

重点看 `derived.csv` 里的：

```
fallback_ratio = Δanon_fault_fallback / (Δanon_fault_alloc + Δanon_fault_fallback)
```

含义：
- `anon_fault_alloc`：anon 64K folio 分配成功次数。
- `anon_fault_fallback`：anon 64K folio 分配失败回退次数。

计数器是累计单调值，比率必须用相邻采样窗口的 Δ 计算。

当前 COW/order-2 细分也在 `vmstat_raw.csv` / `vmstat_derived.csv` 中采集：
- `cow_mthp_order2`：COW 路径成功复制并安装 order-2 folio。
- `cow_mthp_fallback_order0`：COW 尝试 order-2 后回退并完成 order-0 COW。
- `anon_mthp_vma_unsuitable_order2`：anon fault 中 order-2 被 THP 策略允许、但 VMA suitability/对齐窗口不满足。
- `cow_mthp_vma_unsuitable_order2`：COW 路径中 order-2 被 THP 策略允许、但 VMA suitability/对齐窗口不满足。

## 常见坑

- **设备需要 root**：读取 `/sys/kernel/mm/transparent_hugepage/.../stats` 需要 root。默认用 `su -c`；如果已经 `adb root`，传 `--no-use-su`。
- **stats 目录已写死**：固定采样 `/sys/kernel/mm/transparent_hugepage/hugepages-16kB/stats`，manifest 与 CLI 均无 `stats_dir` 概念。
- **计数器是累计值**：用 `derived.csv` 的 Δ，不要直接对 `raw_samples.csv` 算比率。
- **adb 偶发断开**：采样失败会记录到 `raw_samples.csv` 的 `error` 字段并继续。
- **packages 未安装**：脚本会自动过滤，只启动已安装的包。
- **manifest 里的 seed 固定**：默认 `20260617`；换 seed 会得到不同的包启动顺序，但同一 seed 可复现。

## Synthetic APK Workload

当 Cuttlefish 上真实第三方 x86_64 app 不足、无法稳定制造 ART/Scudo/dlopen/VMA/fork-COW 压力时，先使用 synthetic APK 工作负载，而不是继续扩大不可运行的真实 APK 集合。

入口：

```bash
AOSP_ROOT=$PWD scripts/build_mthp_synth_apks.py \
  --out-dir .worklog/synthetic-mthp-apk/out-$(date +%Y%m%d-%H%M%S)-final \
  --max-pads 64 --pad-rodata-kb 256 --pad-data-kb 64
```

详细构建、安装、验收 profile 和 Android linker/package-manager 坑位见 `references/synthetic_mthp_apk_workload.md`。关键验收 profile：

- 当前 synthetic 语义是简单 full-fault：anonymous VMA 启动后逐页写 fault；pad `.so` 和 filemap 逐页只读 fault；COW 仍由 `cow_pages_per_child` 控制。
- synthetic 压力降档优先用运行时参数，不重打 APK：先用 `--synthetic-anon-vma-size-scale` / `--synthetic-cow-pages-scale` / `--synthetic-filemap-size-scale`；若证据显示固定启动成本、VMA 数量或 `.so`/dlopen 主导，再加 `--synthetic-vma-count-scale` / `--synthetic-dlopen-lib-count-scale`。
- 每个 synthetic cell 必须输出 workload expectation：`synthetic_workload_expectations.tsv/json/md`；如果存在 `memstress/cycle_log.jsonl`，还必须输出 `synthetic_workload_launch_weighted.tsv`，用实际 launch count 加权每个 app 的预期压力。
- 每个 synthetic cell 结束后应采集 `/proc/<pid>/maps` 中的 `[anon:mthp_vma_*]`，输出 `synthetic_vma_maps.tsv` 和 `synthetic_vma_maps_summary.txt`，用于直接证明手动 mmap VMA 的起点/长度 16KB 对齐比例。
- Scudo/Java heap 快照使用 `scripts/scudo_java_snapshot_ab.sh`：默认挑 20 个 synthetic 包，运行时强制 `process_count=1`、`scudo_threads=1`，覆盖每个 app `scudo_live_mb=50`、`java_live_mb=50`，并用 `java_churn_ms=0` 表示 Java heap 一次性填满后停止；需要更轻量时用 `SCUDO_MB=30 JAVA_MB=30` 覆盖。运行前必须确认 APK_OUT 指向包含 `java fill_once` 的产物，并用 host/device APK sha256 或 logcat `java fill_once` 自证不是旧 APK。
- Scudo 和 Java heap 必须分开汇总：Scudo 看 smaps `[anon:scudo*]`，Java/ART heap 看 `[anon:dalvik*]`；`Size` 是虚拟映射大小，`Rss/Pss/Private_Dirty/Swap` 才是实际驻留/换出量。快照脚本应输出多次 smaps 的 `snapshot_stability.tsv`；如果 RSS/PSS 未稳定，不要把单次采样当成稳定占用。
- `p00_java_s`：轻量 smoke，期望 `regions=800`、`anon_pages_written=6400`、`dlopen_ok=4`、`mthp_vma=800`。
- `p14_cow_l`：COW smoke，期望 `regions=6000`、`anon_pages_written=24000`、`fork_round=1 children=4 cow_pages_target=65536`。
- `p21_monster_multiproc`：重型多进程，期望主进程 `mthp_vma=6000`，三个 worker 各约 `mthp_vma=2000`，`dlopen_ok=64`。

## Workflow Contract

### Main Workflow
1. 准备设备：确保 adb 连接、已 root、THP 16KB `enabled` / `stats` 路径存在，并已安装 manifest 中的目标包。
2. 预检：先读取 `/sys/kernel/mm/transparent_hugepage/hugepages-16kB/enabled` 和对应 `stats` 目录，确认实验适用于当前内核。
3. 运行：用默认 manifest 执行 `run_memstress_and_collect_logs.py`。
4. 等待运行结束（或按 Ctrl-C 停止）。
5. 验证：检查 `derived.csv` 的 `fallback_ratio` 列、`summary.md`、以及运行后 `enabled` 状态是否与预期一致。
6. 报告：输出 `summary.md`、关键比率趋势、以及 `run_manifest.json`。

### Decision Table
| Phase | Trigger / Symptom | Action | Verify | On Failure | Workflow Effect |
|---|---|---|---|---|---|
| Preflight | `/sys/kernel/mm/transparent_hugepage/hugepages-16kB/enabled` or `/stats` missing | Stop and report that the kernel build does not expose the required 16KB THP interface | `cat .../enabled` and `ls .../stats` both succeed | Do not run the experiment | block |
| Baseline run | User wants a 4 KB baseline with 16KB THP disabled | Set `hugepages-16kB/enabled` to `none` before the run; the main script never touches the THP mode | Post-run `cat .../enabled` still shows `[none]` | Treat the baseline as invalid and rerun | branch |
| THP run | User wants a 16KB THP-enabled run | Set `hugepages-16kB/enabled` to `always` before the run; the main script never touches the THP mode | Post-run `cat .../enabled` shows `[always]` | Treat the run as invalid and rerun | branch |
| Reproducibility | Manifest packages are skipped because they are not installed | Report skipped packages and downgrade the run from exact reproduction to local smoke reproduction | `run_manifest.json` and `packages_resolved` show the actual package set | Share/install the missing APK builds before rerunning | continue |

### Decision Table
| Phase | Trigger / Symptom | Action | Verify | On Failure | Workflow Effect |
|---|---|---|---|---|---|
| Package launch resolution | Cuttlefish packages are installed, but `run_memstress_and_collect_logs.py` fails with `RuntimeError: no launchable activities found` | Resolve launcher components through `dumpsys package <pkg>` Activity Resolver Table when `pm resolve-activity` / `cmd package resolve-activity` return no entry | `resolve_activity(serial, pkg)` returns components such as `com.tencent.mm/.ui.LauncherUI` and `resolved_activities.json` is non-empty | Inspect `dumpsys package <pkg>` manually and confirm the package has `MAIN` + `LAUNCHER`; otherwise drop that package from the manifest | branch to dumpsys parser fallback; do not treat this as package-install, kernel, or CVD boot failure |
| Synthetic workload build | CVD真实APK多数因架构/Google服务/ClassNotFound无法启动，压力不足 | Build/install `scripts/build_mthp_synth_apks.py` APK matrix; keep native libs embedded, uncompressed, and `zipalign -P 16` aligned | `p00` shows `dlopen_ok=4`; `p14` shows first fork round; `p21` starts main plus three worker processes | Read `references/synthetic_mthp_apk_workload.md`; do not fall back to extracted native libs because install-time strip can break linker section-header checks | branch to synthetic workload instead of expanding unusable app list |
| Synthetic APK install/collection | install loop advances one APK/process only, or maps collection only reports first process | Ensure every nested `adb` in `while read` loops uses `</dev/null`; install with `adb install --no-incremental -r -g` | Success/fail TSV records all expected APKs; p21 maps count covers main and three workers | Restart only the preload/collection worker; preserve CVD userdata | replace loop implementation |
| Synthetic APK install/collection | Need to preload the 60-APK synthetic matrix onto A/B CVD profiles | Use `scripts/install_mthp_synth_apks_ab.sh run` with `APK_OUT=<synthetic output>` and profile serials `127.0.0.1:16521`/`127.0.0.1:16522` | `install-A/packages.txt` and `install-B/packages.txt` list at least 60 `com.zzhao.mthp.synth` packages | If adb is offline, launch/fix the profile first through the A/B CVD workflow; do not let adb consume profile TSV input | continue to smoke or sampler |
| Synthetic long run | Running synthetic packages through `run_memstress_and_collect_logs.py` | Preserve the skill/default-manifest pressure knobs explicitly: `--burst-size 4 --hold-ms 15 --launch-gap-ms 15 --cycle-sleep-ms 1000 --seed 20260617`; passing only `--package-file` falls back to the script's lower-pressure built-in defaults | `run_manifest.json` records burst `4`, hold `15`, launch gap `15`, and seed `20260617` | If these values are absent or differ unintentionally, discard the cell and rerun with explicit knobs or `--from-manifest` | replace command |
| B16K residual order0 attribution | Need to classify residual order-0 allocations in B/full16K rather than complete a performance run | Boot B with 16KB-aligned userspace plus early cmdline `uffd_mfill_order2=1 mthp_cow_order2=1`, enable 16KB mTHP and defrag `always`, keep zram/swap on, collect all residual vmstat heads, then stop early only after the distribution is visibly non-flat | `/proc/cmdline` has both order-2 params, `/proc/swaps` shows active zram, `vmstat_samples.csv` contains residual heads such as `order0_tlb_gather_batch_page`, `order0_tlb_table_batch_page`, `order0_pte_alloc_page`, `order0_pmd_alloc_page`, `order0_slub_new_slab_page`, `order0_zsmalloc_page`, `order0_vmalloc_page`, and scudo/dalvik diagnostics | If swap/zram is off, cmdline is missing, or new counters are absent, discard the slice and rerun; if enumerated heads are evenly spread or mostly unknown, continue longer or add the next counter tier before concluding | branch to early attribution gate; do not report total runtime as a completed 120-cycle metric |
| CVD zram setup | `modprobe zram` fails because `/lib/modules` is absent, but `/system_dlkm/lib/modules/<release>/kernel/{mm,drivers/block/zram}` exists | Load zram directly with `insmod /system_dlkm/lib/modules/<release>/kernel/mm/zsmalloc.ko` then `insmod /system_dlkm/lib/modules/<release>/kernel/drivers/block/zram/zram.ko`, set `/sys/block/zram0/disksize`, run `mkswap`, and `swapon` | `/proc/modules` lists `zsmalloc` and `zram`; `/proc/swaps` lists `/dev/block/zram0`; `order0_zsmalloc_page` or `order2_zsmalloc_page` can move during swap pressure | If the ko paths are absent, treat the custom kernel/DLKM image pairing as incomplete and return to the CVD A/B workflow before sampling | branch before workload |
| Synthetic result interpretation | A cell or aggregate summary has `complete=0`, `n=0`, missing order counters, or fewer cycles than the requested `MAX_CYCLES` | Treat it as an incomplete smoke artifact only; do not compare total allocation, stall rate, or A/B deltas against completed runs | Per-cell summary shows `complete=1`, cycles equal requested `MAX_CYCLES`, and order/source counters are populated; for multi-round reports every intended round/cell has a done marker | If only partial cycles exist, report the partial cycle count and rerun the intended cell before drawing allocation-total conclusions | block metric conclusion until completion gate passes |
| Synthetic workload accounting | Any synthetic APK cell completes memstress | Run `scripts/compute_synthetic_workload_expectations.py --profiles-tsv <APK_OUT>/profiles.tsv --run-manifest <cell>/run_manifest.json --package-file <cell>/workload_packages.txt --cycle-log <cell>/memstress/cycle_log.jsonl --out-dir <cell>` | Cell directory contains `synthetic_workload_expectations.tsv/json/md`; if cycle log exists, `synthetic_workload_launch_weighted.tsv` exists and total launches matches `max_cycles × burst_size` | If files are missing, treat the cell report as incomplete even when kernel counters exist; regenerate expectations before writing the experiment report | block report generation until workload expectation is present |
| Source-accounting threshold A/B | Need to compare source split and then test a kswapd order-2 threshold patch | First run the unmodified baseline cells serially with identical package list, seed, runtime scales, and `MAX_CYCLES`; only after all intended cells are complete, switch the patched x86 CVD kernel through `--kernel_path`, keep Android images unchanged, and enable runtime sysctls such as `kswapd_order2_threshold` and `kswapd_order2_wakeup_threshold` for the target B cell | Every compared cell has `status=finished`, requested cycle count, `sample_errors=0`, identical `synthetic_workload_expectations.md` launch-weighted totals, correct B cmdline for COW/UFFD order-2, and pre-snapshot sysctls showing the threshold values | If any cell is partial, uses different workload scales, lacks expectation files, or boots without the expected cmdline/sysctls, discard that metric comparison and rerun the cell; do not recompose Android images for kernel-only threshold changes | branch kernel-only threshold experiment after baseline; block cross-run conclusions unless workload and instrumentation level match |
| Synthetic VMA alignment evidence | Need to claim synthetic hand-created VMA start/length 16KB alignment | After memstress and before stopping the profile, collect `[anon:mthp_vma_*]` entries from `/proc/<pid>/maps` into `synthetic_vma_maps.tsv`, with start/end/size and modulo-16KB columns | `synthetic_vma_maps_summary.txt` reports total, `start_aligned_16k`, `start_unaligned_16k`, `size_aligned_16k`, `size_unaligned_16k` | If maps are missing because processes died, report only logcat `region_size=16384` length evidence and do not claim per-VMA start-address audit | block start-address claims unless maps evidence exists |
| Cycle-time reporting | Comparing workload duration across A/B cells or across runs | Use `memstress/cycle_timing.json` (`total_elapsed_s`, `mean_cycle_s`, `p95_cycle_s`) as the workload cycle-time source of truth; treat outer `wall_time.txt` only as orchestration overhead | Summary table includes cycle timing fields, and `cycle_timing.json` note says deltas are consecutive `cycle_start_ts` including burst/json write/cycle sleep | If only `wall_time.txt` exists, report it explicitly as outer wall time and do not call it cycle time | replace reporting metric |
| 16KB THP profile | Running any `full16K` cell or comparing Pixel/CVD alloc-stall reasons | Explicitly set `/sys/kernel/mm/transparent_hugepage/defrag` before workload; use `always` for Pixel-parity fragmentation attribution unless the experiment intentionally labels `madvise` | Profile snapshot records `/sys/kernel/mm/transparent_hugepage/defrag=[always] ...` or the intentionally chosen mode, plus `hugepages-16kB/enabled=[always]` and other sizes `never` | If defrag was left at boot default such as `[madvise]`, mark the cell contaminated for stall-reason comparison and rerun or relabel it as `defrag=madvise` | block/relabel full16K comparisons; defrag mode is a first-class variable |
| Experiment restart evidence | Any CVD relaunch/restart happens inside an A/B or synthetic long run | Append an immutable restart ledger row for launch start and launch ready, including timestamp, profile, serial, run dir, extra kernel cmdline, launch log, cell/profile context, boot_id, and `/proc/cmdline` | `$RUN_ROOT/state/restart_ledger.tsv` has paired `launch_start`/`launch_ready` rows for every cell and pre-run launch; per-cell `pre_snapshot.txt` still records profile state | If only `runner.log` or overwritten `<profile>_boot.txt` exists, treat restart history as incomplete and add/recover a ledger before relying on cross-cell comparisons | block/reconstruct restart provenance before attributing result differences |
| Synthetic long run | CVD relaunch or image rotation leaves fewer than 60 synthetic packages installed, even after a previous 60/0 preload | After every active-profile relaunch, auto-run `scripts/install_mthp_synth_apks_ab.sh run` with `APK_OUT=<synthetic output>` if `WORKLOAD_PACKAGE_FILE` packages are missing, then wait on `pm path` package evidence | Package check against `WORKLOAD_PACKAGE_FILE` reaches 60 before sampler starts; early `cycle_log.jsonl` has zero launch errors; logcat has `ZZMthpSynthNative`, `regions=`, and COW `fork_round=` markers | If `APK_OUT` is unavailable or install fails, abort the cell early; do not wait forever or continue a 120-cycle run with stale package-count evidence | branch to post-relaunch installer |
| Synthetic workload calibration | Long run shows near-zero `allocstall`/direct reclaim or synthetic pressure is suspected too weak | Sample real device anonymous VMA size/RSS with `scripts/sample_anon_vma_sizes.py`, then compare against synthetic `vma_size_kb`, COW pages, and filemap size | `anon_kind_summary.tsv` and `anon_process_summary.tsv` separate virtual reservations from RSS-bearing VMAs | If only idle/system apps are sampled, launch representative heavy apps and resample before retuning | branch to workload retuning before rerunning A/B |
| Synthetic workload semantics | Need deterministic resident pressure rather than sparse/partial touches | Keep anonymous synthetic VMAs fully write-faulted at startup, keep pad `.so` and filemap paths full read-faulted, and record `anon_full_fault_pages` / `anon_fault_mode` in `profiles.tsv` | logcat shows `anon_pages_written=...`; `profiles.tsv` has `anon_fault_mode=full_write`, `so_fault_mode=full_read`, and `filemap_fault_mode=full_read` | If full-write OOMs immediately, retune `vma_count`/`vma_size_kb` in builder while preserving full-fault semantics; do not reintroduce sparse `parent_touch_pages` as the main pressure knob | replace sparse-touch workload model |
| Synthetic pressure retuning | Full 60-APK profile is too heavy, but package topology should stay fixed | Keep the installed full-profile APKs and pass runtime flags `--synthetic-anon-vma-size-scale <f>`, `--synthetic-cow-pages-scale <f>`, and `--synthetic-filemap-size-scale <f>` first; if per-cycle `order0`/cycle time remains dominated by fixed VMA/dlopen startup evidence, add `--synthetic-vma-count-scale <f>` and `--synthetic-dlopen-lib-count-scale <f>` or runner env `SYNTHETIC_VMA_COUNT_SCALE` / `SYNTHETIC_DLOPEN_LIB_COUNT_SCALE` | `run_manifest.json` records all scale values; logcat has `ZZMthpSynth runtime_scales ... vma_count_scale=... dlopen_lib_count_scale=...`; `started` markers show lower `regions=` and `dlopen_ok=` | If logcat has no `runtime_scales`, the installed APK predates runtime tuning support; rebuild/reinstall synthetic APKs once, then vary flags without rebuilding | branch runtime tuning; do not rebuild APKs for scale-only changes |
| Scudo/Java heap snapshot | Need to compare A/B Scudo and Java heap footprint without long churn loops | Run `scripts/scudo_java_snapshot_ab.sh run` with `SCUDO_MB=50 JAVA_MB=50` or `SCUDO_MB=30 JAVA_MB=30`; snapshot mode forces `process_count=1` and `scudo_threads=1` so targets mean per app, not per worker; use an APK_OUT that contains `java fill_once` | `summary_by_component.tsv` has separate `scudo` and `dalvik_*` rows for A/B; `snapshot_stability.tsv` has at least three snapshots or a stable row; install verification has host/device APK sha256 `ok`; logcat has `scudo_worker ... live_bytes=` and `java fill_once ... live_bytes=` | If B is offline, bring B online through the CVD A/B workflow before treating results as A/B; if Java heap caps, lower `JAVA_MB`; if fill-once log is absent, rebuild/reinstall APKs before interpreting smaps | branch to snapshot workflow, not long-run sampler |
| Scudo/Java heap snapshot preflight | Snapshot result has fixed A-only or B-only `Swap` across repeated runs, or A/B package counts differ after CVD restart | Stop and relaunch both CVD instances with `RESUME=true`, keep the same `--kernel_path` and A/B boot cmdline, run `swapoff -a` plus zram reset, reapply THP/fs folio sysfs, and gate every target package with `pm path` before launching apps | Pre-state files show `SwapTotal=0`, `SwapCached=0`, empty `/proc/swaps`, expected THP/folio caps, A cmdline without order-2 boot params, B cmdline with requested order-2 params, launch ok count equals target count, and missing package count is zero in every snapshot | If packages are missing, install from the exact `APK_OUT` before launching; if swap is nonzero, discard the snapshot instead of interpreting RSS/PSS; if cmdline or THP state diverges, relaunch/reconfigure before sampling | block snapshot interpretation until cleanboot/swap/package gates pass |
| Synthetic Scudo path interpretation | Native synthetic `large_alloc_bytes=65536` is being interpreted as a Scudo large/secondary allocation | Treat it as Scudo primary on Android: `NeededSize = roundUp(65536, 16) + 16 = 65552 = 0x10010`, matching the largest `AndroidSizeClassMap` class; only larger requests or special alignment pressure go secondary | Source check: `external/scudo/standalone/combined.h` allocation branch, `chunk.h` header size, and `size_class_map.h` Android class table | If `large_alloc_bytes` changes above 65536, recompute `NeededSize` and reclassify before writing reports | update interpretation, not workload execution |

### Output Contract
- 运行脚本：`scripts/run_memstress_and_collect_logs.py`
- 使用 manifest：`config/default_memstress_manifest.json`
- 输出目录：`--out-dir` 指定，或默认 `/tmp/thp_memstress_<timestamp>`
- 关键产物：`derived.csv`（含 `fallback_ratio`）、`summary.md`（含 `anon_alloc`/`anon_fallback`/`fallback_ratio`/`alloc_stall`/`compact_stall`，均为 end - start）、`run_manifest.json`
- synthetic 关键产物：`synthetic_workload_expectations.tsv/json/md`；有 `cycle_log.jsonl` 时还应有 `synthetic_workload_launch_weighted.tsv`
- synthetic VMA 对齐产物：`synthetic_vma_maps.tsv`、`synthetic_vma_maps_summary.txt`

## Precondition (可选独立脚本)

按实验需要在 memstress 之前运行，用来制造碎片化初始状态。默认 A/B 短测和常规长测不强制运行 precondition；只有明确要测碎片化初始状态或验证 fallback 压力时才启用。启用时，THP/系统参数由外部启动脚本设置，precondition 只负责 order-0 碎片化。

```bash
python3 scripts/precondition.py --serial <SERIAL> --alloc-mb 5000 --threshold 2000
```

- 自动重启设备、等待 su 就绪
- 运行 fragmem（全 order-0 分配 + munmap 碎片化）
- fragmem 在后台 hold 内存，实验结束后 `killall fragmem`

可选流程顺序：
1. 不启用 precondition：由外部启动脚本设置 THP / sysctl 配置，然后运行 `run_memstress_and_collect_logs.py`。
2. 启用 precondition：先运行 `precondition.py`（重启 + 碎片化）。
3. 启用 precondition 后：不再重启，重新设置 THP / sysctl 配置，再运行 `run_memstress_and_collect_logs.py --post-prepare-cmd '...'`（温控 → 锁频 → post-prepare 设 compaction → workload）。

**规则**：precondition 是可选变量；启用后不再重启，碎片状态通过 fragmem hold 保持。

## CPU Accounting (独立脚本)

采集 kcompactd/kswapd CPU 时间 + direct reclaim/compact 精确耗时。与主脚本解耦。

```bash
# 在 memstress 前启动 trace:
python3 scripts/trace_cpu_accounting.py start --serial <SERIAL> --out-dir <RUN_DIR>

# (跑 memstress)

# memstress 结束后收集:
python3 scripts/trace_cpu_accounting.py stop --serial <SERIAL> --out-dir <RUN_DIR>

# 离线分析:
python3 scripts/trace_cpu_accounting.py analyze --out-dir <RUN_DIR>
```

产出：
- `schedstat_start.json` / `schedstat_end.json`：kcompactd/kswapd 的 on_cpu_ns, wait_ns, timeslices
- `ftrace_mm.txt`：raw ftrace（mm_vmscan_direct_reclaim_begin/end, mm_compaction_begin/end）
- `direct_reclaim_stats.json`：解析后的 direct reclaim/compact 总耗时和次数

**规则**：
- trace 脚本不影响主脚本的任何行为
- schedstat 零开销（读 /proc）
- ftrace mm instance 独立 buffer，事件量小（几万级），开销可忽略
- 随机种子永远不动：`20260617`
