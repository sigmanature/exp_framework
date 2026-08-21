# ADB execution & root/quoting reference

## Table of contents
1. [Pick the right layer: host vs device](#pick-the-right-layer-host-vs-device)
2. [Root models you’ll actually see](#root-models-youll-actually-see)
3. [Quoting rules that prevent 90% of pain](#quoting-rules-that-prevent-90-of-pain)
4. [Safe patterns for root-only artifacts](#safe-patterns-for-root-only-artifacts)
5. [Log capture patterns](#log-capture-patterns)
6. [Common anti-patterns](#common-anti-patterns)
7. [Quick recipes](#quick-recipes)

---

## Pick the right layer: host vs device

### Host-side adb subcommands (run on your computer)
Use these when adb already provides the feature:

- Install / uninstall: `adb install`, `adb uninstall`
- File transfer: `adb push`, `adb pull`
- Log streaming: `adb logcat` (not `adb shell logcat` unless you have a reason)
- Bugreport: `adb bugreport <file.zip>`
- Stream a file to host: `adb exec-out <cmd>`

### Device-side shell commands (run on the phone)
Use `adb shell ...` when you need Android CLI tools:

- Package manager: `pm ...` / `cmd package ...`
- System inspection: `dumpsys ...`, `getprop`, `settings`, `am`, `wm`
- Monkey: `monkey ...`

A good heuristic:
- If the command starts with **`adb <subcommand>`**, run it on host.
- If the command starts with **`pm/dumpsys/am/getprop/monkey`**, run it via `adb shell`.

---

## Root models you’ll actually see

### 1) `adb root` (rare on retail)
Works on `userdebug/eng` builds only. Retail Pixels usually return a denial.

### 2) `su -c` (common with Magisk)
Most rooted retail devices expose `su`. Treat it as **device-side root**.

Root detection (non-interactive):

```bash
adb shell 'command -v su >/dev/null 2>&1 && su -c id >/dev/null 2>&1' && echo "su ok" || echo "no su"
```

Some sysfs nodes still require an **interactive TTY** even when `su -c` exists.
One concrete example on Pixel 6 test devices is:

- `/sys/fs/f2fs/dm-49/max_folio_order_cap`
- `/sys/fs/ext4/dm-3/max_folio_order_cap`

Plain `adb shell su -c 'echo 2 > ...'` may fail with `Permission denied`, while a TTY-backed call succeeds.
If that happens, prefer the TTY pattern documented below (or the skill's `adb_utils.adb_shell(..., tty=True)` helper).

### 3) `run-as <package>` (no root, app-private files)
If the app is debuggable, `run-as` can read `/data/data/<pkg>/...`.

---

## Quoting rules that prevent 90% of pain

### Rule A: pipes/redirection/globs must be inside a remote shell
If you need `|`, `>`, `>>`, `*`, `$()`, or `;`, wrap it in `sh -c`:

```bash
adb shell sh -c 'logcat -d | tail -n 200 > /sdcard/log_tail.txt'
```

### Rule B: root + complex shell = `su -c 'sh -c ...'`
Because `su -c` runs **one command string**, and you still want the device shell to interpret metacharacters:

```bash
adb shell su -c 'sh -c "dmesg | tail -n 200 > /data/local/tmp/dmesg_tail.txt"'
```

### Rule C: keep quoting simple
Prefer single quotes outside, and escape double quotes inside the `sh -c` string.

### Rule D: avoid `adb shell <cmd> > file` confusion
`>` on your computer redirects **host output**, not device output.

If you intend to write a file **on device**, do:

```bash
adb shell sh -c 'echo hello > /sdcard/hello.txt'
```

If you intend to save device output **on host**, do:

```bash
adb shell getprop > getprop.txt
```

---

## Safe patterns for root-only artifacts

### Pattern 1 (recommended): root-copy-to-readable + `adb pull`
1) Use root to create a readable archive in `/data/local/tmp`
2) `chmod 0644` so the shell user can read it
3) `adb pull` the archive

Example (tombstones + anr + dropbox):

```bash
TS=$(date +%Y%m%d_%H%M%S)
DEV=/data/local/tmp/artifacts_$TS.tgz
adb shell su -c 'sh -c "tar -czf '"$DEV"' /data/tombstones /data/anr /data/system/dropbox 2>/dev/null || true; chmod 0644 '"$DEV"'"'
adb pull "$DEV" ./artifacts_$TS.tgz
adb shell rm -f "$DEV"
```

Why it’s safe:
- `adb pull` runs as the **shell** user; it can read `/data/local/tmp`.
- You don’t rely on `adb root`.

### Pattern 2: `adb exec-out` streaming (nice for single files)
If you only need one file and want to avoid temp files:

```bash
adb exec-out su -c 'cat /data/tombstones/tombstone_00' > tombstone_00
```

Notes:
- `exec-out` avoids CRLF munging and is better for binary/large output.
- Still needs `su` for root-only paths.

### Pattern 3: app-private without root (debug builds)

```bash
adb shell run-as com.example.app ls -l files/
adb exec-out run-as com.example.app cat files/crash.log > crash.log
```

---

## Log capture patterns

### Stream logcat during a run (host-side)

```bash
adb logcat -v threadtime -b all > logcat_all_threadtime.txt
```

Optional: clear buffers first (destructive):

```bash
adb logcat -c
```

### dumpsys snapshots

```bash
adb shell dumpsys activity activities > dumpsys_activity.txt
adb shell dumpsys meminfo <pkg> > dumpsys_meminfo_pkg.txt
adb shell dumpsys dropbox --print > dumpsys_dropbox_print.txt
```

### bugreport

```bash
adb bugreport bugreport.zip
```

---

## Common anti-patterns

- **Running `adb` inside `adb shell`**:
  - ❌ `adb shell adb pull ...` (doesn’t work; adb is host-side)

- **Assuming `adb root` exists** on retail:
  - ✅ prefer `su -c` detection

- **Forgetting `sh -c` when using pipes/redirection/globs**:
  - ❌ `adb shell su -c dmesg | tail -n 100` (pipe runs on host)
  - ✅ `adb shell su -c 'sh -c "dmesg | tail -n 100"'`

- **Trying to `adb pull` root-only paths directly**:
  - ❌ `adb pull /data/tombstones ...` (permission denied)
  - ✅ tar/copy to `/data/local/tmp` then pull

---

## Quick recipes

### List third-party packages

```bash
adb shell pm list packages -3 | sed 's/^package://'
```

### Find current foreground activity

```bash
adb shell dumpsys activity activities | grep -E 'mResumedActivity|topResumedActivity' | head
```

### Verify package exists

```bash
adb shell pm path com.example.app
```

### Collect last 200 dropbox lines related to crashes

```bash
adb shell dumpsys dropbox --print | tail -n 200
```

---

## Notes for this skill’s scripts

- 对于长时间 workload（例如 `run_memstress_and_collect_logs.py`），我们会把 `adb shell ...` 的 **超时** 当作一次可记录的失败，而不是直接抛异常中断整轮。你会在日志里看到 `returncode=124`（类似传统的 timeout 语义）。
