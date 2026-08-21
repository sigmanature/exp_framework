#!/usr/bin/env bash
set -u -o pipefail

ANDROID_ROOT=${ANDROID_ROOT:-/home/nzzhao/learn_os/android17}
ADB=${ADB:-$ANDROID_ROOT/out/host/linux-x86/bin/adb}
AAPT2=${AAPT2:-$ANDROID_ROOT/out/host/linux-x86/bin/aapt2}
APK_OUT=${APK_OUT:-}
APK_DIR=${APK_DIR:-}
PROFILES_TSV=${PROFILES_TSV:-}
PROFILE_LIST=${PROFILE_LIST:-A B}
SER_A=${SER_A:-127.0.0.1:16521}
SER_B=${SER_B:-127.0.0.1:16522}
RUN_ROOT=${RUN_ROOT:-$ANDROID_ROOT/.worklog/synthetic-mthp-install/current}
STATE_DIR=${STATE_DIR:-$RUN_ROOT/state}
LOG_DIR=${LOG_DIR:-$RUN_ROOT/logs}
LOCK_FILE=${LOCK_FILE:-$STATE_DIR/install.lock}
PID_FILE=${PID_FILE:-$STATE_DIR/install.pid}
PGID_FILE=${PGID_FILE:-$STATE_DIR/install.pgid}
STATUS_FILE=${STATUS_FILE:-$STATE_DIR/status.env}
HEARTBEAT_FILE=${HEARTBEAT_FILE:-$STATE_DIR/heartbeat}
STOP_REASON_FILE=${STOP_REASON_FILE:-$STATE_DIR/stop_reason}
MAIN_LOG=${MAIN_LOG:-$LOG_DIR/install.log}
EXPECTED_COUNT=${EXPECTED_COUNT:-60}
ADB_WAIT_TIMEOUT_SEC=${ADB_WAIT_TIMEOUT_SEC:-180}
FORCE_REINSTALL=${FORCE_REINSTALL:-false}

mkdir -p "$STATE_DIR" "$LOG_DIR"

log() { printf '%s %s\n' "$(date -Is)" "$*" | tee -a "$MAIN_LOG"; }
set_status() {
  mkdir -p "$STATE_DIR"
  {
    echo "phase=$1"
    echo "detail=${2:-}"
    echo "updated=$(date -Is)"
    echo "run_root=$RUN_ROOT"
  } > "$STATUS_FILE"
  printf '%s phase=%s detail=%s\n' "$(date -Is)" "$1" "${2:-}" > "$HEARTBEAT_FILE"
}

realpath_maybe() { readlink -f "$1" 2>/dev/null || true; }

resolve_apk_out() {
  if [ -n "$APK_OUT" ]; then
    return 0
  fi
  APK_OUT=$(find "$ANDROID_ROOT/.worklog/synthetic-mthp-apk" -maxdepth 1 -type d -name 'out-*' 2>/dev/null | sort | tail -1)
}

prepare_inputs() {
  resolve_apk_out
  [ -n "$APK_OUT" ] || { log "FAIL no APK_OUT and no synthetic output dir"; return 2; }
  APK_DIR=${APK_DIR:-$APK_OUT/apks}
  PROFILES_TSV=${PROFILES_TSV:-$APK_OUT/profiles.tsv}
  [ -d "$APK_DIR" ] || { log "FAIL missing APK_DIR=$APK_DIR"; return 2; }
  [ -f "$PROFILES_TSV" ] || { log "FAIL missing PROFILES_TSV=$PROFILES_TSV"; return 2; }
  [ -x "$ADB" ] || { log "FAIL missing adb=$ADB"; return 2; }
  [ -x "$AAPT2" ] || { log "FAIL missing aapt2=$AAPT2"; return 2; }
}

serial_for_profile() {
  case "$1" in
    A) printf '%s\n' "$SER_A" ;;
    B) printf '%s\n' "$SER_B" ;;
    *) printf '%s\n' "$1" ;;
  esac
}

adb_state() {
  "$ADB" -s "$1" get-state 2>/dev/null | tr -d '\r' || true
}

wait_adb_device() {
  local serial=$1 deadline now
  deadline=$(( $(date +%s) + ADB_WAIT_TIMEOUT_SEC ))
  while true; do
    if [ "$(adb_state "$serial")" = device ]; then
      log "adb ready serial=$serial"
      return 0
    fi
    now=$(date +%s)
    if [ "$now" -ge "$deadline" ]; then
      log "FAIL adb wait timeout serial=$serial"
      return 3
    fi
    set_status waiting_adb "serial=$serial"
    sleep 5
  done
}

wait_boot_completed() {
  local serial=$1 deadline now boot package_ready
  deadline=$(( $(date +%s) + ADB_WAIT_TIMEOUT_SEC ))
  while true; do
    boot=$("$ADB" -s "$serial" shell 'getprop sys.boot_completed' </dev/null 2>/dev/null | tr -d '\r[:space:]' || true)
    package_ready=$("$ADB" -s "$serial" shell 'cmd package list packages android >/dev/null 2>&1 && echo ok || echo no' </dev/null 2>/dev/null | tr -d '\r[:space:]' || true)
    if [ "$boot" = 1 ] && [ "$package_ready" = ok ]; then
      log "boot/package ready serial=$serial"
      return 0
    fi
    now=$(date +%s)
    if [ "$now" -ge "$deadline" ]; then
      log "FAIL boot wait timeout serial=$serial boot=$boot package_ready=$package_ready"
      return 3
    fi
    set_status waiting_boot "serial=$serial boot=$boot package_ready=$package_ready"
    sleep 5
  done
}

disable_verifier() {
  local serial=$1
  "$ADB" -s "$serial" shell 'settings put global verifier_verify_adb_installs 0 || true; settings put global package_verifier_enable 0 || true; settings put global upload_apk_enable 0 || true; settings put secure package_verifier_user_consent -1 || true' </dev/null >>"$MAIN_LOG" 2>&1 || true
}

package_count() {
  local serial=$1
  "$ADB" -s "$serial" shell 'cmd package list packages com.zzhao.mthp.synth 2>/dev/null | wc -l' </dev/null 2>/dev/null | tr -d '\r ' || true
}

package_installed() {
  local serial=$1 pkg=$2
  "$ADB" -s "$serial" shell "pm path '$pkg' >/dev/null 2>&1" </dev/null
}

apk_package_name() {
  local apk=$1
  "$AAPT2" dump packagename "$apk" 2>/dev/null | head -1 | tr -d '\r'
}

write_apk_manifest() {
  local out=$1
  : > "$out"
  awk -F'\t' '
    NR == 1 {
      for (i = 1; i <= NF; i++) {
        if ($i == "package") pkg_col = i;
        if ($i == "apk") apk_col = i;
      }
      next;
    }
    pkg_col && apk_col { print $pkg_col "\t" $apk_col }
  ' "$PROFILES_TSV" | while IFS=$'\t' read -r pkg apk_rel; do
    [ -n "${pkg:-}" ] || continue
    local_apk="$ANDROID_ROOT/$apk_rel"
    if [ ! -f "$local_apk" ]; then
      local_apk="$APK_DIR/$(basename "$apk_rel")"
    fi
    if [ -f "$local_apk" ]; then
      printf '%s\t%s\n' "$pkg" "$local_apk" >> "$out"
    fi
  done
  if [ ! -s "$out" ]; then
    find "$APK_DIR" -maxdepth 1 -type f -name '*.apk' | sort | while read -r apk; do
      pkg=$(apk_package_name "$apk")
      [ -n "$pkg" ] && printf '%s\t%s\n' "$pkg" "$apk" >> "$out"
    done
  fi
}

install_profile() {
  local profile=$1 serial manifest out_dir count rc install_out pkg apk
  serial=$(serial_for_profile "$profile")
  out_dir="$RUN_ROOT/install-$profile"
  mkdir -p "$out_dir"
  manifest="$out_dir/apk-manifest.tsv"
  write_apk_manifest "$manifest"
  count=$(wc -l < "$manifest" | tr -d ' ')
  log "profile=$profile serial=$serial apk_manifest_count=$count manifest=$manifest"
  if [ "${count:-0}" -lt "$EXPECTED_COUNT" ]; then
    log "FAIL profile=$profile expected at least $EXPECTED_COUNT APKs, got $count"
    return 4
  fi
  wait_adb_device "$serial" || return 5
  wait_boot_completed "$serial" || return 5
  disable_verifier "$serial"
  : > "$out_dir/success.tsv"
  : > "$out_dir/skip.tsv"
  : > "$out_dir/fail.tsv"
  : > "$out_dir/detail.log"
  while IFS=$'\t' read -r pkg apk; do
    [ -n "${pkg:-}" ] || continue
    [ -f "$apk" ] || { printf '%s\tmissing-apk\t%s\n' "$pkg" "$apk" >> "$out_dir/fail.tsv"; continue; }
    set_status install "profile=$profile pkg=$pkg"
    if [ "$FORCE_REINSTALL" != true ] && package_installed "$serial" "$pkg"; then
      printf '%s\talready-installed\t%s\n' "$pkg" "$apk" >> "$out_dir/skip.tsv"
      continue
    fi
    if [ "$FORCE_REINSTALL" = true ]; then
      "$ADB" -s "$serial" shell "am force-stop '$pkg' >/dev/null 2>&1; pm uninstall --user 0 '$pkg' >/dev/null 2>&1 || pm uninstall '$pkg' >/dev/null 2>&1 || true" </dev/null >>"$out_dir/detail.log" 2>&1 || true
    fi
    install_out=$("$ADB" -s "$serial" install --no-incremental -r -g "$apk" </dev/null 2>&1)
    rc=$?
    printf '%s\n' "$install_out" >> "$out_dir/detail.log"
    if [ "$rc" -eq 0 ] && printf '%s\n' "$install_out" | grep -q 'Success'; then
      printf '%s\t%s\tSuccess\n' "$pkg" "$apk" >> "$out_dir/success.tsv"
      log "INSTALL success profile=$profile pkg=$pkg"
    else
      printf '%s\t%s\t%s\n' "$pkg" "$apk" "$(printf '%s' "$install_out" | tr '\n' ' ')" >> "$out_dir/fail.tsv"
      log "INSTALL fail profile=$profile pkg=$pkg rc=$rc"
    fi
  done < "$manifest"
  count=$(package_count "$serial")
  "$ADB" -s "$serial" shell 'cmd package list packages com.zzhao.mthp.synth | sort' </dev/null > "$out_dir/packages.txt" 2>/dev/null || true
  log "profile=$profile final_count=$count"
  if [ "${count:-0}" -lt "$EXPECTED_COUNT" ]; then
    log "FAIL profile=$profile final_count=$count expected=$EXPECTED_COUNT"
    return 6
  fi
  local fail_count
  fail_count=$(wc -l < "$out_dir/fail.tsv" | tr -d ' ')
  if [ "${fail_count:-0}" -ne 0 ]; then
    log "FAIL profile=$profile install_fail_count=$fail_count"
    return 7
  fi
}

run_install() {
  prepare_inputs || return $?
  echo $$ > "$PID_FILE"
  ps -o pgid= -p $$ | tr -d ' ' > "$PGID_FILE" 2>/dev/null || true
  trap 'echo "signal $(date -Is)" > "$STOP_REASON_FILE"; set_status exiting signal; exit 143' INT TERM HUP
  trap 'rc=$?; echo "exit rc=$rc $(date -Is)" >> "$STOP_REASON_FILE"; if [ "$rc" -ne 0 ]; then set_status exiting "exit rc=$rc"; fi' EXIT
  log "START synthetic install pid=$$ pgid=$(cat "$PGID_FILE" 2>/dev/null || true) apk_out=$APK_OUT profiles=$PROFILE_LIST"
  for profile in $PROFILE_LIST; do
    install_profile "$profile" || return $?
  done
  set_status complete "profiles=$PROFILE_LIST"
  log "PASS synthetic APK install profiles=$PROFILE_LIST run_root=$RUN_ROOT"
}

status_cmd() {
  echo "run_root=$RUN_ROOT"
  echo "log=$MAIN_LOG"
  if [ -f "$PID_FILE" ]; then
    pid=$(cat "$PID_FILE" 2>/dev/null || true)
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      echo "running=1 pid=$pid pgid=$(cat "$PGID_FILE" 2>/dev/null || true)"
    else
      echo "running=0 stale_pid=$pid"
    fi
  else
    echo "running=0"
  fi
  [ -f "$STATUS_FILE" ] && cat "$STATUS_FILE" || true
  [ -f "$HEARTBEAT_FILE" ] && echo "heartbeat=$(cat "$HEARTBEAT_FILE")" || true
}

stop_cmd() {
  local pid="" pgid=""
  [ -f "$PID_FILE" ] && pid=$(cat "$PID_FILE" 2>/dev/null || true)
  [ -f "$PGID_FILE" ] && pgid=$(cat "$PGID_FILE" 2>/dev/null || true)
  echo "manual-stop $(date -Is)" > "$STOP_REASON_FILE"
  if [ -n "$pgid" ]; then
    kill -TERM "-$pgid" 2>/dev/null || true
    for _ in $(seq 1 30); do
      [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null || break
      sleep 1
    done
    [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null && kill -KILL "-$pgid" 2>/dev/null || true
  elif [ -n "$pid" ]; then
    kill -TERM "$pid" 2>/dev/null || true
  fi
  status_cmd
}

cmd=${1:-run}
case "$cmd" in
  run)
    exec 9>"$LOCK_FILE"
    if ! flock -n 9; then
      echo "FAIL: another install holds $LOCK_FILE" >&2
      exit 1
    fi
    run_install
    exit $?
    ;;
  status) status_cmd ;;
  stop) stop_cmd ;;
  *) echo "usage: $0 {run|status|stop}" >&2; exit 64 ;;
esac
