# 实验框架架构设计

> 本文面向使用与扩展本框架的读者，描述：框架解决什么问题、核心概念、
> 组件职责、如何新增一个实验后端。阅读时间约 10 分钟。

## 1. 背景与目标

本框架管理"在 Android 设备上跑实验并采集指标"这件事。实验通常包含两部分：

- **实验业务**：具体跑什么（memstress 启动 app 压力循环、micro bench 跑一轮
  madvise 基准……），各有各的设备端逻辑与产物解析。
- **观测设施**：vmstat 采样、buddyinfo、ftrace 抓取、tasktime 进程 CPU
  统计、能耗计（ODPM）、lock_stat、logcat/crash 检测——这些对所有实验
  通用，只由"开了哪些采样项"决定。

目标：**采样设施与实验业务解耦**。新增一个实验后端时，只需写"跑什么 +
  怎么解析产物"，观测设施、设备准备、信号清理、结果归档全部复用。

## 2. 核心概念

| 概念 | 说明 |
|---|---|
| **输入配置（config）** | 用户编辑的 JSON 文件，定义实验参数与采样项。见 `config/*.json` |
| **运行时清单（run_manifest.json）** | 框架生成的实验档案：状态、起止时间、采样计数、后端结果。**程序生成，不要手改** |
| **实验后端（Experiment）** | 一个类，描述一个实验"跑什么"。注册后可通过 `backend.name` 选择 |
| **采样前端（sample）** | `sample_start` / `sample_end` 两段通用逻辑，由采样配置驱动 |

## 3. 整体流程

```
┌───────────┐    ┌───────────┐    ┌───────────┐    ┌───────────┐
│  runner   │───▶│  backend  │───▶│  sample   │───▶│  backend  │
│ (CLI/编排)│    │ .prepare  │    │  .start   │    │   .run    │
└───────────┘    └───────────┘    └───────────┘    └───────────┘
                                        │
                                        ▼
                                  ┌───────────┐
                                  │  sample   │（backend.run 结束后必然执行）
                                  │   .end    │
                                  └───────────┘
```

1. **runner** 加载输入配置，实例化后端。
2. **backend.prepare()**：预检与准备，由各后端自行实现（memstress 的
   设备准备 = 唤醒/解锁/锁频/冷却 + 包解析；其他后端可以完全不同的准备
   逻辑）。失败则实验在采样启动前中止（不浪费采样资源）。
3. **sample_start**：按采样配置启动全部观测设施（vmstat 基线、采样线程、
   trace probe、tasktime、logcat/crash 检测等）。
4. **backend.run()**：实验主循环。必须周期性检查 `stop_event`，以便任意
   时刻被打断。
5. **sample_end**：停止观测设施并推导产物（trace 拉取、tasktime 差分、
   vmstat 尾快照、能耗差分、指标推导）。**位于 finally 中，无论 run 正常
   结束、被打断还是异常，都保证执行**。

## 4. 目录结构

```
scripts/
├── experiment/              # 前端框架（通用，与具体实验无关）
│   ├── experiment.py        # Experiment 基类 + 注册表（REGISTRY/register）
│   ├── config.py            # 配置加载/解析、采样配置默认值、运行时清单写入
│   ├── sample.py            # sample_start / sample_end（观测设施启停）
│   └── runner.py            # CLI 入口与流程编排、信号处理、设备清理
├── experiments/             # 实验后端（每个实验一个模块）
│   └── memstress.py         # class Memstress(Experiment)：app 冷启动压力
├── config/                  # 输入配置模板（用户复制修改）
│   ├── baseline_4k_config.json            # baseline 全 4K 实验（99 包，120 轮）
│   ├── kfragd_config.json                 # kfragd 100ms 实验（含 kswapd/kcompactd/kcompressd tasktime）
│   ├── default_memstress_config.json      # 通用 memstress 模板（100 包）
│   └── compact_daemon_precond_config.json # compact daemon 预碎片化实验（50 轮）
├── run_memstress_and_collect_logs.py  # 旧入口兼容薄壳（原 CLI 不变）
└── utils/                   # adb/采样/trace 等底层工具（与框架无关）
```

## 5. 后端接口契约

```python
class Experiment(ABC):
    name: str                       # 注册名，config 中 backend.name 选择

    def __init__(self, backend_config, global_config, serial, out_dir,
                 stop_event): ...
        # backend_config: config["config"]["backend"]["config"]（后端私有参数）
        # work_dir: out_dir/<name>/（后端产物目录，已自动创建）

    @abstractmethod
    def prepare(self) -> dict:      # 必须实现：设备准备、包解析、预检等
        # 返回写入 run_manifest.json 的字段（如 packages_resolved）

    @abstractmethod
    def run(self) -> dict:          # 必须实现；响应 stop_event
        # 返回并入 run_manifest.json 的结果字段

    def cleanup(self) -> None:      # 可选：后端特有清理钩子
```

约束只有一条：**run() 必须能在 stop_event 置位后尽快退出**（轮询型天然满足；
阻塞型需自行分片或设超时）。

## 6. 设备权限（root / su 自动选择）

框架不再要求配置或传参指定 `use_su`。`utils/adb_utils.ensure_privilege()` 在
runner 启动时探测一次权限模式，并把模块级函数对象 `adb_shell_root` 绑定为
具体实现：

1. `adb shell id` 已是 uid=0 → **直接模式**（adbd 以 root 运行，零前缀）
2. `adb root`（adbd 重启后验证 uid=0）→ **直接模式**
3. `su -c id` 可用 → **su 模式**（`adb shell su -c ...`，写 sysfs 时自动
   使用交互 TTY 兼容部分设备）
4. 全部失败 → 抛错拒绝启动实验（早停，避免各处隐性失败）

调用方统一使用 `adb_utils.adb_shell_root(...)`（模块属性引用，绑定生效）；
未探测即调用会显式报错。普通命令（am/pm/dumpsys/input，shell 身份即可）
仍走 `adb_shell(...)`，不受影响。

## 7. 输入配置示例

```json
{
  "config": {
    "interval_s": 60,
    "counters": ["anon_fault_alloc", "swpin", "swpout"],
    "backend": {
      "name": "memstress",
      "config": {
        "packages": ["com.tencent.mm", "com.sankuai.meituan"],
        "max_cycles": 120,
        "burst_size": 4,
        "hold_ms": 15,
        "seed": 20260617
      }
    }
  },
  "sample_config": {
    "vmstat": {"keys": null, "interval_s": 60,
               "buddyinfo": {"enabled": false, "interval_s": 5}},
    "trace": {"captures": [{"name": "main", "buffer_kb": 16384,
                            "events": ["vmscan:mm_vmscan_reclaim_pages"]}]},
    "tasktime": {"procs": ["kswapd0"]},
    "lock_stat": {"enabled": false},
    "power": {"odpm": false}
  }
}
```

## 8. 如何新增一个实验后端

以 madvise pagout micro bench 为例：

1. **新建 `experiments/madvise_pagout.py`**：

```python
from experiment.experiment import Experiment, register

@register("madvise_pagout")
class MadvisePagout(Experiment):

    def prepare(self):
        # 设备准备（唤醒/解锁/锁频/冷却）+ bench 构建/推送/参数校验
        from utils.device_prep import ensure_awake_unlocked_and_stay_awake
        ensure_awake_unlocked_and_stay_awake(self.serial, out_dir=self.out_dir,
                                             retries=3, retry_sleep_s=2,
                                             stop_event=self.stop_event)

    def run(self):
        rounds = 0
        while not self.stop_event.is_set():
            # ... 跑一轮 bench（adb shell），结果写 self.work_dir
            rounds += 1
        return {"rounds": rounds, "results": ...}   # 并入 run_manifest.json
```

2. **新建 `config/madvise_pagout_config.json`**：定义 `backend.config` 参数
   与需要的 `sample_config` 采样项（模板可复制 baseline_4k_config.json）。

3. **运行**：

```bash
python3 -m experiment.runner --serial <serial> \
    --from-config config/madvise_pagout_config.json
```

采样、设备准备、Ctrl-C 打断、清理、归档全部自动获得。新后端需要关心的
只有三件事：backend.config 参数、run() 的业务逻辑、run() 对 stop_event
的响应。

## 9. 优雅退出模型

- **Ctrl-C / SIGTERM**：置位 stop_event，同时启动设备清理线程（trace probe
  停止、tasktime 终止）。`backend.run()` 检测到 stop_event 后尽快返回；
  `sample_end` 在 finally 中必然执行，最后进程以退出码 130 结束。
- **`--stop`**（另一进程请求）：对设备侧发送停止信号，运行中的宿主进程
  自行完成收尾。
- **异常路径**：run 中途抛异常时，`sample_end` 依然执行（采样产物不丢），
  随后设备清理，异常继续上抛。

## 10. 兼容层

`run_memstress_and_collect_logs.py` 是旧入口的薄壳：原 CLI 参数全部保留
（`--package/--burst-size/--seed/--from-manifest/...`），内部组装成新格式
配置后调用统一前端。`launch_baseline_4k.sh` 等外部脚本无需改动（其
MANIFEST 默认值已指向新配置模板 `scripts/config/baseline_4k_config.json`）。

## 11. 术语对照

| 旧叫法（已弃用） | 新叫法 | 含义 |
|---|---|---|
| manifest（输入） | **config** | 用户编辑的实验配置 |
| run_manifest.json（输出） | **run_manifest.json** | 运行时生成的实验档案 |
| backend | **experiment 后端 / Experiment** | 描述"跑什么"的类 |
| sample_config | **sample_config** | 采样项配置（含义不变） |
