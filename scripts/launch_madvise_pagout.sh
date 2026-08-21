#!/bin/bash
# launch_madvise_pagout.sh — 4K/16K × kcompressd 矩阵（每组独立 runner，组间重启隔离）
# 用法: FOLIO_S=21121FDF600C4G bash launch_madvise_pagout.sh
#   OUT_DIR 可覆盖输出根目录（默认 runs/framework_<时间戳>）
set -u -o pipefail

SERIAL="${FOLIO_S:?set FOLIO_S}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT_DIR="${OUT_DIR:-$HOME/learn_os/.worklog/madvise_pagout_bench/runs/framework_$(date +%Y%m%d_%H%M%S)}"
CFG_TEMPLATE="$SCRIPT_DIR/config/madvise_pagout_config.json"
GROUPS=(4k_nokcompressd 4k_kcompressd 16k_nokcompressd 16k_kcompressd)

wait_boot() {
  for i in $(seq 1 48); do
    v=$(adb -s "$SERIAL" shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')
    [ "$v" = "1" ] && return 0
    sleep 5
  done
  echo "FAIL: boot 超时"; return 1
}

for g in "${GROUPS[@]}"; do
  # 按组生成 config：group + 组旋钮期望值（mthp_16k / kcompressd_enabled）
  case "$g" in
    16k*) thp="[always]" ;;
    *)    thp="[never]" ;;
  esac
  case "$g" in
    *kcompressd) kcd="1" ;;
    *)           kcd="0" ;;
  esac
  CFG=$(mktemp)
  jq --arg g "$g" --arg thp "$thp" --arg kcd "$kcd" \
     '.config.backend.config.group = $g
      | (.config.sysctl_nodes[] | select(.param == "mthp_16k") | .expected) = $thp
      | (.config.sysctl_nodes[] | select(.param == "kcompressd_enabled") | .expected) = $kcd' \
     "$CFG_TEMPLATE" > "$CFG" || { echo "FAIL: jq 生成 config 失败"; exit 1; }

  echo "=== 组 $g ==="
  (cd "$SCRIPT_DIR" && python3 -m experiment.runner \
      --serial "$SERIAL" --from-config "$CFG" --out "$OUT_DIR/$g") || {
        echo "FAIL: 组 $g 失败"; rm -f "$CFG"; exit 1; }
  rm -f "$CFG"

  if [ "$g" != "${GROUPS[-1]}" ]; then
    echo "--- 组间重启隔离 ---"
    adb -s "$SERIAL" reboot || exit 1
    adb -s "$SERIAL" wait-for-device || exit 1
    wait_boot || exit 1
    sleep 10
  fi
done
echo "全部完成: $OUT_DIR"
