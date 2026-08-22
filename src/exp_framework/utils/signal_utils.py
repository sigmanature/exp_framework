"""事件化 sleep / 子进程执行：stop_event 置位立即返回（Ctrl+C 快速响应核心）。

设计：Python 信号 handler 本身是异步事件（微秒级执行），响应延迟的真正来源是
主线程阻塞在不可中断的调用里（time.sleep 到期、subprocess.run 等 adb 超时）。
本模块提供两个原语，把所有阻塞点变成"事件唤醒，而不是等到期"：

- sleep_interruptible(stop_event, s)：内部用 stop_event.wait()（条件变量），
  信号置位立即唤醒（毫秒级）；stop_event 为 None 时退化为普通 sleep（兼容）。
- run_interruptible(stop_event, cmd, ...)：Popen + 每 0.2s 轮询 stop_event，
  置位立即 terminate 子进程并抛 TimeoutExpired（语义与超时一致，调用方现有
  异常处理路径直接复用），不再等 adb 自然超时。
"""
from __future__ import annotations

import subprocess
import time
from typing import Optional, Sequence


def sleep_interruptible(stop_event, seconds: float) -> bool:
    """事件化 sleep：stop_event 置位立即返回 False；正常睡满返回 True。

    stop_event 为 None 时退化为普通 time.sleep（返回 True），用于
    无需中断感知的调用点（如信号 handler 内）。
    """
    if stop_event is None:
        time.sleep(seconds)
        return True
    return not stop_event.wait(seconds)


def run_interruptible(
    stop_event,
    cmd: Sequence[str],
    *,
    timeout_s: float = 60,
    check: bool = False,
    capture_output: bool = True,
    text: bool = True,
    poll_interval_s: float = 0.2,
    **kwargs,
) -> subprocess.CompletedProcess:
    """事件化 subprocess：Popen + 轮询 stop_event，置位立即 terminate。

    stop_event 为 None 时行为与 subprocess.run 一致（退化路径）。
    正常退出：返回 CompletedProcess；超时或 stop_event 置位：terminate/kill
    后抛 subprocess.TimeoutExpired（output/stderr 已带回）。
    """
    if stop_event is None:
        return subprocess.run(
            list(cmd),
            timeout=timeout_s,
            check=check,
            capture_output=capture_output,
            text=text,
            **kwargs,
        )

    popen_kwargs = {}
    if capture_output:
        popen_kwargs["stdout"] = subprocess.PIPE
        popen_kwargs["stderr"] = subprocess.PIPE
    proc = subprocess.Popen(list(cmd), text=text, **popen_kwargs, **kwargs)
    deadline = time.monotonic() + timeout_s

    while True:
        if proc.poll() is not None:
            break
        if stop_event.is_set():
            proc.terminate()
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            proc.kill()
            break
        stop_event.wait(min(poll_interval_s, remaining))

    stopped = stop_event.is_set() and proc.poll() is None
    try:
        out, err = proc.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()

    if stopped:
        raise subprocess.TimeoutExpired(
            list(cmd), timeout=timeout_s, output=out, stderr=err)

    ret = subprocess.CompletedProcess(list(cmd), proc.returncode, out, err)
    if check and ret.returncode != 0:
        raise subprocess.CalledProcessError(
            ret.returncode, ret.args, output=ret.stdout, stderr=ret.stderr)
    return ret
