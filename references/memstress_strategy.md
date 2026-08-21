# Memstress strategy (default: `am start` + HOME; optional round force-stop)

`scripts/run_memstress_and_collect_logs.py` is intentionally kept simple:

- Always uses `am start -n <component>` (**no `-W`**) so it does not fully wait for Activity launch completion.
- After each successful launch:
  - sleep `--hold-ms` (default **200ms**)
  - press HOME (`input keyevent KEYCODE_HOME`) to exit foreground
- By default **does not use `am force-stop`** and does not implement any LRU eviction.
  - If you pass `--round-s > 0`, memstress will treat cycles as **rounds**, and at each round boundary it will:
    - `am force-stop <pkg>` for all target runnable packages
    - immediately start the next round (no extra sleep)

This matches the “flash into app briefly then return HOME, but don’t kill” workload.

## Crash signature detection (ClassNotFound / oat/dex symptom)

During memstress, logcat is streamed into `memstress/logcat_all_threadtime.txt`. If we detect:

- `am_crash` and
- one of `ClassNotFoundException` / `NoClassDefFoundError` / `ClassNotFoundError`

in close proximity, we write a marker file:

- `<out_dir>/<serial>/memstress/crash_signature.json`

and stop the run early.

## Useful parameter set (extreme churn)

```bash
--burst-size 1 \
--heavy-per-burst 0 \
--hold-ms 200 \
--launch-gap-ms 0 \
--cycle-sleep-ms 0
```

## Verifying behavior

- Per-cycle log: `<out_dir>/<serial>/memstress/cycle_log.jsonl`
  - `launched`: packages successfully started
  - `launch_errors`: launch failures (no force-stop cleanup)
  - `event=round_boundary`: end-of-round force-stop operations (only when `--round-s > 0`)
