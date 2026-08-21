#!/usr/bin/env python3
import argparse, csv, os, re, subprocess, sys, time
from pathlib import Path
from statistics import median

ANON_KIND_PATTERNS = [
    ("scudo", re.compile(r"\[anon:scudo", re.I)),
    ("dalvik", re.compile(r"\[anon:dalvik", re.I)),
    ("special_anon", re.compile(r"\[anon:", re.I)),
    ("malloc_heap", re.compile(r"\[heap\]")),
    ("stack", re.compile(r"\[stack")),
]

def run(cmd, check=False, text=True, timeout=None):
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=text, timeout=timeout)
    if check and p.returncode != 0:
        raise RuntimeError(f"cmd failed rc={p.returncode}: {' '.join(cmd)}\nSTDERR:\n{p.stderr[-2000:]}")
    return p

def adb(serial, args, **kw):
    return run(["adb", "-s", serial] + args, **kw)

def shell(serial, cmd, **kw):
    return adb(serial, ["shell", cmd], **kw)

def exec_out(serial, cmd, **kw):
    return adb(serial, ["exec-out", cmd], **kw)

def classify(path):
    if not path:
        return "anonymous_unlabeled"
    for name, pat in ANON_KIND_PATTERNS:
        if pat.search(path):
            return name
    return "other"

def parse_ps(ps_text):
    procs = []
    for line in ps_text.splitlines():
        line = line.strip()
        if not line or line.startswith("PID "):
            continue
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        pid, ppid, user, name = parts
        if pid.isdigit():
            procs.append((int(pid), int(ppid) if ppid.isdigit() else -1, user, name))
    return procs

def parse_packages(pkg_text):
    pkgs = set()
    for line in pkg_text.splitlines():
        line = line.strip()
        if line.startswith("package:"):
            pkgs.add(line.split(":", 1)[1])
    return pkgs

def proc_pkg(name, packages):
    base = name.split(":", 1)[0]
    if base in packages:
        return base
    # Fallback: process names are sometimes truncated, but keep only app-like names.
    if "." in base and not base.startswith(("android.", "com.android.", "vendor.", "system.")):
        return base
    return ""

def is_anon_mapping(path):
    if path == "" or path.startswith("[anon:") or path.startswith("[heap]") or path.startswith("[stack"):
        return True
    return False

def parse_smaps(pkg, pid, proc, text):
    records = []
    cur = None
    header_re = re.compile(r"^([0-9a-fA-F]+)-([0-9a-fA-F]+)\s+(\S+)\s+\S+\s+\S+\s+\S+\s*(.*)$")
    numeric_keys = {"Size", "Rss", "Pss", "Private_Dirty", "Private_Clean", "Shared_Dirty", "Shared_Clean", "Referenced", "Anonymous", "AnonHugePages", "ShmemPmdMapped", "FilePmdMapped", "Shared_Hugetlb", "Private_Hugetlb", "Swap", "SwapPss", "KernelPageSize", "MMUPageSize"}
    def finish():
        if not cur:
            return
        if is_anon_mapping(cur["path"]):
            cur["kind"] = classify(cur["path"])
            records.append(cur.copy())
    for line in text.splitlines():
        m = header_re.match(line)
        if m:
            finish()
            start, end, perms, path = m.groups()
            cur = {
                "pkg": pkg, "pid": pid, "proc": proc,
                "start": int(start, 16), "end": int(end, 16),
                "perms": perms, "path": path.strip(),
            }
            for k in numeric_keys:
                cur[k] = 0
            continue
        if cur is None or ":" not in line:
            continue
        key, rest = line.split(":", 1)
        if key in numeric_keys:
            mm = re.search(r"(-?\d+)", rest)
            if mm:
                cur[key] = int(mm.group(1))
    finish()
    return records

def percentile(vals, p):
    if not vals:
        return 0
    vals = sorted(vals)
    idx = min(len(vals) - 1, int(round((len(vals) - 1) * p / 100.0)))
    return vals[idx]

def summarize(records, keys):
    groups = {}
    for r in records:
        g = tuple(r[k] for k in keys)
        groups.setdefault(g, []).append(r)
    rows = []
    for g, recs in groups.items():
        sizes = [r["Size"] for r in recs]
        rss = [r["Rss"] for r in recs]
        row = {k: v for k, v in zip(keys, g)}
        row.update({
            "count": len(recs),
            "size_mib": round(sum(sizes) / 1024, 2),
            "rss_mib": round(sum(rss) / 1024, 2),
            "pss_mib": round(sum(r["Pss"] for r in recs) / 1024, 2),
            "anon_mib": round(sum(r["Anonymous"] for r in recs) / 1024, 2),
            "swap_mib": round(sum(r["Swap"] for r in recs) / 1024, 2),
            "p50_size_kb": percentile(sizes, 50),
            "p75_size_kb": percentile(sizes, 75),
            "p90_size_kb": percentile(sizes, 90),
            "p95_size_kb": percentile(sizes, 95),
            "p99_size_kb": percentile(sizes, 99),
            "max_size_kb": max(sizes) if sizes else 0,
            "p50_rss_kb": percentile(rss, 50),
            "p90_rss_kb": percentile(rss, 90),
            "max_rss_kb": max(rss) if rss else 0,
            "vma_ge_1m": sum(1 for x in sizes if x >= 1024),
            "vma_ge_16m": sum(1 for x in sizes if x >= 16*1024),
            "vma_ge_64m": sum(1 for x in sizes if x >= 64*1024),
            "vma_ge_256m": sum(1 for x in sizes if x >= 256*1024),
            "vma_rss_ge_64k": sum(1 for x in rss if x >= 64),
            "vma_rss_ge_1m": sum(1 for x in rss if x >= 1024),
        })
        rows.append(row)
    return rows

def write_tsv(path, rows, fields):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, delimiter="\t", extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--serial", default="18281FDF6007HB")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--max-procs", type=int, default=240)
    ap.add_argument("--include-system", action="store_true")
    args = ap.parse_args()
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "command.txt").write_text(" ".join(sys.argv) + "\n")
    from utils import adb_utils
    try:
        su_out = adb_utils.adb_shell_root(args.serial, "id", timeout_s=15, check=False)
    except Exception as e:
        su_out = f"ERR: {e}"
    (out / "su_status.txt").write_text(su_out)
    packages = parse_packages(shell(args.serial, "pm list packages -3", text=True, check=True).stdout)
    (out / "packages_3p.txt").write_text("\n".join(sorted(packages)) + "\n")
    ps = shell(args.serial, "ps -A -o PID,PPID,USER,NAME", text=True, check=True).stdout
    (out / "ps.txt").write_text(ps)
    procs = []
    for pid, ppid, user, name in parse_ps(ps):
        pkg = proc_pkg(name, packages)
        if not pkg and not args.include_system:
            continue
        if pid <= 0 or name.startswith("["):
            continue
        if not args.include_system and not pkg:
            continue
        procs.append((pid, pkg or name.split(":",1)[0], name))
    procs = procs[:args.max_procs]
    records = []
    failures = []
    for idx, (pid, pkg, proc) in enumerate(procs, 1):
        from utils import adb_utils
        smaps = adb_utils.adb_shell_root(args.serial, f"cat /proc/{pid}/smaps",
                                         timeout_s=20, check=False)
        p = subprocess.CompletedProcess(["adb", "shell"], 0, stdout=smaps, stderr="")
        if p.returncode != 0 or not p.stdout:
            failures.append({"pid": pid, "pkg": pkg, "proc": proc, "rc": p.returncode, "stderr": p.stderr[-300:]})
            continue
        (out / f"smaps_{pid}_{proc.replace('/', '_').replace(':', '_')}.txt").write_text(p.stdout)
        records.extend(parse_smaps(pkg, pid, proc, p.stdout))
    fields = ["pkg","pid","proc","kind","path","perms","Size","Rss","Pss","Private_Dirty","Private_Clean","Shared_Dirty","Shared_Clean","Referenced","Anonymous","Swap","SwapPss","AnonHugePages","KernelPageSize","MMUPageSize","start","end"]
    write_tsv(out / "anon_vmas.tsv", records, fields)
    summary_fields = ["kind","count","size_mib","rss_mib","pss_mib","anon_mib","swap_mib","p50_size_kb","p75_size_kb","p90_size_kb","p95_size_kb","p99_size_kb","max_size_kb","p50_rss_kb","p90_rss_kb","max_rss_kb","vma_ge_1m","vma_ge_16m","vma_ge_64m","vma_ge_256m","vma_rss_ge_64k","vma_rss_ge_1m"]
    kind_rows = sorted(summarize(records, ["kind"]), key=lambda r: r["size_mib"], reverse=True)
    write_tsv(out / "anon_kind_summary.tsv", kind_rows, summary_fields)
    proc_fields = ["pkg","pid","proc","count","size_mib","rss_mib","pss_mib","anon_mib","swap_mib","p50_size_kb","p90_size_kb","p99_size_kb","max_size_kb","vma_ge_1m","vma_ge_16m","vma_ge_64m","vma_ge_256m","vma_rss_ge_64k","vma_rss_ge_1m"]
    proc_rows = sorted(summarize(records, ["pkg","pid","proc"]), key=lambda r: r["rss_mib"], reverse=True)
    write_tsv(out / "anon_process_summary.tsv", proc_rows, proc_fields)
    top = sorted(records, key=lambda r: (r["Size"], r["Rss"]), reverse=True)[:2000]
    write_tsv(out / "anon_top_vmas.tsv", top, fields)
    rss_top = sorted(records, key=lambda r: (r["Rss"], r["Size"]), reverse=True)[:2000]
    write_tsv(out / "anon_top_rss_vmas.tsv", rss_top, fields)
    write_tsv(out / "failures.tsv", failures, ["pid","pkg","proc","rc","stderr"])
    print(f"out_dir={out}")
    print(f"packages={len(packages)} procs_selected={len(procs)} records={len(records)} failures={len(failures)}")
    for r in kind_rows[:10]:
        print(r)

if __name__ == "__main__":
    main()
