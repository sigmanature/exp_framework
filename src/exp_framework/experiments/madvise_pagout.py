"""madvise_pagout 实验后端：4K/16K × kcompressd 开关，500MB/轮 × 100 轮/组。

框架职责：设备准备编排（ensure_awake）、tasktime/odpm/vmstat 采样、
信号清理、manifest 归档（sample_config 驱动）。
本后端职责：锁 max 覆盖、冷却、组参数设置与自检（sysctl_util.verify 一行）、
绑核 bench 循环、order2 采样、收尾等待（kcompressd 稳定双判据）、tsv 产物。

用法：python3 -m experiment.runner --serial <s> \\
            --from-config config/madvise_pagout_config.json --out <dir>
"""
import json
import os
import subprocess
import sys
import time
from typing import Dict, Any, List, Optional

from exp_framework.utils import adb_utils, device_nodes, sysctl_util

from exp_framework.experiment.experiment import Experiment, register

GROUPS = ["4k_nokcompressd", "4k_kcompressd",
          "16k_nokcompressd", "16k_kcompressd"]

THP_16K = "/sys/kernel/mm/transparent_hugepage/hugepages-16kB/enabled"
KCD = "/sys/module/kcompressd/parameters/kcompressd_enabled"
ZRAM = "/sys/block/zram0"
MP_STAT = ZRAM + "/multi_pages_stat"
MM_STAT = ZRAM + "/mm_stat"


def _kcd_pids(serial: str) -> List[str]:
    out = adb_utils.adb_shell_root(
        serial, "ps -A -o PID,NAME | grep '\\[kcompressd:' | awk '{print $1}'",
        timeout_s=15, check=False)
    return [ln.strip() for ln in out.splitlines() if ln.strip().isdigit()]


@register("madvise_pagout")
class MadvisePagout(Experiment):

    def _cfg(self, key: str, default=None):
        return self.backend_config.get(key, default)

    # ---------------- prepare ----------------

    def prepare(self) -> Dict[str, Any]:
        group = str(self._cfg("group", ""))
        if group not in GROUPS:
            raise RuntimeError(f"bad group {group!r} (expect {GROUPS})")
        self.group = group
        self.rounds = int(self._cfg("rounds", 100))
        self.pin_cpu = str(self._cfg("pin_cpu", "0x80"))
        self.bench_remote = str(self._cfg(
            "bench_remote", "/data/local/tmp/madvise_pagout_bench"))

        # 1) 锁 max（与历史实验一致：scaling_min = scaling_max，8 核）
        self._lock_max()

        # 2) 节点设置+回读+自检：一行，全走框架（config 里已配 _ctx + sysctl_nodes，
        #    每个组一份配置文件，组旋钮期望值已在文件里写死）
        results = sysctl_util.verify(self.global_config)
        if any(not r["ok"] for r in results):
            raise RuntimeError("sysctl verify failed: "
                               + str([r["param"] for r in results if not r["ok"]]))

        # 3) zram swap 确保启用（verify 只写了 disksize，swapon 是命令需单独做；
        #    重启后 swap off 会导致 bench MADV_PAGEOUT 无空间、orig_delta=0）
        self._ensure_zram()

        # 3) bench 编译推送
        self._build_and_push_bench()

        baseline = adb_utils.adb_shell_root(
            self.serial, f"cat {MP_STAT}", timeout_s=15, check=False).strip()
        print(f"[{self.serial}] group={group} rounds={self.rounds} "
              f"pin={self.pin_cpu} baseline_mp={baseline}", file=sys.stderr)
        return {"group": group, "rounds": self.rounds,
                "pin_cpu": self.pin_cpu}

    # ---------------- run ----------------

    def run(self) -> Dict[str, Any]:
        tsv = self.work_dir / f"{self.group}.tsv"
        with tsv.open("w", encoding="utf-8") as f:
            f.write("group round write_s pageout_s order2_large_pages "
                    "orig_delta compr_delta huge_delta\n")

        rounds_done = 0
        for r in range(1, self.rounds + 1):
            if self.stop_event.is_set():
                print(f"[{self.serial}] stop requested, break at round {r}",
                      file=sys.stderr)
                break
            out = adb_utils.adb_shell_root(
                self.serial,
                f"taskset {self.pin_cpu} {self.bench_remote} --rounds 1",
                timeout_s=120, check=False)
            fields = self._parse_bench(out)
            order2 = adb_utils.adb_shell_root(
                self.serial, f"awk '{{print $2}}' {MP_STAT}",
                timeout_s=15, check=False).strip()
            if not fields or not order2:
                print(f"[{self.serial}] round {r} parse fail:\n{out}",
                      file=sys.stderr)
                continue
            with tsv.open("a", encoding="utf-8") as f:
                f.write(f"{self.group} {r} {fields} {order2}\n")
            rounds_done = r
            if r % 20 == 0:
                print(f"[{self.serial}] {self.group} round {r}/{self.rounds} "
                      f"pageout={fields.split()[1]}s", file=sys.stderr)

        # 收尾等待已删除：压缩收尾窗口对能耗影响 <2%（实测 tail 占比），
        # 且 sample_end 的 tasktime/odpm 终止时机不依赖它
        return {"rounds_done": rounds_done, "tsv": str(tsv)}

    # ---------------- 工具 ----------------

    def _ensure_zram(self) -> None:
        swaps = adb_utils.adb_shell_root(
            self.serial, "grep zram0 /proc/swaps", timeout_s=15, check=False)
        if "zram0" in swaps:
            return
        for c in ("echo 8G > /sys/block/zram0/disksize",
                  "mkswap /dev/block/zram0",
                  "swapon -p 100 /dev/block/zram0"):
            adb_utils.adb_shell_root(self.serial, c, timeout_s=30, check=False)
        got = adb_utils.adb_shell_root(
            self.serial, "grep zram0 /proc/swaps", timeout_s=15, check=False)
        if "zram0" not in got:
            raise RuntimeError("zram swap 启用失败")

    def _lock_max(self) -> None:
        # 锁频目标（默认锁 max）：锁频前先读每核原始 scaling_max_freq（未锁时
        # 即为该核硬件最大频率）作为目标，整段实验频率固定不变。
        # 注意：锁 max 可能触发用户态 thermal HAL 压频（BIG 2802→1745MHz），
        # 温度冲高时测量分段会劣化；若需保守档位，用 backend_config["lock_freq"]
        # = {"little": kHz, "mid": kHz, "big": kHz} 覆盖。
        lf = self._cfg("lock_freq", {}) or {}
        target = []
        for i in range(8):
            cluster = "little" if i < 4 else ("mid" if i < 6 else "big")
            if cluster in lf:
                target.append(int(lf[cluster]))
            else:
                p = f"/sys/devices/system/cpu/cpu{i}/cpufreq"
                raw = [device_nodes.read_node(self.serial, p + "/scaling_max_freq")
                       for _ in range(3)]
                target.append(int(max(set(raw), key=raw.count)))
        for i, freq in enumerate(target):
            p = f"/sys/devices/system/cpu/cpu{i}/cpufreq"
            device_nodes.set_node(self.serial, p + "/scaling_max_freq", freq)
            device_nodes.set_node(self.serial, p + "/scaling_min_freq", freq)

        # 自检（adb 读偶发脏值：每核读 3 次取多数值）
        def read_majority(path: str) -> str:
            vals = [device_nodes.read_node(self.serial, path)
                    for _ in range(3)]
            return max(set(vals), key=vals.count)

        ok = all(
            read_majority(f"/sys/devices/system/cpu/cpu{i}/cpufreq/scaling_min_freq")
            == read_majority(f"/sys/devices/system/cpu/cpu{i}/cpufreq/scaling_max_freq")
            for i in range(8))
        print(f"[{self.serial}] lock_cpu_freq min==max: {ok} "
              f"target={target}", file=sys.stderr)

        # 防热降频（内核侧）：CPU zone passive trip 提到 115°C（shutdown 保留）
        for z in ("thermal_zone0", "thermal_zone1", "thermal_zone2"):
            device_nodes.set_node(self.serial,
                                  f"/sys/class/thermal/{z}/trip_point_2_temp",
                                  115000)
        trips = [device_nodes.read_node(self.serial,
                  f"/sys/class/thermal/{z}/trip_point_2_temp")
                 for z in ("thermal_zone0", "thermal_zone1", "thermal_zone2")]
        trips_ok = all(t == "115000" for t in trips)
        print(f"[{self.serial}] disable_thermal_downclock trip=115000: "
              f"{trips_ok} ({trips})", file=sys.stderr)

        if not (ok and trips_ok):
            print(f"[{self.serial}] WARN: 锁频/防降频自检未全过",
                  file=sys.stderr)

    def _build_and_push_bench(self) -> None:
        src = str(self._cfg("bench_src"))
        out_local = str(self._cfg("bench_out"))
        ndk = os.environ.get("NDK",
                             "/home/nzzhao/android-sdk/ndk/android-ndk-r27d")
        cc = (f"{ndk}/toolchains/llvm/prebuilt/linux-x86_64/bin/"
              "aarch64-linux-android28-clang")
        if not os.path.exists(cc):
            raise RuntimeError(f"clang not found: {cc}")
        subprocess.run([cc, "-O2", "-Wall", "-Wextra", "-o", out_local, src],
                       check=True)
        subprocess.run(["adb", "-s", self.serial, "push", out_local,
                        self.bench_remote], check=True)
        adb_utils.adb_shell_root(self.serial, f"chmod 755 {self.bench_remote}",
                                 timeout_s=15, check=False)

    @staticmethod
    def _parse_bench(out: str) -> Optional[str]:
        """bench 输出 -> 'write_s pageout_s orig_d compr_d huge_d'
        round N fill=X pageout=Y orig_delta=A compr_delta=B huge_delta=C
        parts: [round, N, fill=X, pageout=Y, orig_delta=A, compr_delta=B, huge_delta=C]"""
        for ln in out.splitlines():
            if not ln.startswith("round "):
                continue
            parts = ln.split()
            if len(parts) < 7:
                return None
            try:
                write_s = parts[2].split("=")[1]
                pageout_s = parts[3].split("=")[1]
                vals = {p.split("=")[0]: p.split("=")[1]
                        for p in parts[4:] if "=" in p}
                return (f"{write_s} {pageout_s} "
                        f"{vals.get('orig_delta', '-1')} "
                        f"{vals.get('compr_delta', '-1')} "
                        f"{vals.get('huge_delta', '-1')}")
            except (IndexError, ValueError):
                return None
        return None

