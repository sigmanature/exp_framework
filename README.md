# Android THP 16KB Fallback Reproduction Experiment

This project runs a real-world Android app launch/background workload to compare a **16KB THP enabled** run against a **4 KB baseline with 16KB THP disabled**. It measures how often the kernel falls back from anonymous 16KB hugepages to 4KB pages.

The main output is `summary.md`, which reports:

- Number of successful 16KB anonymous hugepage allocations
- Number of fallbacks to 4KB pages
- Fallback ratio
- Memory allocation stall and compaction event counts

## What you need

- An Android device connected via adb
- A rooted device (the script wraps root commands with `su -c` by default; if you already ran `adb root`, use `--no-use-su` instead)
- Python 3.10 or newer
- A kernel that exposes both of these paths:
  - `/sys/kernel/mm/transparent_hugepage/hugepages-16kB/enabled`
- `/sys/kernel/mm/transparent_hugepage/hugepages-16kB/stats`
- Some apps installed on the device that you want to test

## Python environment

Install the Python environment with:

```bash
python3 -m pip install -r requirements.txt
```

## Reproduction Commands

Replace `<YOUR_DEVICE_SERIAL>` with your device's adb serial from `adb devices`.

### 1) 4 KB baseline (16KB THP disabled)

The main script never touches the THP mode; it is your job to set it before the run. For the baseline run, set `hugepages-16kB/enabled` to `none` first.

```bash
adb -s <YOUR_DEVICE_SERIAL> shell "su -c 'echo none > /sys/kernel/mm/transparent_hugepage/hugepages-16kB/enabled'"
python3 scripts/run_memstress_and_collect_logs.py \
  --serial <YOUR_DEVICE_SERIAL> \
  --from-manifest config/default_memstress_manifest.json \
  --out ./output/baseline_4k

adb -s <YOUR_DEVICE_SERIAL> shell "su -c 'cat /sys/kernel/mm/transparent_hugepage/hugepages-16kB/enabled'"
```

The final `cat` should still show `[none]` for `hugepages-16kB/enabled`.

### 2) 16 KB THP run (16KB THP enabled)

```bash
adb -s <YOUR_DEVICE_SERIAL> shell "su -c 'echo always > /sys/kernel/mm/transparent_hugepage/hugepages-16kB/enabled'"
adb -s <YOUR_DEVICE_SERIAL> shell "su -c 'echo always > /sys/kernel/mm/transparent_hugepage/defrag'"
python3 scripts/run_memstress_and_collect_logs.py \
  --serial <YOUR_DEVICE_SERIAL> \
  --from-manifest config/default_memstress_manifest.json \
  --out ./output/thp_16k

adb -s <YOUR_DEVICE_SERIAL> shell "su -c 'cat /sys/kernel/mm/transparent_hugepage/hugepages-16kB/enabled'"
```
**It's not suggested to change the random seed in default_memstress_manifest.json.**
## How to adapt it to your own device

### Replace the package list

If you want run the experiment with your own apps,a quick way to get installed package names:

```bash
adb shell pm list packages | sed 's/package://' > my_packages.txt
```

Then edit the `packages` field in `config/default_memstress_manifest.json`, or create a new manifest file.


### If you are using adb root

If you already ran `adb root`, add `--no-use-su` to skip the `su -c` wrapper:

```bash
python3 scripts/run_memstress_and_collect_logs.py \
  --serial <YOUR_DEVICE_SERIAL> \
  --from-manifest config/default_memstress_manifest.json \
  --out-dir ./output/thp_16k \
  --no-use-su
```

### Change a single parameter

For example, to use a different output directory:

```bash
python3 scripts/run_memstress_and_collect_logs.py \
  --serial <YOUR_DEVICE_SERIAL> \
  --from-manifest config/default_memstress_manifest.json \
  --out-dir ./output/run_001
```

### Manifest parameters explained

`config/default_memstress_manifest.json` is the default configuration file. You normally do not need to change it, but if you want to tune the experiment, these are the fields you can edit:

| Field | Meaning |
|---|---|
| `serial` | The adb serial of the target device. The command-line `--serial` overrides this value. |
| `config.counters` | The kernel THP stats counters to sample. The default set is `anon_fault_alloc`, `anon_fault_fallback`, `anon_fault_fallback_charge`, `split`, `swpin`, `swpout`, `zswpout`. |
| `config.interval_s` | How often, in seconds, the script reads the THP stats. Default is 60 seconds. |
| `config.use_su` | Whether to wrap root commands with `su -c`. Default is `true`. Set to `false` if you already ran `adb root`. |
| `config.no_network_check` | Whether to skip the network connectivity check. Default is `true`, so the network check is skipped. |
| `config.max_cycles` | Total number of memstress cycles. In each cycle the script launches a burst of apps. Default is 120. |
| `config.memstress.packages` | The list of package names that the workload will launch. This is the most common field to change for your own device. |
| `config.memstress.burst_size` | How many apps to launch in each cycle. Default is 4. |
| `config.memstress.hold_ms` | How long, in milliseconds, each app stays in the foreground before going home. Default is 15. |
| `config.memstress.launch_gap_ms` | Delay in milliseconds between app launches within the same cycle. Default is 15. |
| `config.memstress.cycle_sleep_ms` | Delay in milliseconds between the end of one cycle and the start of the next. Default is 1000. |
| `config.memstress.seed` | Random seed for shuffling the app order. Fixing this makes the experiment reproducible. Default is 20260617. |
| `config.memstress.mode` | `launch_only` (default) launches and holds apps. `interactive` adds a touch step; you usually do not need it. |
| `config.buddyinfo_interval_s` | How often to sample `/proc/buddyinfo`. Set to 0 to disable. Default is 0. |
| `config.vmstat_interval_s` | How often to sample `/proc/vmstat`. Set to 0 to disable. Default is 10. |

Full list of command-line parameters:

```bash
python3 scripts/run_memstress_and_collect_logs.py --help
```

## What output you will get

Each `--out-dir` will contain:

- `raw_samples.csv`: raw cumulative kernel counter values
- `derived.csv`: per-window deltas and the fallback ratio
- `summary.md`: the final summary report
- `run_manifest.json`: the complete parameters and runtime record of this run
- `vmstat_start.json` / `vmstat_end.json`: snapshots of `/proc/vmstat` at the start and end of the experiment
- `memstress/cycle_log.jsonl`: which apps were launched in each cycle

## Metric explanations

`summary.md` contains these metrics:

| Metric | Meaning |
|---|---|
| **anon_alloc** | Total number of successful 16KB anonymous hugepage allocations during the experiment. |
| **anon_fallback** | Total number of 16KB anonymous hugepage allocations that failed and fell back to 4KB pages. |
| **fallback_ratio** | Fallback ratio: `anon_fallback / (anon_alloc + anon_fallback)`. A higher value means large-page allocations are more likely to fail. |
| **alloc_stall** | Total number of times memory allocation stalled because of memory pressure (`allocstall_normal + allocstall_movable`). |
| **compact_stall** | Total number of times memory allocation triggered synchronous compaction to free large contiguous blocks. |

For the 4 KB baseline run, `fallback_ratio` may stay at zero or otherwise be less informative, because 16KB THP is disabled. The baseline run is still useful for comparing the workload path, `alloc_stall`, and `compact_stall` against the 16KB THP run.


## Project file layout

```text
.
├── README.md                           # this file
├── config/
│   └── default_memstress_manifest.json # default experiment configuration
└── scripts/
    ├── run_memstress_and_collect_logs.py   # main script
    ├── derive_metrics.py                   # generates derived.csv and summary.md
    └── utils/                              # modules required by the main script
```