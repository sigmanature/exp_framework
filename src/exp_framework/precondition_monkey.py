"""Monkey 打碎 precondition：以用户操作方式耗尽 buddy order2+ 内存。

慢节奏深交互（用户确认的形态）：
  对每个重内存应用：am start 点开 → monkey 单应用内深交互（throttle 500ms、
  appswitch 0——不切走）→ 驻留后台 → 切下一个应用。
  切换由脚本显式控制（不是 monkey 随机 appswitch），模拟"点开→玩→切走"。

网络控制（实测：airplane_mode 标记在 Pixel6 不断网，必须组件级）：
  打碎阶段：网络开（svc wifi enable）——应用加载内容，内存占用大
  打碎完成后：断网（svc wifi disable）——冷启动阶段等效飞行模式（排除网络因素）

注意：打碎应用（backend.packages 之外的专用重内存应用）与冷启动负载包必须
不相交——打碎时启动过的应用是热进程，后面冷启动测它们就不是冷启动了
（memstress.prepare 调用前会校验 disjoint）。
"""
from __future__ import annotations

import argparse
import sys
import time
from typing import List, Optional, Sequence

from exp_framework.utils import adb_utils
from exp_framework.utils.buddyinfo_utils import parse_buddyinfo

DEFAULT_ZONE = "Normal"
DEFAULT_EVENTS_PER_APP = 1200
DEFAULT_THROTTLE_MS = 60
DEFAULT_SEED = 42

# 重内存打碎应用（已确认设备安装；与 memstress 负载包不相交）
DEFAULT_FRAGMENT_APPS = [
    "com.ss.android.ugc.aweme",       # 抖音（先刷）
    "tv.danmaku.bili",                # bilibili（再点视频）
]


def _buddy_high_order_sum(serial: str, min_order: int = 2) -> int:
    """读 /proc/buddyinfo，返回 Normal zone 的 order>=min_order 页数之和。"""
    out = adb_utils.adb_shell_root(serial, "cat /proc/buddyinfo",
                                   timeout_s=15, check=False)
    zones = parse_buddyinfo(out)
    orders = zones.get(DEFAULT_ZONE, [])
    if len(orders) <= min_order:
        return 0
    return sum(orders[min_order:])


def set_network(serial: str, enabled: bool) -> None:
    """组件级断网/联网（飞行模式标记在 Pixel6 不断网，实测必须 svc）。"""
    cmd = "svc wifi enable" if enabled else "svc wifi disable"
    adb_utils.adb_shell_root(serial, cmd, timeout_s=15, check=False)
    if not enabled:
        adb_utils.adb_shell_root(serial, "svc data disable",
                                 timeout_s=15, check=False)
    print(f"[fragment] network {'ON' if enabled else 'OFF（冷启动等效飞行模式）'}",
          flush=True)


def _screen_size(serial: str):
    """读设备屏幕尺寸 (w, h)。"""
    out = adb_utils.adb_shell_root(serial, "wm size", timeout_s=15,
                                   check=False)
    for line in out.splitlines():
        if "Physical size" in line:
            w, h = line.split(":")[1].strip().split("x")
            return int(w), int(h)
    return 1080, 2400


def fragment_app(serial: str, pkg: str, events: int, seed: int,
                 throttle_ms: int) -> None:
    """点开应用（切换慢：3s 加载），自定义安全区域深交互。

    不用 monkey 随机事件——它的 motion 会从屏幕顶部下拉系统通知栏。
    自定义序列：60% 滑动（屏幕中部纵向，刷内容）+ 40% 点击
    （避开顶部 20% 状态栏/通知栏区与底部导航区）。
    """
    import random
    rng = random.Random(seed)

    adb_utils.adb_shell_root(
        serial, f"am start -n {pkg}", timeout_s=30, check=False)
    time.sleep(3)   # 切换慢：应用加载

    w, h = _screen_size(serial)
    top = int(h * 0.20)      # 避开顶部 20%（通知栏下拉区）
    bottom = int(h * 0.92)   # 避开底部导航
    for i in range(events):
        # 周期性检查前台应用：被回收/闪退回桌面时重新拉起，
        # 否则随机事件会点到桌面图标（电话/设置等——用户踩过）
        if i % 50 == 0:
            top_act = adb_utils.adb_shell_root(
                serial, "dumpsys activity activities | grep -m1 topResumedActivity",
                timeout_s=15, check=False)
            if pkg not in top_act:
                adb_utils.adb_shell_root(
                    serial, f"am start -n {pkg}", timeout_s=30, check=False)
                time.sleep(2)
        if rng.random() < 0.6:
            # 滑动（60%）：屏幕中部纵向——刷视频/信息流，不下拉通知栏
            x = w // 2 + rng.randint(-w // 4, w // 4)
            y1 = rng.randint(int(h * 0.65), int(h * 0.80))
            y2 = rng.randint(int(h * 0.25), int(h * 0.40))
            adb_utils.adb_shell_root(
                serial, f"input swipe {x} {y1} {x} {y2} 100",
                timeout_s=15, check=False)
        else:
            # 点击（40%）：内容区随机点
            x = rng.randint(int(w * 0.1), int(w * 0.9))
            y = rng.randint(top, bottom)
            adb_utils.adb_shell_root(
                serial, f"input tap {x} {y}", timeout_s=15, check=False)
        time.sleep(throttle_ms / 1000.0)


def fragment(serial: str, apps: Sequence[str], events_per_app: int,
             seed: int, throttle_ms: int, sample_interval_s: float = 0.5) -> int:
    """逐应用交互打碎（INTERACTION_MAP 分发：douyin 刷视频/bilibili 点视频/
    youtube 下滑+点视频/camera 拍照），应用驻留后台。返回打碎后 order2+。

    sample_interval_s: 打碎期间高频采样 order2+（观察碎片化速度）。
    """
    from exp_framework.utils.interactive import (INTERACTION_MAP,
                                                 interact_bilibili,
                                                 interact_camera,
                                                 interact_douyin,
                                                 interact_youtube)

    def _interact(pkg: str) -> None:
        kind = INTERACTION_MAP.get(pkg, "")
        try:
            if kind == "douyin":
                # 上下 swipe 刷视频（快手/抖音同模式），快节奏
                r = interact_douyin(serial, swipes=max(10, events_per_app // 40),
                                    gap_s=throttle_ms / 1000.0)
            elif kind == "bilibili":
                r = interact_bilibili(serial, clicks=max(4, events_per_app // 100),
                                      gap_s=throttle_ms / 1000.0)
            elif kind == "youtube":
                r = interact_youtube(serial, swipes=3, gap_s=0.8)
            elif pkg == "com.google.android.GoogleCamera":
                r = interact_camera(serial, shots=5)
            else:
                # 通用：启动 + 点弹窗 + 固定区域滑动（暂未精细化交互的应用）
                adb_utils.adb_shell_root(
                    serial, f"am start -n {pkg}", timeout_s=30, check=False)
                time.sleep(3)
                r = {"swiped": 0, "errors": 0}
        except Exception as e:
            r = {"errors": 1, "exc": str(e)[:100]}
        print(f"[fragment] {pkg} ({kind}) 交互完成: {r}", flush=True)

    before = _buddy_high_order_sum(serial)
    print(f"[fragment] 打碎前 order2+={before}", flush=True)
    import threading
    samples: List[int] = []
    stop_sampling = threading.Event()

    def _sampler():
        while not stop_sampling.wait(sample_interval_s):
            samples.append(_buddy_high_order_sum(serial))

    sampler_t = threading.Thread(target=_sampler, daemon=True)
    sampler_t.start()

    for pkg in apps:
        _interact(pkg)
        o2 = _buddy_high_order_sum(serial)
        print(f"[fragment] {pkg} 后 order2+={o2}", flush=True)

    stop_sampling.set()
    sampler_t.join(timeout=5)
    if samples:
        print(f"[fragment] 打碎期间采样曲线（{sample_interval_s}s 间隔）: {samples}",
              flush=True)
    after = _buddy_high_order_sum(serial)
    print(f"[fragment] 打碎完成 order2+={after} "
          f"（相对 {before} 降 {(before - after) / max(1, before) * 100:.0f}%）",
          flush=True)
    return after


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="monkey 慢节奏深交互打碎 buddy 内存")
    p.add_argument("--serial", required=True)
    p.add_argument("--apps", nargs="*", default=None,
                   help="打碎应用包列表（默认 7 个重内存应用）")
    p.add_argument("--events-per-app", type=int, default=DEFAULT_EVENTS_PER_APP)
    p.add_argument("--throttle-ms", type=int, default=DEFAULT_THROTTLE_MS)
    p.add_argument("--seed", type=int, default=DEFAULT_SEED)
    p.add_argument("--network-off-after", action="store_true",
                   help="打碎完成后断网（冷启动阶段等效飞行模式）")
    p.add_argument("--no-fragment", action="store_true",
                   help="只做网络控制（调试用）")
    args = p.parse_args(argv)

    adb_utils.ensure_privilege(args.serial)
    apps = list(args.apps) if args.apps else DEFAULT_FRAGMENT_APPS

    set_network(args.serial, enabled=True)   # 打碎阶段网络开（应用加载内容）
    if not args.no_fragment:
        fragment(args.serial, apps, args.events_per_app,
                 args.seed, args.throttle_ms)
    if args.network_off_after:
        set_network(args.serial, enabled=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
