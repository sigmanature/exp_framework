# Recovering from `adb: device offline` during long runs

When a device is listed as `offline` in `adb devices -l`, it may be:
- rebooting / unstable (watchdog, crash, recovery)
- stuck in a bad USB/adb state

## Quick recovery checklist (host-side)

```bash
adb devices -l
```

If a device stays `offline` for more than ~10–30s:

1) Try reconnecting only offline devices:

```bash
adb reconnect offline
adb devices -l
```

2) Restart adb server (safe; affects all devices):

```bash
adb kill-server
adb start-server
adb devices -l
```

3) If it is still missing/offline, assume the phone is not fully booted
   (or is in recovery). You may need manual intervention on the device
   (power key / reboot system).

## Notes for automation

- Some stress patterns (very fast `am start -W` + `am force-stop`) can
  trigger instability on experimental builds. Expect occasional `offline`
  transitions; scripts should keep sampling best-effort and not crash on a
  single timeout.
