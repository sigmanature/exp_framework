# CVD 后端重构 SOP（case × platform 两轴）

分工：DeepSeek V4 Flash 按本 SOP 逐阶段生成代码 + 自测；主线程（Opus）逐阶段审核。
本文件是权威流程与审核 checklist；跨上下文压缩后读本文件恢复状态。

## 0. 锁定决策（唯一真源）

| 项 | 决定 |
|---|---|
| 架构轴 | `case`（跑什么压力，平台无关）× `platform`（命令式设备拉起）两根正交轴 |
| 目录名 | 保留 `backend/` 名，语义=case（零改名 churn） |
| `device_prep.py` | 不删不 shim；通用件（ensure_zram/reboot_device_and_wait/cleanup_after_boot）留原位，pixel 件（prepare_cooldown_and_lock/LOCK_FREQ_*/thermal）由 pixel platform 调用，只改调用方 |
| `requirements()` | 不做；platform 自读 config |
| `--extra_kernel_cmdline` 配置项 | 删；由 platform.boot_cmdline(boot_params) 派生 |
| 边界 | 声明式(节点=值/采样/cmdline)→config(sysctl_nodes/sample_config/boot_params)；命令式(拉起/装包/insmod/通配发现)→platform |
| precondition | 用现有轴（precondition.mode + case.precondition()），不重造 |
| 兼容 | config loader shim：老 config.backend → case + platform（取 exp_ctx.platform，默认 pixel），56 旧模板零改动 |
| GUI | 在现有 GUI 增量改：注册表拆 case/platform + 启动校验两轴；其余不动 |
| 采样默认 v1 | frag20ms 探针不做；trace_cpu 用框架 trace/tasktime 不移植；pre/post 一次性快照保留（放 cuttlefish/guest.py） |

## 1. 契约

### Platform ABC（experiment/platform.py 新）
```
PLATFORM_REGISTRY: Dict[str, type]
register_platform(name)
create_platform(name, cfg, global_cfg, serial, out_dir, stop_event) -> Platform
class Platform(ABC):
    def prepare_device(self) -> dict      # 命令式拉起；返回并入 manifest 私有字段；默认 no-op
    def teardown(self) -> None            # 默认 no-op
    def device_residuals(self, serial) -> list[str]  # 默认 []
    def boot_cmdline(self, boot_params) -> str | None # 默认 None
```
platform 只做命令式动作，不碰 sysctl/采样/校验；数据从 self.cfg / self.global_cfg 自取。

### Case（现 Experiment ABC，语义澄清，签名不变）
prepare() 只做 case 级准备；进入时假设"设备已就绪+root+包已装好"。

### 组合（runner.py 改）
```
platform = create_platform(cfg.platform.name, cfg.platform.config, global_cfg, serial, out_dir, stop_event)
case     = create_experiment(cfg.case.name, cfg.case.config, global_cfg, serial, out_dir, stop_event)
prepare : platform.prepare_device() -> case.prepare()
verify  : _apply_sysctl_verify(sysctl_nodes); boot_params 校验; launch cmdline 由 platform.boot_cmdline 生成
run     : case.run()
cleanup : case.cleanup() -> platform.teardown()
residual: case.device_residuals() + platform.device_residuals()
```

### config schema + shim
```
"config": {
  "case":     {"name": "...", "config": {...}},
  "platform": {"name": "...", "config": {...}},
  "rounds": 1, "precondition": {...}
},
"sysctl_nodes": [...], "boot_params": [...], "sample_config": {...},
"exp_ctx": {"exp_name":..., "domain":..., "serial":...}   # platform 身份统一读 config.platform.name
```
shim：见到 config.backend → case={backend} + platform={"name": exp_ctx.platform or "pixel", "config": <行为保真映射>}。
行为保真映射：memstress → pixel.config.cooldown=true,lock_freq=true；madvise_pagout/frag_ab → false。

## 2. 目标目录

```
experiment/{experiment.py(Case ABC), platform.py(新), __init__.py(改导出), runner.py(改), config.py(改), sample.py(不动)}
backend/{memstress.py(改:剥冷却锁频/解频/lmkd), madvise_pagout.py, frag_ab.py}
platform/{__init__.py, pixel.py, cuttlefish/{__init__.py, lifecycle.py, guest.py, workload_install.py}}
utils/(不动, device_prep 留原位)
ui/(增量改两轴)
config/(新增 cvd_16k_baseline/aggressive)
```

## 3. 阶段（每阶段：DeepSeek 生成+自测 → 主线程审核门）

### Phase 0 绿色基线
修 tests/test_device_prep.py 3 失败（2 mock-stale：mock 里 patch adb_utils.adb_shell_root 或先调 ensure_privilege 桩；1 real-device：无设备 skipUnless 守卫）。
自测：`pytest tests/ -q` 全绿（含真机环境亦可）。审核门：只动测试不动被测逻辑。

### Phase 1 引入 platform 轴（零行为变化）
产出：experiment/platform.py；platform/pixel.py（prepare_device 按 cfg.cooldown/lock_freq 调 device_prep；teardown 解频+start lmkd；boot_cmdline 返回 None）；platform/__init__.py；experiment/__init__.py 增导出；config.py 加 case/platform 解析+shim+映射表；runner.py 组合两轴；backend/memstress.py 删 prepare 里 cooldown 调用与 cleanup 里解频/lmkd；tests/test_runner_flow.py 更新 mock（加 create_platform）。
自测：pytest 全绿 + 新增 test_platform_compose.py（mock adb，跑老 schema 与新 schema，断言 prepare/cleanup 设备命令序列与 Phase0 一致；memstress 冷却、madvise/frag 不冷却）；56 模板批量 load 通过。
审核门：无 if platform== 撒进 case/sample/runner 业务；pixel 逐命令 diff 无变化。

### Phase 2 cuttlefish platform（命令式移植旧脚本）
移植源：.worklog/scripts/thp_ab_4k16k_runner.sh（launch_profile 620-746 / apply/zram/install / preflight）与 run_order0_distribution_longtest.sh。
lifecycle.py：launch_cvd(--daemon --resume --system_image_dir --guest_enforce_security=false --memory_mb --base_instance_num/--num_instances/--vsock_guest_cid + 镜像 override + exec 9>&- + setsid)、等 VIRTUAL_DEVICE_BOOT_COMPLETED/kernel_log、cvd_manual_adb_proxy.sh connect(env RUN_DIR/PORT/CID/VSOCK_PORT/ADB_BIN)、adb root 重连、wait boot_completed、userdata 大盘 symlink、restart_ledger、preflight_run_dir(findmnt UUID+9 镜像+os_composite)。
guest.py：setup_zram(insmod zsmalloc/zram→reset→disksize/comp_algorithm 交 sysctl_nodes+框架 ensure_zram)；folio caps 通配发现→追加进 global_cfg["sysctl_nodes"]（早于框架 verify）；capture_snapshot(pre/post 证据)。
workload_install.py：install_workload_apks + wait_package_file_installed（每 adb 调用 </dev/null）+ overlay wipe 检测。
__init__.py：Cuttlefish(Platform)：prepare_device=lifecycle→preflight→zram→folio→install→包门；boot_cmdline=" ".join(f"{p['param']}={p['expected']}")；teardown(默认保活,stop_on_exit 才停)。
自测：test_cuttlefish_unit.py（mock subprocess）覆盖 boot_cmdline 拼接/包文件解析/launch 参数拼装。
审核门：与旧 shell 逐条对照；platform 内无 knob verify/无采样。

### Phase 3 CVD config
config/cvd_16k_baseline.json + cvd_16k_aggressive.json：case=memstress + platform=cuttlefish + sysctl_nodes(THP/kfragd/kswapd/compaction/zram disksize+comp_algorithm) + boot_params(uffd_mfill_order2=1/mthp_cow_order2=1/order0_order2_zone_isolation=0/order0_cow_parent_root=1) + sample_config(vmstat 全量白名单+pagetypeinfo+buddyinfo)。baseline vs aggressive 仅差 kfragd_enabled 与 exp_name。
自测：load_config/resolve_sample_config 解析通过；boot_cmdline 输出符合。审核门：knob 全走 sysctl_nodes；boot_params 单源。

### Phase 4 CVD smoke gate（真机，主线程审证据）
baseline config，max_cycles=3。审核门：sys.boot_completed=1；device_done total_cycles=3 total_err=0；vmstat/pagetypeinfo samples 非空；sysctl 全 OK；/proc/cmdline 含全部 boot_params；fatal_scan 无真实崩溃；state/exit_code=0。任一不过回 Phase2/3。

### Phase 5 GUI 两轴（增量改）
ui/main_window.py backend_registry()→case_registry()+platform_registry()；启动校验 config.case.name 与 config.platform.name 均注册；config_model/queue_model/audit/driver 基本不动；GUI 侧同吃 shim。
自测：expf_ui_driver.py offscreen 通过；能载入 cvd 模板显示两轴；老模板仍能载入。

### Phase 6 文档 & 归档
更新 SKILL.md/README.md/config/README.md；标注归档 .worklog/scripts/{run_order0_distribution_longtest,thp_ab_4k16k_runner}.sh 并映射到新配置。

## 4. 主线程审核 checklist
- [ ] 声明式(knob/采样/cmdline)全在 config，未泄漏进 platform 命令式代码
- [ ] platform 只有命令式动作，无 verify/采样
- [ ] case 平台无关（无 CVD/pixel 分支、无冷却/launch 假设）
- [ ] if platform== 只在框架组合层与 kernel_boot_utils
- [ ] pixel 逐命令无回归（Phase1 对照测试）
- [ ] DeepSeek 自测命令与结果已附、可复跑
- [ ] 无越界写盘/secrets；git diff 干净

## 5. DeepSeek 派发方式
worker = `opencode run -m Mify-DS/deepseek/deepseek-v4-flash --dir <exp_framework> "<phase prompt>"`
每阶段：主线程写 phase prompt（含契约+验收+自测命令）→ 派发 → DeepSeek 生成+自测并回报 → 主线程 review（读 diff + 复跑自测）→ 通过才进下一阶段。
