# 项目架构说明 — CVD 后端重构

## 最小用户需求集合
- 用户要在 Cuttlefish(CVD) 上跑与 pixel 相同的 memstress 压力实验（baseline/aggressive 两组，分别重复跑，不交替）。
- 框架要普适：以后很多 case（memstress/madvise_pagout/frag_ab...）都能在不同平台跑，不能让每个 case 耦合平台代码。
- 代码生成+自测交给 DeepSeek V4 Flash，主线程只负责审核。
- 明确不希望：为"未来通用性"加用户看不到价值的抽象（如 requirements() 能力协商）。

## 最终交互体验（用户视角）
1. 用户写/选一个 config：`case`（跑什么）+ `platform`（在哪跑）+ sysctl_nodes/boot_params/sample_config。
2. `runner` 组合两轴：platform 先命令式拉起设备（CVD launch/proxy/装包/zram），case 再跑压力，框架统一采样/校验/收尾。
3. 老的 pixel config（backend schema）不改也能跑（shim 自动映射）。
4. GUI 里 case 和 platform 各一个下拉。

## 需求→架构映射
| 用户需求 | 承载文件 | 为何必需 |
|---|---|---|
| case 与平台解耦 | experiment/platform.py(Platform ABC) + backend/(=case) | 否则 N case × M 平台类爆炸 |
| 命令式设备拉起 | platform/pixel.py, platform/cuttlefish/* | config 的 set+verify 表达不了 launch/insmod/装包 |
| 声明式状态 | config: sysctl_nodes/sample_config/boot_params（现有轴，不动） | 已完善的 set+回读+采样 |
| 两轴组合 | experiment/runner.py, config.py(含 shim) | 编排 + 旧配置兼容 |
| 旧 pixel 零回归 | config.py shim + 行为保真映射 | 56 旧模板不迁移 |

## 最小数据与状态
- config.case.config：case 私有参数（memstress: max_cycles/scales/workload.package_file）。
- config.platform.config：仅 CVD 专属 env（home/run_dir/port/cid/instance_num/userdata_root/image_work_dir/memory_mb/resume）。
- 不新增用户看不到价值的字段；requirements()/能力协商已否决。

## 可行性边界
- 可机械验证：pytest（Phase0-3,5 自测）、config 批量 load、boot_cmdline 拼接。
- 只能真机验证：Phase4 CVD smoke（框架设备端 cycle runner 从未在 CVD 跑过），由主线程审证据，不能纯自测。

## 当前状态
- 已存在：experiment/{experiment,runner,sample,config}、backend/{memstress,madvise_pagout,frag_ab}、utils/*、ui/*、config/*(56 个 pixel)。
- 待建（最少）：experiment/platform.py、platform/ 包、2 个 cvd config。
- 待改：runner.py、config.py、experiment/__init__.py、backend/memstress.py、ui/main_window.py、tests。
- 下一步：Phase 0（修 3 个失败测试建绿色基线）。基线现状：3 failed(test_device_prep) / 58 passed / 1 skipped。
