#!/usr/bin/env bash
set -u -o pipefail

ANDROID_ROOT=${ANDROID_ROOT:-/home/nzzhao/learn_os/android17}
ADB=${ADB:-$ANDROID_ROOT/out/host/linux-x86/bin/adb}
APK_OUT=${APK_OUT:-$(cat "$ANDROID_ROOT/.worklog/latest-guard-synth-apk-out.txt" 2>/dev/null || echo "$ANDROID_ROOT/.worklog/synthetic-mthp-apk/out-20260713-173103-guard-vma-full60")}
OUT_ROOT=${OUT_ROOT:-$ANDROID_ROOT/.worklog/scudo-java-snapshots/snapshot-$(date +%Y%m%d-%H%M%S)}
PACKAGE_FILE=${PACKAGE_FILE:-$ANDROID_ROOT/.worklog/scudo-java-snapshot-top20-packages.txt}
COUNT=${COUNT:-20}
SCUDO_MB=${SCUDO_MB:-50}
JAVA_MB=${JAVA_MB:-50}
JAVA_CHURN_MS=${JAVA_CHURN_MS:-0}
VMA_COUNT_SCALE=${VMA_COUNT_SCALE:-0.5}
ANON_VMA_SIZE_SCALE=${ANON_VMA_SIZE_SCALE:-0.1}
COW_PAGES_SCALE=${COW_PAGES_SCALE:-0.1}
FILEMAP_SIZE_SCALE=${FILEMAP_SIZE_SCALE:-0.3}
DLOPEN_LIB_COUNT_SCALE=${DLOPEN_LIB_COUNT_SCALE:-0.5}
SNAPSHOT_MIN_COUNT=${SNAPSHOT_MIN_COUNT:-3}
SNAPSHOT_MAX_COUNT=${SNAPSHOT_MAX_COUNT:-5}
SNAPSHOT_GAP_SEC=${SNAPSHOT_GAP_SEC:-10}
STABLE_DELTA_MIB=${STABLE_DELTA_MIB:-16}
SER_A=${SER_A:-127.0.0.1:16521}
SER_B=${SER_B:-127.0.0.1:16522}

cmd=${1:-run}
shift || true

mkdir -p "$OUT_ROOT"

log() { printf '%s %s\n' "$(date -Is)" "$*" | tee -a "$OUT_ROOT/snapshot.log"; }

ensure_package_file() {
  if [ -s "$PACKAGE_FILE" ]; then
    return 0
  fi
  python3 - "$APK_OUT" "$PACKAGE_FILE" "$COUNT" <<'PY'
import csv, pathlib, sys
apk_out=pathlib.Path(sys.argv[1])
out=pathlib.Path(sys.argv[2])
count=int(sys.argv[3])
rows=list(csv.DictReader((apk_out/'profiles.tsv').open(), delimiter='\t'))
chosen=sorted(rows, key=lambda r:(int(r.get('scudo_live_mb') or 0), int(r.get('java_live_mb') or 0)), reverse=True)[:count]
out.write_text('\n'.join(r['package'] for r in chosen)+'\n')
PY
}

build_override_json() {
  local pkg=$1
  python3 - "$APK_OUT" "$pkg" "$SCUDO_MB" "$JAVA_MB" "$JAVA_CHURN_MS" <<'PY'
import csv, json, pathlib, sys
apk_out=pathlib.Path(sys.argv[1])
pkg=sys.argv[2]
scudo=int(sys.argv[3])
java=int(sys.argv[4])
churn=int(sys.argv[5])
rows=list(csv.DictReader((apk_out/'profiles.tsv').open(), delimiter='\t'))
row=next(r for r in rows if r['package']==pkg)
out={}
for k,v in row.items():
    if k in {'apk','apk_bytes','profile_name','label','package'} or v == '':
        continue
    try:
        out[k]=int(v)
    except ValueError:
        out[k]=v
out['scudo_live_mb']=scudo
out['java_live_mb']=java
out['process_count']=1
out['scudo_threads']=1
out['java_churn_ms']=churn
out['gc_period_ms']=0
print(json.dumps(out, separators=(',', ':')))
PY
}

component_for_pkg() {
  local serial=$1 pkg=$2
  local comp
  comp=$("$ADB" -s "$serial" shell "cmd package resolve-activity --brief -a android.intent.action.MAIN -c android.intent.category.LAUNCHER '$pkg' 2>/dev/null" </dev/null 2>/dev/null | tr -d '\r' | awk '/\// {line=$0} END {print line}')
  if [ -z "$comp" ]; then
    comp="$pkg/com.zzhao.mthp.synthetic.WorkloadRuntime\$MainActivity"
  fi
  printf '%s\n' "$comp"
}

install_if_needed() {
  local serial=$1 pkg=$2
  if "$ADB" -s "$serial" shell "pm path '$pkg' >/dev/null 2>&1" </dev/null; then
    return 0
  fi
  local apk
  apk=$(python3 - "$APK_OUT" "$pkg" <<'PY'
import csv, pathlib, sys
apk_out=pathlib.Path(sys.argv[1])
pkg=sys.argv[2]
for r in csv.DictReader((apk_out/'profiles.tsv').open(), delimiter='\t'):
    if r['package']==pkg:
        print(r['apk'])
        break
PY
)
  [ -n "$apk" ] || return 2
  "$ADB" -s "$serial" install --no-incremental -r -g "$apk" </dev/null >/dev/null
}

launch_profile() {
  local serial=$1 label=$2 out_dir=$3
  mkdir -p "$out_dir"
  : >"$out_dir/launch.tsv"
  local idx=0 pkg comp json rc
  while read -r pkg; do
    [ -n "${pkg:-}" ] || continue
    idx=$((idx + 1))
    [ "$idx" -le "$COUNT" ] || break
    install_if_needed "$serial" "$pkg" || true
    comp=$(component_for_pkg "$serial" "$pkg")
    json=$(build_override_json "$pkg")
    "$ADB" -s "$serial" shell "am force-stop '$pkg'" </dev/null >/dev/null 2>&1 || true
    "$ADB" -s "$serial" shell am start -n "$comp" \
      --es zz_mthp_profile_json "$json" \
      --ef zz_mthp_vma_count_scale "$VMA_COUNT_SCALE" \
      --ef zz_mthp_anon_vma_size_scale "$ANON_VMA_SIZE_SCALE" \
      --ef zz_mthp_cow_pages_scale "$COW_PAGES_SCALE" \
      --ef zz_mthp_filemap_size_scale "$FILEMAP_SIZE_SCALE" \
      --ef zz_mthp_dlopen_lib_count_scale "$DLOPEN_LIB_COUNT_SCALE" \
      </dev/null >"$out_dir/launch_${idx}_${pkg}.log" 2>&1
    rc=$?
    printf '%s\t%s\t%s\t%s\n' "$idx" "$pkg" "$rc" "$comp" >>"$out_dir/launch.tsv"
  done <"$PACKAGE_FILE"
}

collect_smaps() {
  local serial=$1 label=$2 out_dir=$3
  mkdir -p "$out_dir/smaps" "$out_dir/maps"
  "$ADB" -s "$serial" shell 'cat > /data/local/tmp/collect_scudo_java_smaps.py <<"PY"
#!/system/bin/python3
import os, re, sys
pkgs=set(sys.argv[1:])
print("pid\tprocess\tcomponent\tsize_kb\trss_kb\tpss_kb\tprivate_dirty_kb\tshared_dirty_kb\tswap_kb\tvma_count")
for pid in sorted(x for x in os.listdir("/proc") if x.isdigit()):
    try:
        cmd=open(f"/proc/{pid}/cmdline","rb").read().replace(b"\0",b" ").decode(errors="ignore").strip()
    except Exception:
        continue
    if not any(cmd.startswith(pkg) for pkg in pkgs):
        continue
    cur=None
    totals={}
    counts={}
    def flush(v):
        if not v: return
        name=v.get("name","")
        if "[anon:scudo" in name:
            comp="scudo"
        elif "[anon:dalvik" in name:
            lower=name.lower()
            if "large object" in lower:
                comp="dalvik_large_object"
            elif "main space" in lower:
                comp="dalvik_main_space"
            elif "zygote" in lower:
                comp="dalvik_zygote_space"
            elif "non moving" in lower:
                comp="dalvik_non_moving_space"
            elif "linearalloc" in lower or "linear-alloc" in lower:
                comp="dalvik_linearalloc"
            elif "boot" in lower or "/system/framework/" in lower:
                comp="dalvik_boot_image"
            elif any(token in lower for token in ("bitmap", "card table", "mark-compact", "allocation stack", "live stack")):
                comp="dalvik_gc_metadata"
            elif "compiler" in lower or "ref table" in lower or "sentinel" in lower:
                comp="dalvik_runtime_metadata"
            else:
                comp="dalvik_other"
        elif "[anon:mthp_vma_" in name:
            comp="mthp_vma"
        elif "[anon:mthp_guard_" in name:
            comp="mthp_guard"
        else:
            return
        t=totals.setdefault(comp,{"Size":0,"Rss":0,"Pss":0,"Private_Dirty":0,"Shared_Dirty":0,"Swap":0})
        counts[comp]=counts.get(comp,0)+1
        for k in t:
            t[k]+=v.get(k,0)
    try:
        with open(f"/proc/{pid}/smaps", "r", errors="ignore") as f:
            for line in f:
                if re.match(r"^[0-9a-f]+-[0-9a-f]+ ", line):
                    flush(cur)
                    cur={"name":line.rstrip("\n")}
                elif cur is not None and ":" in line:
                    key,val=line.split(":",1)
                    if key in ("Size","Rss","Pss","Private_Dirty","Shared_Dirty","Swap"):
                        cur[key]=int(val.strip().split()[0])
            flush(cur)
    except Exception:
        continue
    for comp,t in sorted(totals.items()):
        print(f"{pid}\t{cmd}\t{comp}\t{t['Size']}\t{t['Rss']}\t{t['Pss']}\t{t['Private_Dirty']}\t{t['Shared_Dirty']}\t{t['Swap']}\t{counts.get(comp,0)}")
PY
chmod 755 /data/local/tmp/collect_scudo_java_smaps.py' </dev/null >/dev/null
  local pkg_args=()
  while read -r pkg; do [ -n "${pkg:-}" ] && pkg_args+=("$pkg"); [ "${#pkg_args[@]}" -ge "$COUNT" ] && break; done <"$PACKAGE_FILE"
  "$ADB" -s "$serial" shell python3 /data/local/tmp/collect_scudo_java_smaps.py "${pkg_args[@]}" </dev/null >"$out_dir/smaps_components.tsv"
  "$ADB" -s "$serial" shell 'for p in "$@"; do pidof "$p" 2>/dev/null | tr " " "\n" | sed "/^$/d" | while read pid; do echo "== $p $pid =="; cat /proc/$pid/maps; done; done' sh "${pkg_args[@]}" </dev/null >"$out_dir/maps/all_maps.txt" 2>/dev/null || true
  "$ADB" -s "$serial" logcat -d -v threadtime </dev/null >"$out_dir/logcat.txt" 2>/dev/null || true
}

summarize() {
  python3 - "$OUT_ROOT" <<'PY'
import csv, pathlib, sys, collections
root=pathlib.Path(sys.argv[1])
rows=[]
for label in ['A','B']:
    p=root/label/'smaps_components.tsv'
    if not p.exists(): continue
    for r in csv.DictReader(p.open(), delimiter='\t'):
        r['label']=label
        for k in ['size_kb','rss_kb','pss_kb','private_dirty_kb','shared_dirty_kb','swap_kb','vma_count']:
            r[k]=int(r[k])
        rows.append(r)
(root/'all_components.tsv').write_text('')
if rows:
    with (root/'all_components.tsv').open('w', newline='') as f:
        w=csv.DictWriter(f, fieldnames=['label','pid','process','component','size_kb','rss_kb','pss_kb','private_dirty_kb','shared_dirty_kb','swap_kb','vma_count'], delimiter='\t')
        w.writeheader(); w.writerows(rows)
summary=collections.defaultdict(lambda: collections.Counter())
proc_count=collections.defaultdict(set)
for r in rows:
    key=(r['label'],r['component'])
    for k in ['size_kb','rss_kb','pss_kb','private_dirty_kb','shared_dirty_kb','swap_kb','vma_count']:
        summary[key][k]+=r[k]
    proc_count[key].add(r['pid'])
with (root/'summary_by_component.tsv').open('w') as f:
    f.write('label\tcomponent\tprocesses\tsize_mib\trss_mib\tpss_mib\tprivate_dirty_mib\tswap_mib\tvma_count\n')
    for key,c in sorted(summary.items()):
        label,comp=key
        f.write(f"{label}\t{comp}\t{len(proc_count[key])}\t{c['size_kb']/1024:.1f}\t{c['rss_kb']/1024:.1f}\t{c['pss_kb']/1024:.1f}\t{c['private_dirty_kb']/1024:.1f}\t{c['swap_kb']/1024:.1f}\t{c['vma_count']}\n")
with (root/'README.md').open('w') as f:
    f.write('# Scudo/Java Snapshot\n\n')
    f.write(f'- output root: `{root}`\n')
    f.write('- Scudo/Dalvik are separated by smaps VMA name: `[anon:scudo*]` vs `[anon:dalvik*]`.\n')
    f.write('- Size is virtual mapping size; RSS/PSS/Private_Dirty indicate faulted resident memory.\n\n')
    f.write('## Summary by component\n\n')
    if (root/'summary_by_component.tsv').exists():
        lines=(root/'summary_by_component.tsv').read_text().splitlines()
        if lines:
            f.write('| ' + ' | '.join(lines[0].split('\t')) + ' |\n')
            f.write('|---' * len(lines[0].split('\t')) + '|\n')
            for line in lines[1:]:
                f.write('| ' + ' | '.join(line.split('\t')) + ' |\n')
print(root/'README.md')
PY
}

run_one() {
  local label=$1 serial=$2
  local out="$OUT_ROOT/$label"
  log "START label=$label serial=$serial scudo_mb=$SCUDO_MB java_mb=$JAVA_MB packages=$(wc -l < "$PACKAGE_FILE")"
  "$ADB" -s "$serial" wait-for-device
  "$ADB" -s "$serial" root >/dev/null 2>&1 || true
  sleep 1
  "$ADB" -s "$serial" wait-for-device
  "$ADB" -s "$serial" logcat -c >/dev/null 2>&1 || true
  launch_profile "$serial" "$label" "$out"
  local stability="$out/snapshot_stability.tsv"
  printf 'snapshot\tsize_kb\trss_kb\tpss_kb\tprivate_dirty_kb\tswap_kb\tvma_count\tdelta_rss_kb\tstable\n' >"$stability"
  local prev_rss=-1
  local stable_delta_kb=$((STABLE_DELTA_MIB * 1024))
  local snap size_kb rss_kb pss_kb private_kb swap_kb vma_count delta_rss abs_delta stable
  for snap in $(seq 1 "$SNAPSHOT_MAX_COUNT"); do
    local snap_dir="$out/snapshot_${snap}"
    collect_smaps "$serial" "$label" "$snap_dir"
    cp -f "$snap_dir/smaps_components.tsv" "$out/smaps_components.tsv"
    read -r size_kb rss_kb pss_kb private_kb swap_kb vma_count <<EOF
$(awk -F'\t' 'NR>1 {size+=$4; rss+=$5; pss+=$6; priv+=$7; swap+=$9; vma+=$10} END {printf "%d %d %d %d %d %d", size, rss, pss, priv, swap, vma}' "$out/smaps_components.tsv")
EOF
    delta_rss=0
    stable=0
    if [ "$prev_rss" -ge 0 ]; then
      delta_rss=$((rss_kb - prev_rss))
      abs_delta=${delta_rss#-}
      if [ "$abs_delta" -le "$stable_delta_kb" ]; then
        stable=1
      fi
    fi
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
      "$snap" "$size_kb" "$rss_kb" "$pss_kb" "$private_kb" "$swap_kb" "$vma_count" "$delta_rss" "$stable" >>"$stability"
    log "SNAPSHOT label=$label snap=$snap rss_mib=$((rss_kb / 1024)) delta_rss_kb=$delta_rss stable=$stable"
    if [ "$snap" -ge "$SNAPSHOT_MIN_COUNT" ] && [ "$stable" -eq 1 ]; then
      break
    fi
    if [ "$snap" -lt "$SNAPSHOT_MAX_COUNT" ]; then
      sleep "$SNAPSHOT_GAP_SEC"
    fi
    prev_rss=$rss_kb
  done
  log "DONE label=$label out=$out"
}

case "$cmd" in
  run)
    ensure_package_file
    cp -f "$PACKAGE_FILE" "$OUT_ROOT/packages.txt"
    {
      echo "SCUDO_MB=$SCUDO_MB"
      echo "JAVA_MB=$JAVA_MB"
      echo "JAVA_CHURN_MS=$JAVA_CHURN_MS"
      echo "VMA_COUNT_SCALE=$VMA_COUNT_SCALE"
      echo "ANON_VMA_SIZE_SCALE=$ANON_VMA_SIZE_SCALE"
      echo "COW_PAGES_SCALE=$COW_PAGES_SCALE"
      echo "FILEMAP_SIZE_SCALE=$FILEMAP_SIZE_SCALE"
      echo "DLOPEN_LIB_COUNT_SCALE=$DLOPEN_LIB_COUNT_SCALE"
      echo "SNAPSHOT_MIN_COUNT=$SNAPSHOT_MIN_COUNT"
      echo "SNAPSHOT_MAX_COUNT=$SNAPSHOT_MAX_COUNT"
      echo "SNAPSHOT_GAP_SEC=$SNAPSHOT_GAP_SEC"
      echo "STABLE_DELTA_MIB=$STABLE_DELTA_MIB"
      echo "APK_OUT=$APK_OUT"
    } >"$OUT_ROOT/params.env"
    run_one A "$SER_A"
    if "$ADB" -s "$SER_B" get-state >/dev/null 2>&1; then
      run_one B "$SER_B"
    else
      log "SKIP B serial=$SER_B not online"
    fi
    summarize
    ;;
  summarize) summarize ;;
  *) echo "usage: $0 run|summarize" >&2; exit 64 ;;
esac
