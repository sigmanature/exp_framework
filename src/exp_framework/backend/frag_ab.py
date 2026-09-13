"""frag_ab 实验后端：持续碎片压力下 kfragd 的压分行为。

- source=fragmem : fragmem(4K 棋盘, threshold=0) 起-hold-停 循环制造锯齿压力
- source=douyin  : 抖音持续 swipe（网络开）

压分指标不在这里算：kfragd 的 kround/时间戳从 trace_main.txt 取
（derive_metrics 的 frag_ab 段），vmstat/buddyinfo/pagetypeinfo/tasktime
由采样层（统一 probe 采样器）负责。
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List

from exp_framework.experiment.experiment import Experiment, register
from exp_framework.fragmem_host import (FRAGMEM_BINARY, push_fragmem,
                                        start_fragmem, stop_fragmem)
from exp_framework.prefrag import set_network
from exp_framework.utils import adb_utils
from exp_framework.utils.signal_utils import sleep_interruptible

DOUYIN = "com.ss.android.ugc.aweme"
DOUYIN_ACT = "com.ss.android.ugc.aweme/.splash.SplashActivity"
SWIPE = (540, 1800, 540, 600, 300)


def _fragmem_binary() -> Path:
    env = os.environ.get("FRAGMEM_BIN")
    candidates: List[Path] = []
    if env:
        candidates.append(Path(env))
    candidates.append(FRAGMEM_BINARY)
    candidates.append(Path(__file__).resolve().parents[3]
                      / "scripts" / "fragmem" / "fragmem")
    for path in candidates:
        if path.exists():
            return path
    raise RuntimeError("fragmem binary not found; tried: "
                       + ", ".join(str(p) for p in candidates))


@register("frag_ab")
class FragAb(Experiment):

    def _cfg(self, key: str, default=None):
        return self.backend_config.get(key, default)

    def prepare(self) -> Dict[str, Any]:
        self.source = str(self._cfg("source", "fragmem"))
        if self.source not in ("fragmem", "douyin"):
            raise RuntimeError(f"frag_ab: bad source {self.source!r}")
        self.duration_s = float(self._cfg("duration_s", 180))
        self.alloc_mb = int(self._cfg("fragmem_alloc_mb", 4000))
        self.hold_s = float(self._cfg("fragmem_cycle_s", 15))
        self.gap_s = float(self._cfg("douyin_gap_s", 0.3))
        self.fragmem_loop = bool(self._cfg("fragmem_loop_on_device", False))
        self.proact_keeper = int(self._cfg("proact_keeper", 0) or 0)
        if self.source != "fragmem" and self.fragmem_loop:
            raise RuntimeError("frag_ab: fragmem_loop_on_device 仅用于 fragmem 源")
        self.fragmem_bin = None
        if self.source == "fragmem":
            self.fragmem_bin = _fragmem_binary()
            push_fragmem(self.serial, self.fragmem_bin)
        else:
            out = adb_utils.adb_shell(self.serial,
                                      f"pm list packages {DOUYIN}",
                                      timeout_s=20, check=False)
            if DOUYIN not in str(out):
                raise RuntimeError(f"frag_ab: {DOUYIN} not installed")
        return {"frag_ab": {"source": self.source,
                            "duration_s": self.duration_s,
                            "alloc_mb": self.alloc_mb,
                            "fragmem_cycle_s": self.hold_s,
                            "douyin_gap_s": self.gap_s,
                            "fragmem_loop_on_device": self.fragmem_loop,
                            "proact_keeper": self.proact_keeper}}

    def precondition(self) -> Dict[str, Any]:
        return {}

    # ---------------- 压力源 ----------------

    def _fragmem_burst(self) -> None:
        start_fragmem(self.serial, alloc_mb=self.alloc_mb, chunk_kb=4,
                      stride=2, threshold=0, quiet=True, timeout_s=300)
        sleep_interruptible(self.stop_event, self.hold_s)
        stop_fragmem(self.serial)

    _LOOP_SH = "/data/local/tmp/fragmem_loop.sh"
    _LOOP_STOP = "/data/local/tmp/fragmem_loop_stop"
    _LOOP_CYC = "/data/local/tmp/fragmem_loop_cycles"

    _PROACT_SH = "/data/local/tmp/proact_keep.sh"
    _PROACT_STOP = "/data/local/tmp/proact_keep_stop"

    def _start_proact_keeper(self) -> None:
        script = f"""#!/system/bin/sh
STOP={self._PROACT_STOP}
PID=/data/local/tmp/proact_keep_pid
rm -f "$STOP" "$PID"
echo $$ > "$PID"
while [ ! -e "$STOP" ]; do
  echo {self.proact_keeper} > /proc/sys/vm/compaction_proactiveness
  sleep 0.1
done
rm -f "$PID"
"""
        local = Path("/tmp/opencode/proact_keep.sh")
        local.write_text(script, encoding="utf-8")
        subprocess.run(["adb", "-s", self.serial, "push", str(local),
                        self._PROACT_SH], capture_output=True, timeout=30)
        adb_utils.adb_shell_root(self.serial, f"chmod 755 {self._PROACT_SH}",
                                 timeout_s=15, check=False)
        adb_utils.adb_shell_root(
            self.serial,
            f"(setsid sh {self._PROACT_SH} </dev/null >/dev/null 2>&1 &)",
            timeout_s=15, check=False)

    def _stop_proact_keeper(self) -> None:
        adb_utils.adb_shell_root(self.serial, f"touch {self._PROACT_STOP}",
                                 timeout_s=15, check=False)
        deadline = time.time() + 15
        while time.time() < deadline:
            alive = adb_utils.adb_shell_root(
                self.serial,
                f"pid=$(cat /data/local/tmp/proact_keep_pid 2>/dev/null); "
                f"[ -n \"$pid\" ] && kill -0 $pid 2>/dev/null && echo ALIVE",
                timeout_s=15, check=False).strip()
            if "ALIVE" not in alive:
                break
            sleep_interruptible(self.stop_event, 1)
        adb_utils.adb_shell_root(
            self.serial, f"rm -f {self._PROACT_STOP} "
                         f"/data/local/tmp/proact_keep_pid",
            timeout_s=15, check=False)

    def _start_fragmem_loop(self) -> None:
        # 防御：启动前清掉历史残留（循环进程/子 fragmem/状态文件）
        adb_utils.adb_shell_root(
            self.serial,
            f"pkill -f '[f]ragmem_loop.sh' 2>/dev/null; killall fragmem 2>/dev/null; "
            f"rm -f {self._LOOP_STOP} {self._LOOP_CYC} "
            f"/data/local/tmp/fragmem_loop_pid /data/local/tmp/proact_keep_pid",
            timeout_s=15, check=False)
        script = f"""#!/system/bin/sh
BIN=/data/local/tmp/fragmem
STOP={self._LOOP_STOP}
CYC={self._LOOP_CYC}
PID=/data/local/tmp/fragmem_loop_pid
rm -f "$STOP" "$PID"
echo $$ > "$PID"
i=0
while [ ! -e "$STOP" ]; do
  i=$((i + 1))
  echo "$i" > "$CYC"
  "$BIN" --alloc-mb {self.alloc_mb} --chunk-kb 4 --stride 2 \\
     --threshold 0 --quiet &
  end=$(( $(date +%s) + {int(self.hold_s)} ))
  while [ ! -e "$STOP" ] && [ "$(date +%s)" -lt "$end" ]; do sleep 0.2; done
  killall fragmem 2>/dev/null
  sleep 1
done
rm -f "$PID"
"""
        local = Path("/tmp/opencode/fragmem_loop.sh")
        local.write_text(script, encoding="utf-8")
        subprocess.run(["adb", "-s", self.serial, "push", str(local),
                        self._LOOP_SH], capture_output=True, timeout=30)
        adb_utils.adb_shell_root(self.serial, f"chmod 755 {self._LOOP_SH}",
                                 timeout_s=15, check=False)
        adb_utils.adb_shell_root(
            self.serial,
            f"(setsid sh {self._LOOP_SH} </dev/null >/dev/null 2>&1 &)",
            timeout_s=15, check=False)

    def _stop_fragmem_loop(self) -> int:
        """touch STOP → 确认循环进程退出、fragmem 清空 → 才删状态文件。

        未确认前绝不删 STOP（trace_utils.stop_trace_probe 同款语义）。
        残留无法确认时抛 RuntimeError（走 cleanup_failed 路径，gate 可见）。
        """
        problems: List[str] = []
        adb_utils.adb_shell_root(self.serial, f"touch {self._LOOP_STOP}",
                                 timeout_s=15, check=False)
        deadline = time.time() + int(self.hold_s) + 10
        loop_gone = False
        while time.time() < deadline:
            alive = adb_utils.adb_shell_root(
                self.serial,
                f"pid=$(cat /data/local/tmp/fragmem_loop_pid 2>/dev/null); "
                f"[ -n \"$pid\" ] && kill -0 $pid 2>/dev/null && echo ALIVE",
                timeout_s=15, check=False).strip()
            if "ALIVE" not in alive:
                loop_gone = True
                break
            sleep_interruptible(self.stop_event, 1)
        out = adb_utils.adb_shell_root(
            self.serial, f"cat {self._LOOP_CYC} 2>/dev/null",
            timeout_s=15, check=False)
        cycles = int(str(out).strip()) if str(out).strip().isdigit() else -1
        adb_utils.adb_shell_root(self.serial, f"killall fragmem 2>/dev/null",
                                 timeout_s=15, check=False)
        sleep_interruptible(self.stop_event, 1)
        frag_deadline = time.time() + 10
        while time.time() < frag_deadline:
            left = adb_utils.adb_shell_root(
                self.serial, "pidof fragmem", timeout_s=15,
                check=False).strip()
            if not left:
                break
            adb_utils.adb_shell_root(self.serial,
                                     "killall -9 fragmem 2>/dev/null",
                                     timeout_s=15, check=False)
            sleep_interruptible(self.stop_event, 1)
        else:
            problems.append(f"fragmem 残留: pidof={left!r}")
        if not loop_gone:
            problems.append("fragmem_loop.sh 残留（STOP 确认超时）")
        if problems:
            raise RuntimeError("frag_ab stop 残留: " + "; ".join(problems))
        adb_utils.adb_shell_root(
            self.serial,
            f"rm -f {self._LOOP_STOP} {self._LOOP_CYC} "
            f"/data/local/tmp/fragmem_loop_pid",
            timeout_s=15, check=False)
        return cycles

    def _douyin_start(self) -> None:
        set_network(self.serial, enabled=True)
        adb_utils.adb_shell_root(self.serial, f"am force-stop {DOUYIN}",
                                 timeout_s=15, check=False)
        adb_utils.adb_shell_root(self.serial, f"am start -n {DOUYIN_ACT}",
                                 timeout_s=30, check=False)
        sleep_interruptible(self.stop_event, 4.0)

    def _douyin_foreground(self) -> bool:
        out = adb_utils.adb_shell_root(
            self.serial,
            "dumpsys activity activities | grep -m1 topResumedActivity",
            timeout_s=20, check=False)
        return DOUYIN in str(out)

    def _douyin_swipe(self) -> None:
        from exp_framework.utils.interactive import _swipe
        _swipe(self.serial, *SWIPE)
        sleep_interruptible(self.stop_event, self.gap_s)

    def _stop_source(self) -> None:
        try:
            if self.source == "fragmem":
                if getattr(self, "fragmem_loop", False):
                    self._stop_fragmem_loop()
                else:
                    stop_fragmem(self.serial)
            else:
                adb_utils.adb_shell_root(self.serial,
                                         f"am force-stop {DOUYIN}",
                                         timeout_s=15, check=False)
                set_network(self.serial, enabled=False)
        except Exception:
            pass

    # ---------------- 主循环 ----------------

    def run(self) -> Dict[str, Any]:
        self.work_dir.mkdir(parents=True, exist_ok=True)
        if self.source == "douyin":
            self._douyin_start()

        t0 = time.monotonic()
        deadline = t0 + self.duration_s
        cycles = 0
        if self.proact_keeper > 0:
            self._start_proact_keeper()
        try:
            if self.source == "douyin":
                while not self.stop_event.is_set() and time.monotonic() < deadline:
                    if cycles % 50 == 0 and not self._douyin_foreground():
                        self._douyin_start()
                    self._douyin_swipe()
                    cycles += 1
                self._stop_source()
            elif self.fragmem_loop:
                self._start_fragmem_loop()
                while (not self.stop_event.is_set()
                       and time.monotonic() < deadline):
                    sleep_interruptible(self.stop_event, min(5.0,
                                             deadline - time.monotonic()))
                cycles = self._stop_fragmem_loop()
            else:
                while not self.stop_event.is_set() and time.monotonic() < deadline:
                    self._fragmem_burst()
                    cycles += 1
                self._stop_source()
        finally:
            if self.proact_keeper > 0:
                self._stop_proact_keeper()
        elapsed = time.monotonic() - t0

        meta = {"source": self.source, "duration_s": self.duration_s,
                "elapsed_s": round(elapsed, 3), "cycles": cycles,
                "alloc_mb": self.alloc_mb, "fragmem_cycle_s": self.hold_s,
                "fragmem_loop_on_device": self.fragmem_loop,
                "proact_keeper": self.proact_keeper,
                "douyin_gap_s": self.gap_s}
        (self.work_dir / "fragab_meta.json").write_text(
            json.dumps(meta, indent=2) + "\n", encoding="utf-8")
        return {"frag_ab": meta}

    # ---------------- 收尾 ----------------

    def stop_device(self) -> None:
        self._stop_source()

    def device_residuals(self, serial: str) -> List[str]:
        problems: List[str] = []
        for name, pidfile in (("fragmem_loop.sh",
                               "/data/local/tmp/fragmem_loop_pid"),
                              ("proact_keep.sh",
                               "/data/local/tmp/proact_keep_pid")):
            out = adb_utils.adb_shell_root(
                serial,
                f"pid=$(cat {pidfile} 2>/dev/null); "
                f"[ -n \"$pid\" ] && kill -0 $pid 2>/dev/null && echo RESIDUAL",
                timeout_s=15, check=False).strip()
            if "RESIDUAL" in out:
                problems.append(f"{name} 残留(pidfile 存活)")
        frag = adb_utils.adb_shell_root(serial, "pidof fragmem",
                                        timeout_s=15, check=False).strip()
        if frag:
            problems.append(f"fragmem 残留: {frag[:60]}")
        for f in (self._LOOP_STOP, self._LOOP_CYC, self._PROACT_STOP,
                  "/data/local/tmp/fragmem_loop_pid",
                  "/data/local/tmp/proact_keep_pid"):
            if adb_utils.adb_shell_root(serial, f"test -e {f} && echo Y",
                                        timeout_s=15,
                                        check=False).strip():
                problems.append(f"状态文件残留: {f}")
        return problems

    def cleanup(self) -> None:
        # 兜底：循环/子进程残留一律清掉（含信号中止路径）。
        # 注意：禁止裸 pkill -f（会自匹配误杀包装 shell）；用 pidfile/括号模式。
        try:
            adb_utils.adb_shell_root(
                self.serial,
                f"for p in $(cat /data/local/tmp/fragmem_loop_pid 2>/dev/null) "
                f"$(cat /data/local/tmp/proact_keep_pid 2>/dev/null); do "
                f"kill -9 $p 2>/dev/null; done; "
                f"pkill -f '[f]ragmem_loop.sh' 2>/dev/null; "
                f"pkill -f '[p]roact_keep.sh' 2>/dev/null; "
                f"killall -9 fragmem 2>/dev/null; "
                f"rm -f {self._LOOP_STOP} {self._LOOP_CYC} "
                f"{self._PROACT_STOP} /data/local/tmp/fragmem_loop_pid "
                f"/data/local/tmp/proact_keep_pid",
                timeout_s=20, check=False)
        except Exception:
            pass
        self._stop_source()