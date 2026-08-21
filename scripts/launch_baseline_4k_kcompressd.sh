#!/bin/bash
# launch_baseline_4k_kcompressd.sh — baseline 全 4K + kcompressd 异步压缩 实验启动器（无 kfragd / 无大 folio）
# 用法: bash scripts/launch_baseline_4k_kcompressd.sh
#   默认: 重启进 bootloader -> 刷 vendor_boot_baseline.img -> 重启 -> 自检 -> 实验
# 自检要求(必须全部满足, 否则拒绝启动):
#   - fs_disable_large_folio = 1 (boot 参数, 由 vendor_boot bootconfig 注入)
#   - kfragd_enabled = 0 (kfragd 不能开)
#   - THP 16kB enabled != always (THP folio order 必须为 0)
#   - f2fs/ext4 min/max_folio_order_cap = 0 (文件页 folio order 必须为 0)
set -u -o pipefail

SERIAL="${FOLIO_S:-}"
[ -z "$SERIAL" ] && { echo "FAIL: 环境变量 FOLIO_S 未设置"; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$(dirname "$SCRIPT_DIR")"
OUT_DIR="${OUT_DIR:-$HOME/learn_os/.worklog/baseline_4k_$(date +%Y%m%d_%H%M%S)}"
RUNNER="$SCRIPT_DIR/run_memstress_and_collect_logs.py"
MANIFEST="${MANIFEST:-$SKILL_DIR/scripts/config/baseline_4k_config.json}"
VENDOR_BOOT_IMG="${VENDOR_BOOT_IMG:-$HOME/learn_os/.worklog/vendor_boot_images/vendor_boot_baseline.img}"
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
cat_dev "fs_disable_large_folio (boot)" /sys/module/kernel/parameters/fs_disable_large_folio
cat_dev "THP 16kB enabled" /sys/kernel/mm/transparent_hugepage/hugepages-16kB/enabled
cat_dev "f2fs min_folio_order_cap" /sys/fs/f2fs/dm-49/min_folio_order_cap
cat_dev "f2fs max_folio_order_cap" /sys/fs/f2fs/dm-49/max_folio_order_cap
cat_dev "ext4 min_folio_order_cap" /sys/fs/ext4/min_folio_order_cap
cat_dev "ext4 max_folio_order_cap" /sys/fs/ext4/max_folio_order_cap
cat_dev "zram (swap)" /proc/swaps
cat_dev "thermal_zone0 (BIG)" /sys/class/thermal/thermal_zone0/temp
cat_dev "thermal_zone2 (LITTLE)" /sys/class/thermal/thermal_zone2/temp

# ---- 3. 设置 baseline 全 4K 参数 ----
echo "=== 设置 baseline 参数 ==="
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
set_dev "kfragd_enabled" /proc/sys/vm/kfragd_enabled 0
set_dev "THP 16kB enabled" /sys/kernel/mm/transparent_hugepage/hugepages-16kB/enabled never
set_dev "f2fs min_folio_order_cap" /sys/fs/f2fs/dm-49/min_folio_order_cap 0
set_dev "f2fs max_folio_order_cap" /sys/fs/f2fs/dm-49/max_folio_order_cap 0

# ext4 全局 folio order cap 置 0 (ext4 为全局属性, 挂在 /sys/fs/ext4/ 根目录)
set_dev "ext4 min_folio_order_cap" /sys/fs/ext4/min_folio_order_cap 0
set_dev "ext4 max_folio_order_cap" /sys/fs/ext4/max_folio_order_cap 0
set_dev "kcompressd_enabled" /sys/module/kcompressd/parameters/kcompressd_enabled 1

# ---- 4. 回读自检: baseline 参数 ----
echo "=== 启动参数(设置后回读) ==="
read_dev() {
  adb -s "$SERIAL" shell "su -c 'cat $1 2>/dev/null'" </dev/null 2>/dev/null | tr -d '\r'
}

v="$(read_dev /proc/sys/vm/kfragd_enabled)"
ok="OK"; [ "$v" != "0" ] && { ok="MISMATCH(期望=0, kfragd 必须关闭)"; FAIL=1; }
printf "  %-24s = %-8s [%s]\n" "kfragd_enabled" "$v" "$ok"

v="$(read_dev /sys/module/kernel/parameters/fs_disable_large_folio)"
ok="OK"; [ "$v" != "1" ] && { ok="FAIL(boot 参数 fs_disable_large_folio 必须=1)"; FAIL=1; }
printf "  %-24s = %-8s [%s]\n" "fs_disable_large_folio" "$v" "$ok"

v="$(read_dev /sys/kernel/mm/transparent_hugepage/hugepages-16kB/enabled)"
case "$v" in
  "[always]"*) ok="FAIL(THP 16kB 仍是 always)"; FAIL=1 ;;
  *) ok="OK" ;;
esac
printf "  %-24s = %-8s [%s]\n" "THP_16kB_enabled" "$v" "$ok"

for cap in min max; do
  v="$(read_dev /sys/fs/f2fs/dm-49/${cap}_folio_order_cap)"
  ok="OK"; [ "$v" != "0" ] && { ok="MISMATCH(期望=0, 文件页必须全 4K)"; FAIL=1; }
  printf "  %-24s = %-8s [%s]\n" "f2fs_${cap}_folio_order_cap" "$v" "$ok"
done

for cap in min max; do
  v="$(read_dev /sys/fs/ext4/${cap}_folio_order_cap)"
  ok="OK"; [ "$v" != "0" ] && { ok="MISMATCH(期望=0)"; FAIL=1; }
  printf "  %-24s = %-8s [%s]\n" "ext4_${cap}_folio_order_cap" "$v" "$ok"
done

v="$(read_dev /proc/sys/vm/kfragd_enabled)"
zram_line="$(adb -s "$SERIAL" shell 'su -c "grep zram0 /proc/swaps"' </dev/null 2>/dev/null | tr -d '\r')"
if [ -n "$zram_line" ]; then
  echo "  zram (swap)          = 已启用 ($zram_line)  [OK]"
else
  echo "  zram (swap)          = 未启用      [FAIL]"
  FAIL=1
fi

v="$(read_dev /sys/module/kcompressd/parameters/kcompressd_enabled)"
ok="OK"; [ "$v" != "1" ] && { ok="MISMATCH(期望=1, kcompressd 必须开启)"; FAIL=1; }
printf "  %-24s = %-8s [%s]\n" "kcompressd_enabled" "$v" "$ok"

# kcompressd 线程必须常驻(init 创建), 否则拒绝启动
kc="$(adb -s "$SERIAL" shell 'su -c "ps -A -o NAME | awk '"'"'/kcompressd:/'"'"'"' </dev/null 2>/dev/null | tr -d '\r')"
kc_n="$(printf '%s\n' "$kc" | grep -c kcompressd)"
if [ "$kc_n" -ge 1 ] 2>/dev/null; then
  echo "  kcompressd threads      = $kc_n 个 [OK]"
else
  echo "  kcompressd threads      = 0 个      [FAIL: 模块未创建线程]"
  FAIL=1
fi

cat_dev "thermal_zone0 (BIG, 设置后)" /sys/class/thermal/thermal_zone0/temp
cat_dev "thermal_zone2 (LITTLE, 设置后)" /sys/class/thermal/thermal_zone2/temp

if [ "$FAIL" -ne 0 ]; then
  echo "FAIL: 自检未通过, 停止启动实验"
  exit 1
fi
echo "自检全部通过 (baseline 全 4K)"

# ---- 5. 启动实验 ----
[ -f "$MANIFEST" ] || { echo "FAIL: manifest 不存在 $MANIFEST"; exit 1; }
[ -f "$RUNNER" ] || { echo "FAIL: runner 不存在 $RUNNER"; exit 1; }
mkdir -p "$OUT_DIR"
say "启动实验: $(date '+%F %T')"
say "runner=$RUNNER"
say "manifest=$MANIFEST"
say "out_dir=$OUT_DIR"
cd "$SKILL_DIR" || exit 1
python3 "$RUNNER" \
  --serial "$SERIAL" \
  --from-manifest "$MANIFEST" \
  --out "$OUT_DIR"
exit $?
