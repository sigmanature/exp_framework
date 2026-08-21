#!/bin/bash
# launch_kfragd_100ms_exp.sh — kfragd force-reclaim + 100ms interval + 16KB THP 实验启动器
# 用法: bash scripts/launch_kfragd_100ms_exp.sh
#   默认: 重启进 bootloader -> 刷 vendor_boot_default.img -> 重启 -> 自检 -> 实验
# 前置: $FOLIO_S 设备已 root(magisk)、已刷入含 kfragd 的内核
# 流程: 重启刷镜像 -> 自检(设备在线+cat当前值) -> 设置参数 -> 回读验证 -> 启动 memstress 实验
set -u -o pipefail

SERIAL="${FOLIO_S:-}"
[ -z "$SERIAL" ] && { echo "FAIL: 环境变量 FOLIO_S 未设置"; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$(dirname "$SCRIPT_DIR")"
OUT_DIR="${OUT_DIR:-$HOME/learn_os/.worklog/thp_16k_kfragd_100ms_$(date +%Y%m%d_%H%M%S)}"
RUNNER="$SCRIPT_DIR/run_memstress_and_collect_logs.py"
MANIFEST="${MANIFEST:-$SKILL_DIR/scripts/config/kfragd_config.json}"
VENDOR_BOOT_IMG="${VENDOR_BOOT_IMG:-$HOME/learn_os/.worklog/vendor_boot_images/vendor_boot_default.img}"
FAIL=0

say() { echo "[exp] $*"; }

# ---- 重启进 bootloader -> 刷 vendor_boot -> 重启等 boot 完成 ----
reflash_vendor_boot() {
  local img="$1"
  echo "=== 重启进 bootloader 并刷入 $(basename "$img") ==="
  [ -f "$img" ] || { echo "FAIL: 镜像不存在 $img"; exit 1; }
  adb -s "$SERIAL" reboot bootloader || { echo "FAIL: adb reboot bootloader 失败"; exit 1; }
  local i=0
  while [ $i -lt 60 ]; do
    fastboot devices 2>/dev/null | grep -q "$SERIAL" && break
    sleep 1; i=$((i + 1))
  done
  fastboot devices 2>/dev/null | grep -q "$SERIAL" || { echo "FAIL: fastboot 设备未出现(60s)"; exit 1; }
  fastboot flash vendor_boot "$img" || { echo "FAIL: fastboot flash 失败"; exit 1; }
  fastboot reboot || { echo "FAIL: fastboot reboot 失败"; exit 1; }
  echo "  flash 完成, 等待设备 boot..."
  adb wait-for-device 2>/dev/null
  local booted=""
  i=0
  while [ $i -lt 300 ]; do
    booted="$(adb -s "$SERIAL" shell 'getprop sys.boot_completed' 2>/dev/null | tr -d '\r')"
    [ "$booted" = "1" ] && break
    sleep 2; i=$((i + 2))
  done
  [ "$booted" = "1" ] || { echo "FAIL: 设备 boot 超时(300s)"; exit 1; }
  sleep 10
  echo "  设备 boot 完成, 就绪"
}

# ---- zram 自开: 未启用则配置 8G 并挂载(幂等) ----
ensure_zram() {
  echo "=== 确保 zram 启用 (8G) ==="
  if adb -s "$SERIAL" shell 'su -c "grep -q zram0 /proc/swaps"' </dev/null 2>/dev/null; then
    echo "  zram 已启用, 跳过配置"
    return 0
  fi
  cur="$(adb -s "$SERIAL" shell 'su -c "cat /sys/block/zram0/disksize 2>/dev/null"' </dev/null 2>/dev/null | tr -d '\r')"
  if [ -z "$cur" ] || [ "$cur" = "0" ]; then
    adb -s "$SERIAL" shell 'su -c "echo 8589934592 > /sys/block/zram0/disksize"' </dev/null >/dev/null 2>&1 || {
      echo "FAIL: 设置 zram disksize 失败"; FAIL=1; return 1; }
  fi
  adb -s "$SERIAL" shell 'su -c "mkswap /dev/block/zram0"' </dev/null >/dev/null 2>&1 || {
    echo "FAIL: mkswap 失败"; FAIL=1; return 1; }
  adb -s "$SERIAL" shell 'su -c "swapon /dev/block/zram0 -p 100"' </dev/null >/dev/null 2>&1 || {
    echo "FAIL: swapon 失败"; FAIL=1; return 1; }
  echo "  zram 已配置并启用 (8G, priority 100)"
}

# ---- 1. 设备在线 ----
if ! adb -s "$SERIAL" get-state >/dev/null 2>&1; then
  echo "FAIL: 设备 $SERIAL 不在线"
  exit 1
fi

# ---- 1.5 重启刷镜像 ----
reflash_vendor_boot "$VENDOR_BOOT_IMG"

# ---- 2. 自检: 设置前 cat 当前值 ----
echo "=== 自检: 设置前当前值 (serial=$SERIAL) ==="
cat_dev() {
  local label="$1" path="$2"
  local v
  v="$(adb -s "$SERIAL" shell "su -c 'cat $path 2>/dev/null'" </dev/null 2>/dev/null | tr -d '\r')"
  printf "  %-48s = %s\n" "$label" "${v:-<读不到>}"
}
cat_dev "kfragd_enabled" /proc/sys/vm/kfragd_enabled
cat_dev "kfragd_force_reclaim" /proc/sys/vm/kfragd_force_reclaim
cat_dev "kfragd_interval_ms" /proc/sys/vm/kfragd_interval_ms
cat_dev "kfragd_frag_high" /proc/sys/vm/kfragd_frag_high
cat_dev "kfragd_frag_low" /proc/sys/vm/kfragd_frag_low
cat_dev "kfragd_reclaim_batch" /proc/sys/vm/kfragd_reclaim_batch
cat_dev "THP 16kB enabled" /sys/kernel/mm/transparent_hugepage/hugepages-16kB/enabled
cat_dev "THP defrag" /sys/kernel/mm/transparent_hugepage/defrag
cat_dev "zram (swap)" /proc/swaps
cat_dev "thermal_zone0 (BIG)" /sys/class/thermal/thermal_zone0/temp
cat_dev "thermal_zone2 (LITTLE)" /sys/class/thermal/thermal_zone2/temp

# ---- 3. 设置实验参数 ----
echo "=== 设置实验参数 ==="
ensure_zram
set_dev() {
  local label="$1" path="$2" value="$3"
  adb -s "$SERIAL" shell "su -c 'echo $value > $path'" </dev/null >/dev/null 2>&1
  local rc=$?
  if [ $rc -ne 0 ]; then
    echo "FAIL: 写 $label ($path=$value) 失败 rc=$rc"
    FAIL=1
  fi
  return $rc
}
set_dev "kfragd_enabled" /proc/sys/vm/kfragd_enabled 1
set_dev "kfragd_force_reclaim" /proc/sys/vm/kfragd_force_reclaim 1
set_dev "kfragd_interval_ms" /proc/sys/vm/kfragd_interval_ms 100
set_dev "THP 16kB enabled" /sys/kernel/mm/transparent_hugepage/hugepages-16kB/enabled always
set_dev "THP defrag" /sys/kernel/mm/transparent_hugepage/defrag always

# ---- 4. 回读自检: 启动参数 ----
echo "=== 启动参数(设置后回读) ==="
REQS=(
  "kfragd_enabled|/proc/sys/vm/kfragd_enabled|1"
  "kfragd_force_reclaim|/proc/sys/vm/kfragd_force_reclaim|1"
  "kfragd_interval_ms|/proc/sys/vm/kfragd_interval_ms|100"
  "THP_16kB_enabled|/sys/kernel/mm/transparent_hugepage/hugepages-16kB/enabled|[always]"
  "THP_defrag|/sys/kernel/mm/transparent_hugepage/defrag|[always]"
)
for entry in "${REQS[@]}"; do
  name="${entry%%|*}"; rest="${entry#*|}"; path="${rest%%|*}"; expect="${rest#*|}"
  v="$(adb -s "$SERIAL" shell "su -c 'cat $path 2>/dev/null'" </dev/null 2>/dev/null | tr -d '\r')"
  ok="OK"
  case "$v" in
    "$expect"*) : ;;
    *) ok="MISMATCH(期望=$expect)"; FAIL=1 ;;
  esac
  printf "  %-24s = %-8s [%s]\n" "$name" "$v" "$ok"
done

# zram 必须已启用, 否则拒绝启动实验
if ! adb -s "$SERIAL" shell 'su -c "grep -q zram0 /proc/swaps"' </dev/null 2>/dev/null; then
  echo "  zram (swap)          = 未启用      [FAIL]"
  FAIL=1
else
  zram_line="$(adb -s "$SERIAL" shell 'su -c "grep zram0 /proc/swaps"' </dev/null 2>/dev/null | tr -d '\r')"
  echo "  zram (swap)          = 已启用 ($zram_line)  [OK]"
fi
cat_dev "thermal_zone0 (BIG, 设置后)" /sys/class/thermal/thermal_zone0/temp
cat_dev "thermal_zone2 (LITTLE, 设置后)" /sys/class/thermal/thermal_zone2/temp

if [ "$FAIL" -ne 0 ]; then
  echo "FAIL: 自检未通过, 停止启动实验"
  exit 1
fi
echo "自检全部通过"

# ---- 5. 启动实验 ----
[ -f "$MANIFEST" ] || { echo "FAIL: manifest 不存在 $MANIFEST"; exit 1; }
[ -f "$RUNNER" ] || { echo "FAIL: runner 不存在 $RUNNER"; exit 1; }
mkdir -p "$OUT_DIR"
say "启动实验: $(date '+%F %T')"
say "runner=$RUNNER"
say "manifest=$MANIFEST"
say "out_dir=$OUT_DIR"
cd "$SKILL_DIR" || exit 1
exec python3 "$RUNNER" \
  --serial "$SERIAL" \
  --from-manifest "$MANIFEST" \
  --out "$OUT_DIR"
