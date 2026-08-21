from __future__ import annotations

import os
import pty
import select
import shlex
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Callable, Iterable, List, Optional, Sequence, Tuple


def run(
    cmd: Sequence[str],
    *,
    timeout_s: int = 60,
    check: bool = False,
    capture_output: bool = True,
    text: bool = True,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        list(cmd),
        timeout=timeout_s,
        check=check,
        capture_output=capture_output,
        text=text,
    )


def run_with_pty(cmd: Sequence[str], *, timeout_s: int = 60) -> subprocess.CompletedProcess:
    """Run a command with a pseudo-tty attached and return combined output in stdout.

    Some Android devices require an interactive TTY for `adb shell -t -t su -c ...`
    to make sysfs writes succeed.
    """

    master_fd, slave_fd = pty.openpty()
    proc = subprocess.Popen(list(cmd), stdin=slave_fd, stdout=slave_fd, stderr=slave_fd, close_fds=True)
    os.close(slave_fd)

    chunks: List[bytes] = []
    deadline = time.time() + timeout_s

    try:
        while True:
            if time.time() > deadline:
                proc.kill()
                proc.wait(timeout=5)
                raise subprocess.TimeoutExpired(cmd=list(cmd), timeout=timeout_s)

            ready, _, _ = select.select([master_fd], [], [], 0.2)
            if master_fd in ready:
                try:
                    data = os.read(master_fd, 4096)
                except OSError:
                    data = b""
                if data:
                    chunks.append(data)
                elif proc.poll() is not None:
                    break

            if proc.poll() is not None and master_fd not in ready:
                break
    finally:
        try:
            os.close(master_fd)
        except OSError:
            pass

    stdout = b"".join(chunks).decode("utf-8", "ignore")
    return subprocess.CompletedProcess(list(cmd), proc.returncode or 0, stdout=stdout, stderr="")


def adb_devices() -> List[str]:
    cp = run(["adb", "devices"], timeout_s=20, check=True)
    serials: List[str] = []
    for line in cp.stdout.splitlines():
        line = line.strip()
        if not line or line.startswith("List of devices"):
            continue
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            serials.append(parts[0])
    return serials


def resolve_serial(user_serial: Optional[str]) -> str:
    if user_serial:
        return user_serial
    serials = adb_devices()
    if len(serials) == 1:
        return serials[0]
    if not serials:
        raise RuntimeError("No device found (adb devices shows none in 'device' state)")
    raise RuntimeError("Multiple devices detected; pass --serial. Devices: " + ", ".join(serials))


def _split_serial_tokens(tokens: Sequence[str]) -> List[str]:
    out: List[str] = []
    for t in tokens:
        for part in str(t).split(","):
            x = part.strip()
            if x:
                out.append(x)
    return out


def resolve_serials(user_serials: Optional[Sequence[str]], *, all_devices: bool) -> List[str]:
    if all_devices:
        serials = adb_devices()
        if not serials:
            raise RuntimeError("No device found (adb devices shows none in 'device' state)")
        return serials

    if user_serials:
        serials = _split_serial_tokens(user_serials)
        if not serials:
            raise RuntimeError("--serial provided but empty after parsing")
        return serials

    return [resolve_serial(None)]


def adb_base(serial: str) -> List[str]:
    return ["adb", "-s", serial]


def adb_shell_cp(serial: str, cmd: str, *, timeout_s: int = 60, check: bool = False) -> subprocess.CompletedProcess:
    """Run `adb shell <cmd>` and return a CompletedProcess.

    Unlike `run(...)`, this helper is used in long-running loops (memstress/sampling).
    We treat timeouts as a non-fatal outcome and return a synthetic CompletedProcess
    with returncode=124, so callers can log and continue.
    """

    argv = adb_base(serial) + ["shell", cmd]
    try:
        return run(argv, timeout_s=timeout_s, check=check)
    except subprocess.TimeoutExpired as e:
        # `subprocess.run(..., text=True)` may still provide bytes in TimeoutExpired on some versions.
        out = e.stdout
        err = e.stderr
        if isinstance(out, bytes):
            out = out.decode("utf-8", "ignore")
        if isinstance(err, bytes):
            err = err.decode("utf-8", "ignore")
        return subprocess.CompletedProcess(
            argv,
            124,
            stdout=(out or ""),
            stderr=(err or f"timeout after {timeout_s}s"),
        )


def adb_shell(
    serial: str,
    cmd: str,
    *,
    timeout_s: int = 30,
    tty: bool = False,
    check: bool = True,
) -> str:
    base = adb_base(serial)

    shell_cmd = ["shell"]
    if tty:
        shell_cmd.extend(["-t", "-t"])
        cp = run_with_pty(base + shell_cmd + [cmd], timeout_s=timeout_s)
    else:
        # Avoid extra `sh -c` layer for argv-style tools such as `input`/`wm`.
        cp = run(base + shell_cmd + [cmd], timeout_s=timeout_s, check=False)

    if check and cp.returncode != 0:
        raise RuntimeError((cp.stderr or cp.stdout or "adb shell failed").strip())
    return cp.stdout or ""


def adb_shell_retry(
    serial: str,
    cmd: str,
    *,
    timeout_s: int,
    retries: int,
    retry_sleep_s: int,
    tty: bool = False,
) -> str:
    last_err: Optional[Exception] = None
    attempts = max(1, retries + 1)
    for i in range(attempts):
        try:
            return adb_shell(serial, cmd, timeout_s=timeout_s, tty=tty, check=True)
        except Exception as e:
            last_err = e
            if i + 1 < attempts:
                time.sleep(max(0, retry_sleep_s))
    raise RuntimeError(str(last_err) if last_err else "adb_shell_retry failed")


# ================= 权限命令统一入口（自动 root / su） =================
#
# 设计：探测一次权限模式后，把模块级函数对象 adb_shell_root rebind 成
# 具体实现（root 直接跑 / su 包装前缀），之后每次调用就是普通函数调用，
# 无分支、无缓存查找。调用点使用 `adb_utils.adb_shell_root(...)`（模块属性
# 引用，rebind 生效）；未探测即调用会显式报错。


def _shell_root_direct(serial: str, cmd: str, *, timeout_s: int = 30,
                       tty: bool = False, check: bool = True) -> str:
    """root 模式：adbd 已以 root 运行，直接执行（tty 忽略）。"""
    return adb_shell(serial, cmd, timeout_s=timeout_s, check=check)


def _shell_root_su(serial: str, cmd: str, *, timeout_s: int = 30,
                   tty: bool = False, check: bool = True) -> str:
    """su 模式：adb shell su -c '<cmd>'（cmd 整体单引号引用）。

    注意：不能包一层 `sh -c`（su -c sh -c 'cmd'）——magisk su 的 -c 只取
    紧跟的一个参数作为命令，`sh` 会被当作命令执行而 'cmd' 被忽略，输出为空。
    tty 时用交互 TTY（部分设备 sysfs 写必须 -t -t + pty 才能成功）。
    """
    quoted = shlex.quote(cmd)
    if tty:
        cp = run_with_pty(adb_base(serial) + ["shell", "-t", "-t",
                           f"su -c {quoted}"], timeout_s=timeout_s)
    else:
        cp = run(adb_base(serial) + ["shell", "su", "-c", quoted],
                 timeout_s=timeout_s, check=False)
    if check and cp.returncode != 0:
        raise RuntimeError((cp.stderr or cp.stdout or "adb shell failed").strip())
    return cp.stdout or ""


def _shell_root_uninit(serial: str, cmd: str, **kw) -> str:
    raise RuntimeError("privilege mode not initialized: call "
                       "adb_utils.ensure_privilege(serial) first")


# 模块级函数对象：ensure_privilege() 探测后 rebind 为具体实现。
adb_shell_root = _shell_root_uninit


def _shell_id_uid0(serial: str) -> bool:
    """adb shell id 输出是否 uid=0（adbd 已 root）。"""
    try:
        out = adb_shell(serial, "id", timeout_s=15, check=False)
    except Exception:
        return False
    return "uid=0" in out


def _su_id_uid0(serial: str) -> bool:
    """su -c id 是否 uid=0（magisk/root su 可用）。tty 变体兜底。"""
    for tty in (False, True):
        try:
            out = _shell_root_su(serial, "id", timeout_s=15, tty=tty, check=False)
        except Exception:
            continue
        if "uid=0" in out:
            return True
    return False


def ensure_privilege(serial: str) -> None:
    """探测权限模式并把 adb_shell_root rebind 成具体实现。

    runner 启动时调用一次。顺序：
      1) adbd 已是 root（uid=0）          -> 直接模式
      2) adb root（adbd 重启后重连验证）  -> 直接模式
      3) su -c id uid=0                   -> su 模式
      4) 全部失败                          -> 抛错（实验早停）
    """
    global adb_shell_root
    base = adb_base(serial)

    if _shell_id_uid0(serial):
        adb_shell_root = _shell_root_direct
        return

    # 尝试 adb root（重启 adbd；user build 会失败）
    cp = run(base + ["root"], timeout_s=20, check=False)
    root_out = (cp.stdout or "") + (cp.stderr or "")
    if "cannot run as root in production" not in root_out:
        try:
            subprocess.run(base + ["wait-for-device"], timeout_s=30)
        except Exception:
            pass
        time.sleep(1)
        if _shell_id_uid0(serial):
            adb_shell_root = _shell_root_direct
            return

    if _su_id_uid0(serial):
        adb_shell_root = _shell_root_su
        return

    raise RuntimeError(
        f"no privilege mode available on {serial}: adb root refused "
        f"({root_out.strip()[:120]}) and su is not usable")


@dataclass
class LogcatHandle:
    proc: subprocess.Popen
    path: Path
    _fh: IO[str]

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        try:
            self._fh.close()
        except Exception:
            pass


def start_logcat(
    serial: str,
    out_dir: Path,
    *,
    clear_logcat: bool,
    filename: str = "logcat_all_threadtime.txt",
) -> LogcatHandle:
    out_dir.mkdir(parents=True, exist_ok=True)
    logcat_path = out_dir / filename
    base = adb_base(serial)
    if clear_logcat:
        run(base + ["logcat", "-c"], timeout_s=20, check=False)
    fh = logcat_path.open("w", encoding="utf-8")
    proc = subprocess.Popen(base + ["logcat", "-v", "threadtime", "-b", "all"], stdout=fh, stderr=subprocess.DEVNULL)
    return LogcatHandle(proc=proc, path=logcat_path, _fh=fh)


@dataclass
class LogcatStreamHandle:
    """A logcat capture handle that can stream lines to a callback while saving to file."""

    proc: subprocess.Popen
    path: Path
    _fh: IO[str]
    _thread: threading.Thread
    _stop_event: threading.Event

    def stop(self) -> None:
        self._stop_event.set()
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        try:
            self._thread.join(timeout=5)
        except Exception:
            pass
        try:
            self._fh.close()
        except Exception:
            pass


def start_logcat_stream(
    serial: str,
    out_dir: Path,
    *,
    clear_logcat: bool,
    filename: str = "logcat_all_threadtime.txt",
    line_callback: Optional[Callable[[str], None]] = None,
    stop_event: Optional[threading.Event] = None,
) -> LogcatStreamHandle:
    """Start `adb logcat` and write to file, optionally streaming each line to callback.

    This is useful when you want to detect log signatures (e.g., crashes) and stop early.
    """

    out_dir.mkdir(parents=True, exist_ok=True)
    logcat_path = out_dir / filename
    base = adb_base(serial)
    if clear_logcat:
        run(base + ["logcat", "-c"], timeout_s=20, check=False)

    fh = logcat_path.open("w", encoding="utf-8")
    proc = subprocess.Popen(
        base + ["logcat", "-v", "threadtime", "-b", "all"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )

    internal_stop = threading.Event()
    external_stop = stop_event

    def _pump() -> None:
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                if internal_stop.is_set() or (external_stop is not None and external_stop.is_set()):
                    break
                fh.write(line)
                if line_callback is not None:
                    try:
                        line_callback(line)
                    except Exception:
                        # Never let callback failures kill log capture.
                        pass
        finally:
            try:
                fh.flush()
            except Exception:
                pass

    t = threading.Thread(target=_pump, name=f"logcat_stream_{serial}", daemon=True)
    t.start()
    return LogcatStreamHandle(proc=proc, path=logcat_path, _fh=fh, _thread=t, _stop_event=internal_stop)


def stop_monkey_best_effort(serial: str) -> None:
    run(adb_base(serial) + ["shell", "sh", "-c", "pkill -f com.android.commands.monkey || true"], timeout_s=10, check=False)


def ensure_adb_works() -> None:
    try:
        run(["adb", "version"], timeout_s=10, check=True)
    except Exception as e:
        raise RuntimeError("adb not found or not working in PATH") from e
