# Config templates

本目录保存 `exp_framework` 组件的默认运行配置（manifest）。

## 组件归属说明

本目录属于 **memstress 负载组件**（`exp_framework`）。这里的 manifest 是该组件自己的默认配置，只描述本组件的负载行为；内核参数、设备准备、实验编排等属于其他组件，各有各的 manifest，不在本目录定义。

## 默认配置 vs synthetic 负载（平台形态）

本组件的默认配置 `default_memstress_manifest.json` 是**真实 App 负载**，适用于真实设备（如 Pixel）：

- 直接用 `--from-manifest config/default_memstress_manifest.json --serial <设备序列号>` 运行；
- `packages` 列表中的包未安装时会被脚本自动跳过，无需改动 manifest。

**CVD（Cuttlefish）场景**改用 synthetic 负载（x86_64 虚拟设备装不了足够的真实第三方 App）：

- 流程见 [`references/synthetic_mthp_apk_workload.md`](../references/synthetic_mthp_apk_workload.md)；
- synthetic 的规模参数（`--package-file`、`--synthetic-*-scale` 等）是运行时 CLI 参数，**不属于默认 manifest**，也不会写回本目录。

## 框架编排字段（config.config 顶层，runner 通用）

- `rounds`（整数，缺省 1）：整个实验重复轮数。`rounds=1` 输出布局与旧版一致；
  `rounds>1` 时每轮产物在 `<out>/<exp_id>/round_<n>/`，根目录 `run_manifest.json`
  为聚合清单（含 `rounds[]` 每轮汇总与 `samples`/`sample_errors` 合计）。
- `precondition`（对象，缺省即不启用）：系统状态预备（碎片化打碎/重启）的调度模式。
  - `mode: "once"`：设备准备（prepare）后、第一轮采样前执行一次，所有轮次共享该初始状态；
  - `mode: "per_round"`：每轮采样开始前都重新执行一次；
  - `restart: true`（可选，默认 false）：执行打碎**之前**先重启设备到干净系统态
    （等 boot_completed + settle + 清后台），重启也归入 precondition 编排；
    once+restart = 第一轮前重启+打碎共享；per_round+restart = 每轮重启后打碎；
  - 不写 block / `mode: "none"`：不做任何 precondition。
- 每类设施的清理/残留检查归各自层管理：采样设施（trace probe/tasktime）在
  `src/exp_framework/experiment/sample.py`，后端部署（如 memstress device runner）
  在后端自己的 `device_residuals()`——框架（runner）只调度不展开命令。
- 打碎"内容"仍由后端配置描述：memstress 后端读 `backend.config.prefrag`
  （threshold/max_swipes/killall_only 等），框架只负责"什么时候打"。
- 与旧 CLI 的关系：`--precondition` / `--precondition-threshold` 属于旧
  precondition.py 流程与短壳，仍保留；新实验建议用 config 的
  `precondition.mode` + `backend.config.prefrag`，runner 还有
  `--rounds` / `--precondition-mode` 两个 CLI 覆盖项。

## 采样主开关

`sample_config.enabled`（默认 `true`）是框架级采样主开关：`false` 时 runner 跳过全部采样（工作负载照跑），各采样域（cycle_sample 子域 / lock_stat / power / tasktime / trace）也支持各自的 `enabled` 显式关闭（缺省 `true`）。注意深合并语义：**模板里未写的键会回落默认**（如 power.odpm 默认开），要"关"必须显式写 `enabled: false`。

## 文件说明

- [`default_memstress_manifest.json`](./default_memstress_manifest.json)：标准 memstress + THP 16KB stats 采样配置。
  - 已固定随机种子、轮次、采样间隔和 memstress 节奏。
  - 已移除 `stats_dir`：采样路径固定为 `/sys/kernel/mm/transparent_hugepage/hugepages-16kB/stats`。
  - 运行前把 `serial` 替换为实际 adb 序列号，或直接在命令行用 `--serial` 覆盖。
  - `packages` 列表是示例；未安装的包会被脚本自动跳过。

本目录只包含**可复用的默认参数**。单次真实运行的完整产物（`packages_resolved`、真实时间戳、采样结果）会写在 `--out-dir` 下的 `run_manifest.json`（运行快照，不是本组件的默认配置）。

使用方式：

```bash
python3 scripts/run_memstress_and_collect_logs.py \
  --serial <YOUR_DEVICE_SERIAL> \
  --from-manifest config/default_memstress_manifest.json
```
