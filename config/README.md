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
