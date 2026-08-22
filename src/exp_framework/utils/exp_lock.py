"""实验域×设备串行锁（游标注册表 + FIFO 队列）。

设计（用户确认的最终形态）：
- 锁的键 = (domain, device)，同键互斥、异键并行；锁不依赖进程具体状态，
  权威是游标状态 + 显式的 claim/release。
- exp_lock_claim 是唯一入口：立即跑 or 排队（FIFO），返回
  "running" | "queued:N" | "rejected:cleanup_failed"。
- 状态机：running → done | failed | cleanup_failed；
  cleanup_failed（清理未完成）锁保持占用，必须 exp_lock_clean 人工放行。
- 原子性：所有"读-判-写"临界区由 flock(LOCK_EX) 串行化（内核排队，
  临界区内读到的必是最新状态）；文件写入 = 临时文件 + os.replace
  原子替换（读者永不看到半写 JSON）。
- 硬规范：任何进程不得绕过 exp_lock_* 直接改写 run_cursor.json。
- 注册表位置 ~/.worklog/run_cursor.json（与 experiment_standard 的
  defer/poll/attach 生态兼容）。
"""
from __future__ import annotations

import fcntl
import json
import os
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

CURSOR_FILE = Path.home() / ".worklog" / "run_cursor.json"
LOCK_FILE = Path.home() / ".worklog" / "run_cursor.json.lock"

_local = threading.Lock()  # 同进程多线程再串一层（fcntl 之外的补充）


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read_cursor() -> Dict[str, Any]:
    if not CURSOR_FILE.exists():
        return {"entries": {}}
    return json.loads(CURSOR_FILE.read_text(encoding="utf-8"))


def _write_cursor(d: Dict[str, Any]) -> None:
    """原子替换：写临时文件 + os.replace（读者永不看到半写 JSON）。"""
    CURSOR_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(CURSOR_FILE.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
        os.replace(tmp, CURSOR_FILE)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _with_lock(fn):
    """flock 临界区包装：所有写操作 API 统一走它。"""
    def wrapper(*args, **kwargs):
        with _local:  # 同进程多线程互斥
            fd = os.open(LOCK_FILE, os.O_CREAT | os.O_RDWR, 0o644)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)  # 原子性关键：内核排队
                return fn(*args, **kwargs)
            finally:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    os.close(fd)
    return wrapper


def _entry_claimable(e: Optional[Dict]) -> bool:
    """空闲判定：无条目，或终态（done/failed）。"""
    return e is None or e.get("state") in ("done", "failed")


@_with_lock
def exp_lock_claim(domain: str, device: str, exp_id: str, run_dir: str,
                   session_id: str = "", agent_tool: str = "") -> str:
    """立即跑 or 排队（唯一入口）。

    判定顺序（临界区内，状态必是最新）：
      1) cleanup_failed → 拒绝（必须 exp_lock_clean 后才能再来）
      2) running（别人）→ 已在队列则报位置，否则入队
      3) 空闲（无条目 / done / failed）→ 出队（若曾排队）→ 整体替换为
         当前实验的 running 条目（queue 保留）
    """
    key = f"{domain}:{device}"
    d = _read_cursor()
    e = d["entries"].get(key)

    if e and e.get("state") == "cleanup_failed":
        return f"rejected:cleanup_failed ({e.get('exit_reason')!r})"

    if e and e.get("state") == "running":
        queue: List[Dict] = e.setdefault("queue", [])
        for i, item in enumerate(queue):
            if item.get("exp_id") == exp_id:
                return f"queued:{i + 1}"
        queue.append({"exp_id": exp_id, "run_dir": run_dir,
                      "session_id": session_id, "agent_tool": agent_tool,
                      "enqueued_at": _now()})
        _write_cursor(d)
        return f"queued:{len(queue)}"

    # 空闲：出队（若曾排队）→ 整体替换为当前实验（queue 保留）
    queue = [q for q in (e or {}).get("queue", [])
             if q.get("exp_id") != exp_id]
    d["entries"][key] = {
        "domain": domain, "device": device, "state": "running",
        "exp_id": exp_id, "run_dir": run_dir,
        "session_id": session_id, "agent_tool": agent_tool,
        "pid": os.getpid(), "queue": queue,
        "heartbeat_ts": _now(), "claimed_at": _now(),
    }
    _write_cursor(d)
    return "running"


@_with_lock
def exp_lock_release(domain: str, device: str, exp_id: str, state: str,
                     reason: str = "") -> str:
    """干净路径放行（state ∈ done|failed）。锁释放；queue 保留（轮询方取队首）。

    防误释放：条目 exp_id 必须与调用者一致，否则拒绝。
    """
    key = f"{domain}:{device}"
    d = _read_cursor()
    e = d["entries"].get(key)
    if not e:
        return "warn:no-entry"
    if e.get("exp_id") != exp_id:
        return (f"rejected:exp_id mismatch (锁持有者是 {e.get('exp_id')!r} "
                f"session={e.get('session_id')!r}，你传的是 {exp_id!r})")
    e["state"] = state
    e["exit_reason"] = reason or ""
    e["finished_at"] = _now()
    _write_cursor(d)
    return f"ok:{state}"


@_with_lock
def exp_lock_mark_cleanup_failed(domain: str, device: str, exp_id: str,
                                 reason: str) -> str:
    """清理失败：state → cleanup_failed，锁保持占用（claim 被拒）。"""
    key = f"{domain}:{device}"
    d = _read_cursor()
    e = d["entries"].get(key)
    if not e:
        return "warn:no-entry"
    if e.get("exp_id") != exp_id:
        return (f"rejected:exp_id mismatch (锁持有者是 {e.get('exp_id')!r})")
    e["state"] = "cleanup_failed"
    e["exit_reason"] = reason
    e["finished_at"] = _now()
    _write_cursor(d)
    return "ok:cleanup_failed"


@_with_lock
def exp_lock_clean(domain: str, device: str, exp_id: str, reason: str) -> str:
    """人工确认清理干净后的放行（→ failed + manual_clean 标记）。"""
    key = f"{domain}:{device}"
    d = _read_cursor()
    e = d["entries"].get(key)
    if not e:
        return "warn:no-entry"
    if e.get("exp_id") != exp_id:
        return (f"rejected:exp_id mismatch (锁持有者是 {e.get('exp_id')!r})")
    e["state"] = "failed"
    e["exit_reason"] = f"manual_clean: {reason}"
    e["finished_at"] = _now()
    _write_cursor(d)
    return "ok:released"


@_with_lock
def exp_lock_heartbeat(domain: str, device: str, exp_id: str) -> str:
    key = f"{domain}:{device}"
    d = _read_cursor()
    e = d["entries"].get(key)
    if e and e.get("state") == "running" and e.get("exp_id") == exp_id:
        e["heartbeat_ts"] = _now()
        _write_cursor(d)
        return "ok"
    return "warn:not-running"


def exp_lock_get(domain: str, device: str) -> Optional[Dict]:
    """只读查询：不持 EX 锁（写侧 os.replace 原子替换保证一致性）。"""
    return _read_cursor()["entries"].get(f"{domain}:{device}")


def exp_lock_peek(domain: str, device: str) -> Optional[Dict]:
    e = exp_lock_get(domain, device)
    if e:
        q = e.get("queue") or []
        return q[0] if q else None
    return None


def exp_lock_list() -> Dict[str, Any]:
    return _read_cursor()


def exp_lock_poll_until_free(domain: str, device: str,
                             poll_s: float = 10.0,
                             max_wait_s: float = 43200) -> bool:
    """host 端轮询（纯本地文件读，设备零负载）：空闲 True，超时 False。"""
    deadline = time.monotonic() + max_wait_s
    while time.monotonic() < deadline:
        e = exp_lock_get(domain, device)
        if _entry_claimable(e):
            return True
        time.sleep(poll_s)
    return False


def exp_lock_status(domain: str, device: str) -> str:
    """人类可读状态行（log_view 看板/排查用）。"""
    e = exp_lock_get(domain, device)
    if not e:
        return f"({domain},{device}) 空闲"
    q = e.get("queue") or []
    qs = f" queue={len(q)}" if q else ""
    return (f"({domain},{device}) {e.get('state')} exp={e.get('exp_id')} "
            f"session={e.get('session_id') or 'manual'} "
            f"heartbeat={e.get('heartbeat_ts')}{qs}")
