"""周期采样统一管理（cycle_sample）：任何按固定间隔周期采集的样本。

设计（Kustomize 式组装模型的一部分）：
- 每个周期采样器 = cycle_sample 的一个子域（counters/vmstat/buddyinfo/thermal/cpufreq），
  各自有独立配置（enabled/interval_s/字段/输出文件），配置来自
  config/default_sample_config.json 模板与 manifest sample_config 差异的深合并。
- 每个子采样器一个线程 + 一个独立 CSV；统一启动（start_cycle_samplers），
  统一停止（共享 stop_event，调用方 join）。
- 新增采样器 = 注册 loop 函数 + 模板加一段配置，框架代码零改动。

loop 函数统一签名：
  loop(*, serial, out_csv, interval_s, stop_event, **cfg_extra)
"""
from __future__ import annotations

import csv
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

from exp_framework.utils import adb_utils
from exp_framework.utils.signal_utils import sleep_interruptible

# ---------------- thermal / cpufreq 采样循环 ----------------

def thermal_sample_loop(
    *,
    serial: str,
    out_csv: Path,
    interval_s: int,
    stop_event,
    zones: Optional[Sequence[str]] = None,
) -> int:
    """周期采集热区温度（/sys/class/thermal/<zone>/temp，毫摄氏度转 °C）。"""
    zones = list(zones or ["thermal_zone0", "thermal_zone2"])
    fieldnames = ["host_ts"] + [f"temp_{z.split('_')[-1]}" for z in zones]
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    num = 0

    with out_csv.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        next_t = time.time()

        while True:
            if stop_event is not None and stop_event.is_set():
                break
            now = time.time()
            if now < next_t:
                sleep_interruptible(stop_event, min(next_t - now, 1.0))
                continue
            row = {"host_ts": int(time.time())}
            try:
                out = adb_utils.adb_shell_root(
                    serial,
                    "for z in %s; do cat /sys/class/thermal/$z/temp 2>/dev/null; done"
                    % " ".join(zones),
                    timeout_s=10, check=False)
                vals = [t for t in str(out).split()
                        if t.replace(".", "").isdigit()][:len(zones)]
                for i, z in enumerate(zones):
                    if i < len(vals):
                        row[f"temp_{z.split('_')[-1]}"] = float(vals[i]) / 1000.0
            except Exception:
                pass
            w.writerow(row)
            f.flush()
            num += 1
            next_t += max(1, interval_s)
    return num


def cpufreq_sample_loop(
    *,
    serial: str,
    out_csv: Path,
    interval_s: int,
    stop_event,
    cpus: Optional[Sequence[int]] = None,
) -> int:
    """周期采集各核当前频率（scaling_cur_freq，kHz）。"""
    cpus = list(cpus or range(8))
    fieldnames = ["host_ts"] + [f"freq_cpu{i}" for i in cpus]
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    num = 0

    with out_csv.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        next_t = time.time()

        while True:
            if stop_event is not None and stop_event.is_set():
                break
            now = time.time()
            if now < next_t:
                sleep_interruptible(stop_event, min(next_t - now, 1.0))
                continue
            row = {"host_ts": int(time.time())}
            try:
                out = adb_utils.adb_shell_root(
                    serial,
                    "for i in %s; do "
                    "cat /sys/devices/system/cpu/cpu$i/cpufreq/scaling_cur_freq "
                    "2>/dev/null; done" % " ".join(str(c) for c in cpus),
                    timeout_s=10, check=False)
                vals = [t for t in str(out).split()
                        if t.replace(".", "").isdigit()][:len(cpus)]
                for i, cpu in enumerate(cpus):
                    if i < len(vals):
                        row[f"freq_cpu{cpu}"] = int(float(vals[i]))
            except Exception:
                pass
            w.writerow(row)
            f.flush()
            num += 1
            next_t += max(1, interval_s)
    return num


# ---------------- 注册表 ----------------

def vmstat_loop(*, serial, out_csv, interval_s, stop_event, keys=None):
    from exp_framework.utils.vmstat_utils import vmstat_sample_loop
    return vmstat_sample_loop(serial=serial, out_csv=out_csv,
                              interval_s=interval_s, stop_event=stop_event,
                              keys=keys)


def buddyinfo_loop(*, serial, out_csv, interval_s, stop_event):
    from exp_framework.utils.buddyinfo_utils import buddyinfo_sample_loop
    return buddyinfo_sample_loop(serial=serial, out_csv=out_csv,
                                 interval_s=interval_s, stop_event=stop_event)


def counters_loop(*, serial, out_csv, interval_s, stop_event, keys=None,
                  stats_dir="/data/local/tmp/memstress_stats",
                  retries=2, retry_sleep_s=2):
    from exp_framework.utils.sampling_utils import sample_loop
    return sample_loop(serial=serial, stats_dir=stats_dir, counters=keys,
                       interval_s=interval_s, out_csv=out_csv,
                       retries=retries, retry_sleep_s=retry_sleep_s,
                       stop_event=stop_event)


def _wrap(loop_fn: Callable, extra: Dict[str, str]) -> Callable:
    """把 loop 函数包装成统一启动签名：start(serial, out_dir, cfg, stop_event)。"""
    def start(serial: str, out_dir: Path, cfg: Dict, stop_event) -> int:
        kwargs = {
            "serial": serial,
            "out_csv": out_dir / str(cfg.get("out", "samples.csv")),
            "interval_s": int(cfg.get("interval_s", 60)),
            "stop_event": stop_event,
        }
        for kw, key in extra.items():
            if key in cfg and cfg[key] not in (None, ""):
                kwargs[kw] = cfg[key]
        return loop_fn(**kwargs)
    return start


SAMPLER_REGISTRY: Dict[str, Callable] = {
    "vmstat": _wrap(vmstat_loop, {"keys": "keys"}),
    "buddyinfo": _wrap(buddyinfo_loop, {}),
    "thermal": _wrap(thermal_sample_loop, {"zones": "zones"}),
    "cpufreq": _wrap(cpufreq_sample_loop, {"cpus": "cpus"}),
    "counters": _wrap(counters_loop, {"keys": "keys"}),
}


def start_cycle_samplers(
    serial: str,
    out_dir: Path,
    cycle_cfg: Dict,
    stop_event,
    result_sink: Optional[Dict] = None,
) -> List[threading.Thread]:
    """按 cycle_sample 配置启动所有启用的采样线程（enabled 且 interval_s>0）。

    result_sink：可选 dict，线程正常退出时写入 {name: loop返回值}；
    异常退出写入 {name: None}（供调用方读取采样统计，如 counters 的样本数）。
    """
    threads: List[threading.Thread] = []

    for name, sampler_cfg in (cycle_cfg or {}).items():
        if not isinstance(sampler_cfg, dict):
            continue
        start_fn = SAMPLER_REGISTRY.get(name)
        if start_fn is None:
            continue
        if not sampler_cfg.get("enabled", False):
            continue
        interval = int(sampler_cfg.get("interval_s", 0))
        if interval <= 0:
            continue

        def _run(name=name, sampler_cfg=sampler_cfg):
            try:
                result = SAMPLER_REGISTRY[name](serial, out_dir,
                                                sampler_cfg, stop_event)
                if result_sink is not None:
                    result_sink[name] = result
            except Exception as e:
                print(f"[{serial}] cycle_sample[{name}] error: {e}",
                      file=__import__("sys").stderr)
                if result_sink is not None:
                    result_sink[name] = None

        t = threading.Thread(
            target=_run, name=f"cycle_sample_{name}_{serial}", daemon=True)
        t.start()
        threads.append(t)
        print(f"[{serial}] cycle_sample[{name}] started "
              f"(interval={interval}s -> {sampler_cfg.get('out')})",
              file=__import__("sys").stderr)
    return threads
