"""Experiment backend base class and registry.

实验后端基类与注册表。任何实验（memstress、micro bench、……）只要：
  1. 继承 Experiment
  2. 实现 run()（主循环，必须响应 stop_event）
  3. @register("<name>") 注册
即可接入统一前端（采样、设备准备、信号清理、结果归档全部由框架提供）。
"""
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Dict, Any, List
import threading

REGISTRY: Dict[str, type] = {}


def register(name: str):
    """类装饰器：把后端类注册到 REGISTRY，name 即 config 中 backend.name。"""
    def deco(cls):
        cls.name = name
        REGISTRY[name] = cls
        return cls
    return deco


def create_experiment(name: str, backend_config: Dict[str, Any],
                      global_config: Dict[str, Any],
                      serial: str, out_dir: Path,
                      stop_event: threading.Event) -> "Experiment":
    """按 name 实例化后端。"""
    if name not in REGISTRY:
        raise RuntimeError(f"unknown experiment: {name!r} "
                           f"(registered: {sorted(REGISTRY)})")
    return REGISTRY[name](backend_config, global_config, serial, out_dir,
                          stop_event)


class Experiment(ABC):
    """实验后端基类。框架流程：prepare -> sample_start -> run -> sample_end。"""

    name: str = ""

    def __init__(self, backend_config: Dict[str, Any],
                 global_config: Dict[str, Any],
                 serial: str, out_dir: Path,
                 stop_event: threading.Event):
        self.backend_config = backend_config   # config["backend"]["config"]
        self.global_config = global_config     # config 顶层（采样/全局参数）
        self.serial = serial
        self.out_dir = out_dir                 # 实验输出目录
        self.stop_event = stop_event           # 置位后 run() 必须尽快退出
        self.work_dir = out_dir / self.name    # 后端产物目录（自动创建）
        self.work_dir.mkdir(parents=True, exist_ok=True)

    @abstractmethod
    def prepare(self) -> Dict[str, Any]:
        """预检与准备（设备准备、包解析、事件校验等）。

        返回需要写进 run_manifest.json 的私有字段（如 packages_resolved）。
        失败抛 RuntimeError -> 实验在采样启动前中止。
        每轮运行只执行一次（precondition 属于独立 hook，见 precondition()）。
        """

    def precondition(self) -> Dict[str, Any]:
        """可选：制造实验前置状态（碎片化/打碎等），由框架按模式调度。

        两种调度模式（config["config"]["precondition"]["mode"]）：
        - once       ：所有 round 前只跑一次（prepare 之后、第一轮采样之前）
        - per_round  ：每个 round 开始前都跑一次
        precondition 与 prepare 不同——prepare 做设备准备（唤醒/锁频/包解析），
        每轮运行只执行一次；precondition 制造内存初始状态，按模式可反复执行。
        返回值并入当轮 run_manifest.json（如 fragment 结果）。
        """
        return {}

    def assign_work_dir(self, work_dir: Path) -> None:
        """切换后端产物目录（多 round 实验：每轮一个子目录）。

        默认 work_dir = out_dir / <backend.name>；多 round 时框架在每轮开始前
        调用本方法，把 run() 产物落到 round_N/<backend.name>。
        """
        self.work_dir = work_dir
        self.work_dir.mkdir(parents=True, exist_ok=True)

    def device_residuals(self, serial: str) -> List[str]:
        """可选：后端自身的设备端残留检查（结束后由框架合并上报）。

        后端在自己层里检查自己部署的东西（如 device runner 进程/标记文件），
        框架统一调用；返回空列表 = 无残留。采样设施（tasktime/trace probe）
        的残留由 sample 层负责，不在这里。
        """
        return []

    @abstractmethod
    def run(self) -> Dict[str, Any]:
        """主循环。必须周期性检查 self.stop_event，置位即尽早返回。

        返回值并入 run_manifest.json（如 launch_failures/timing/rounds）。
        """

    def stop_device(self) -> None:
        """可选：信号/清理时立即停止设备端运行（不等 run() 轮询）。

        框架在 SIGINT/--stop/异常清理时调用；后端实现自己的设备端停止
        （如 touch 设备端 STOP 文件）。
        """

    def cleanup(self) -> None:
        """可选钩子：后端特有清理（框架已统一处理设备 STOP 文件/trace/tasktime）。"""
