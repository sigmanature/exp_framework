"""cycle_sample 统一只读采样（设备端单进程 + 每 probe 一个 CSV）。

设计（用户确认的最终形态）：
  - 只读样本抽象成 probe 声明：
      kind:  file(单路径) / files(模板+key) / dir(目录+key 文件)
      parse: kv / table(buddyinfo|pagetypeinfo)
  - 设备端脚本不解析：file 原样 cat；dir/files 逐 key 输出单行 "key value"。
    每 probe 一个 interval_s（缺省用 cycle_sample.interval_s，再缺省 60）。
  - 周期采样：device 循环每秒 tick，到点把各 probe 原始输出追加到
    raw/<name>.log（同一 tick 共享同一个设备时间戳 TS）。
  - sample_end：STOP 停进程 → host pull raw → 通用引擎解析成
    <name>_samples.csv（首列 host_ts=设备日期秒）。

解析引擎只有两类，与协议无关：
  - kv 引擎：任何 "key value" / "key=value" 行 → 按 probe.keys 过滤成列；
  - rows 引擎：任何 "标签对… + 尾部整数序列" 的行表 → 按 probe.label_keys
    取标签拼列名（如 buddyinfo/pagetypeinfo），无需识别具体文件。

派生列（如 folio totals）不在这里算——通用采样器不感知某个协议的语义。
"""
from __future__ import annotations

import csv
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from exp_framework.utils import adb_utils

PROBE_DIR = "/data/local/tmp/cycle_probe"
PROBE_SCRIPT = f"{PROBE_DIR}/sampler.sh"
STOP_FILE = f"{PROBE_DIR}/stop"
PID_FILE = f"{PROBE_DIR}/sampler.pid"
HB_FILE = f"{PROBE_DIR}/heartbeat"
HEARTBEAT_INTERVAL_S = 25
HEARTBEAT_TTL_S = 90
DEFAULT_INTERVAL_S = 60

def _adb(serial: str, cmd: str, timeout_s: int = 30) -> str:
    return str(adb_utils.adb_shell_root(serial, cmd, timeout_s=timeout_s,
                                        check=False))


# ---------------------------------------------------------------- probe 归一化

def norm_probe(p: Dict[str, Any], default_interval: int = DEFAULT_INTERVAL_S
               ) -> Dict[str, Any]:
    """缺省补齐；非法字段抛 ValueError。"""
    kind = str(p.get("kind", ""))
    if kind not in ("file", "files", "dir"):
        raise ValueError(f"bad probe kind {kind!r} (file/files/dir)")
    name = str(p.get("name", ""))
    if not name:
        raise ValueError("probe missing name")
    out = str(p.get("out", f"{name}_samples.csv"))
    if "interval_s" in p:
        interval_s = max(1, int(p["interval_s"] or default_interval))
    else:
        interval_s = max(1, default_interval)
    pr = dict(p)
    pr.update({"name": name, "kind": kind, "out": out,
               "interval_s": interval_s})
    if kind == "file" and not str(p.get("path", "")):
        raise ValueError(f"probe {name}: file kind needs path")
    if kind == "files" and not str(p.get("path_template", "")):
        raise ValueError(f"probe {name}: files kind needs path_template")
    if kind == "dir" and not str(p.get("path", "")):
        raise ValueError(f"probe {name}: dir kind needs path")
    keys = list(p.get("keys") or [])
    pr["keys"] = keys
    pr["cols"] = list(p.get("cols") or keys)
    if len(pr["cols"]) != len(keys):
        raise ValueError(f"probe {name}: cols 长度须与 keys 一致")
    pr["extra_paths"] = list(p.get("extra_paths") or [])
    parse = str(p.get("parse", "kv"))
    if parse not in ("kv", "rows"):
        raise ValueError(f"bad probe parse {parse!r} (kv/rows)")
    if parse == "rows":
        lbl = list(p.get("label_keys") or [])
        if not lbl:
            raise ValueError(f"probe {name}: rows parse needs label_keys")
        pr["label_keys"] = lbl
    pr["parse"] = parse
    return pr


def visible_probes(cycle_cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """返回启用的 probe 列表（已 norm）。interval_s：probe 自带 > 全局 > 60。"""
    global_iv = int(cycle_cfg.get("interval_s", 0) or 0)
    default_iv = global_iv if global_iv > 0 else DEFAULT_INTERVAL_S
    probes = []
    for p in cycle_cfg.get("probes", []) or []:
        if not isinstance(p, dict):
            continue
        if not p.get("enabled", False):
            continue
        probes.append(norm_probe(p, default_iv))
    return probes


def vmstat_probe_keys(sample_cfg: Dict[str, Any]) -> List[str]:
    """从合并后的 sample_config 取 vmstat probe 的 keys（未启用/缺省为空）。"""
    for p in (sample_cfg.get("cycle_sample") or {}).get("probes") or []:
        if isinstance(p, dict) and p.get("name") == "vmstat":
            if not p.get("enabled", False):
                return []
            return list(p.get("keys") or [])
    return []


# ---------------------------------------------------------------- 设备脚本生成

def _cat(path: str) -> str:
    return f"cat {path} 2>/dev/null"


def _probe_read_block(p: Dict[str, Any]) -> str:
    """单个 probe 的一次采样 shell 片段（echo TS 由外层处理）。

    dir/files 每 key 输出一行 "key value"（设备端不解析，host kv 引擎直接吃）。
    """
    kind, keys = p["kind"], p["keys"]
    lines: List[str] = []
    if kind == "file":
        lines.append(_cat(str(p["path"])))
    elif kind == "dir":
        for k in keys:
            lines.append(f'echo "{k} $({_cat(str(p["path"]) + "/" + k)})"')
    else:  # files
        for k in keys:
            lines.append(f'echo "{k} $({_cat(str(p["path_template"]).format(key=k))})"')
    for x in p.get("extra_paths", []):
        lines.append(_cat(str(x)))
    return "\n    ".join(lines)


def build_probe_script(probes: List[Dict[str, Any]]) -> str:
    """生成设备端统一采样循环脚本（mksh）；每秒 tick，per-probe 到点采样。"""
    blocks: List[str] = []
    for p in probes:
        read_block = _probe_read_block(p)
        blocks.append(
            f'  if [ $(($ticks % {p["interval_s"]})) -eq 0 ]; then\n'
            f'    {{ echo "TS $ts";\n    {read_block}\n'
            f'    }} >> {PROBE_DIR}/raw/{p["name"]}.log\n'
            f'  fi')
    intervals = "\n\n".join(blocks)
    return f"""#!/system/bin/sh
# generated unified probe sampler (cycle_sample)
DIR={PROBE_DIR}
STOP=$DIR/stop
HB=$DIR/heartbeat
PID=$DIR/sampler.pid
mkdir -p $DIR/raw
rm -f "$STOP" "$PID"
rm -f $DIR/raw/*.log
echo $$ > "$PID"
touch "$HB"
ticks=0
while [ ! -e "$STOP" ]; do
  ts=$(date +%s)

{intervals}

  ticks=$((ticks + 1))
  hb_age=$(( $(date +%s) - $(stat -c %Y "$HB" 2>/dev/null || echo 0) ))
  [ "$hb_age" -gt {HEARTBEAT_TTL_S} ] && break
  sleep 1
done
exit 0
"""


# ---------------------------------------------------------------- 启动/停止

def _start_device_sampler(serial: str, script_text: str) -> None:
    _adb(serial, f"mkdir -p {PROBE_DIR}/raw; chmod 777 {PROBE_DIR}")
    local = Path("/tmp/opencode/cycle_probe_sampler.sh")
    local.write_text(script_text, encoding="utf-8")
    cp = subprocess.run(["adb", "-s", serial, "push", str(local), PROBE_SCRIPT],
                        capture_output=True, text=True, timeout=60)
    if cp.returncode != 0:
        raise RuntimeError(f"probe sampler push failed: {cp.stderr}")
    _adb(serial, f"chmod 755 {PROBE_SCRIPT}")
    _adb(serial, f"(setsid sh {PROBE_SCRIPT} </dev/null >/dev/null 2>&1 &)")


def start_cycle_probes(serial: str, out_dir: Path, cycle_cfg: Dict[str, Any],
                       stop_event) -> List[threading.Thread]:
    """部署并启动设备端统一采样；返回心跳线程列表（随 stop_event 停止）。

    返回空列表 = 未启用任何 probe（无采样）。
    """
    probes = visible_probes(cycle_cfg)
    if not probes:
        return []
    script = build_probe_script(probes)
    _start_device_sampler(serial, script)
    script_local = out_dir / "cycle_probe_sampler.sh"
    script_local.write_text(script, encoding="utf-8")

    heartbeat_stop = threading.Event()
    try:
        stop_event.add(heartbeat_stop)
    except AttributeError:
        pass

    def _hb():
        while not heartbeat_stop.is_set():
            _adb(serial, f"touch {HB_FILE}", timeout_s=15)
            heartbeat_stop.wait(HEARTBEAT_INTERVAL_S)

    hb = threading.Thread(target=_hb, name=f"cycle_hb_{serial}", daemon=True)
    hb.start()
    return [hb]


def _wait_stopped(serial: str, timeout_s: float = 20) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        pid = _adb(serial, f"cat {PID_FILE} 2>/dev/null").strip()
        if not pid or not pid.isdigit():
            return
        alive = _adb(serial, f"kill -0 {pid} 2>/dev/null && echo yes").strip()
        if alive != "yes":
            return
        time.sleep(1)


def stop_cycle_probes(serial: str, out_dir: Path,
                      cycle_cfg: Dict[str, Any]) -> Dict[str, int]:
    """停设备端采样，pull raw 日志并解析成 <name>_samples.csv。

    返回 {probe_name: 样本行数}；未启用的 probe 不在结果里。
    """
    probes = visible_probes(cycle_cfg)
    result: Dict[str, int] = {}
    if not probes:
        return result
    _adb(serial, f"touch {STOP_FILE}")
    _wait_stopped(serial)
    for p in probes:
        raw_text = _adb(serial, f"cat {PROBE_DIR}/raw/{p['name']}.log 2>/dev/null",
                        timeout_s=30)
        n = _parse_raw_to_csv(raw_text, p, out_dir / p["out"])
        result[p["name"]] = n
    _adb(serial, f"rm -f {STOP_FILE} {PID_FILE}")
    return result


# ---------------------------------------------------------------- host 解析

_TS_RE = re.compile(r"^TS (\d+)\s*$")


def _split_blocks(raw_text: str) -> List[Tuple[int, List[str]]]:
    blocks: List[Tuple[int, List[str]]] = []
    cur_ts: Optional[int] = None
    cur: List[str] = []
    for line in raw_text.splitlines():
        m = _TS_RE.match(line.strip())
        if m:
            if cur_ts is not None:
                blocks.append((cur_ts, cur))
            cur_ts, cur = int(m.group(1)), []
            continue
        cur.append(line)
    if cur_ts is not None:
        blocks.append((cur_ts, cur))
    return blocks


def _kv_of(lines: Sequence[str]) -> Dict[str, str]:
    """通用 kv 提取：'key value' 或 'key=value' 行。"""
    out: Dict[str, str] = {}
    for line in lines:
        s = line.strip()
        if not s:
            continue
        if "=" in s:
            k, _, v = s.partition("=")
        else:
            parts = s.split(None, 1)
            if len(parts) < 2:
                continue
            k, v = parts
        out[k.strip()] = v.strip()
    return out


def _to_int(v: str) -> Optional[int]:
    return int(v) if str(v).isdigit() else None


def _rows_of(lines: Sequence[str], label_keys: Sequence[str]) -> Dict[str, Any]:
    """通用行表引擎：行 = 若干 (key, value) 标签对 + 尾部整数序列。

    列名 = 各 label_keys 对应值的 "_" 拼接 + "_o{序号}"；同一文件的多个
    标签行（不同 zone/type）合并成多列。标签缺失或无数值列的行被跳过。
    """
    cols: Dict[str, Any] = {}
    int_re = re.compile(r"^-?\d+$")
    for line in lines:
        toks = line.split()
        if not toks:
            continue
        i = len(toks)
        while i > 0 and int_re.match(toks[i - 1]):
            i -= 1
        ints = [int(x) for x in toks[i:]]
        if not ints:
            continue
        head = toks[:i]
        pairs = {head[j].rstrip(","): head[j + 1].rstrip(",")
                 for j in range(0, len(head) - 1, 2)}
        lbl = [pairs.get(k) for k in label_keys]
        if not all(lbl):
            continue
        prefix = "_".join(str(x) for x in lbl)
        for n, v in enumerate(ints):
            cols[f"{prefix}_o{n}"] = v
    return cols


def _row_values(p: Dict[str, Any], lines: List[str]) -> Dict[str, Any]:
    """按 parse 类型把一次采样块解析成行；两个通用引擎：kv / table。"""
    parse = str(p.get("parse", "kv"))
    cols: Dict[str, Any] = {}

    if parse == "kv":
        kv = _kv_of(lines)
        keys = p["keys"] or list(kv.keys())
        for i, k in enumerate(keys):
            col = p["cols"][i] if p["cols"] and i < len(p["cols"]) else k
            cols[col] = _to_int(kv.get(k, ""))

    else:  # rows: 标签对… + 尾部整数序列（buddyinfo/pagetypeinfo 同引擎）
        cols.update(_rows_of(lines, p.get("label_keys") or []))

    return cols


def _parse_raw_to_csv(raw_text: str, p: Dict[str, Any], out_csv: Path) -> int:
    """raw log → <name>_samples.csv；返回样本行数。"""
    blocks = _split_blocks(raw_text)
    if not blocks:
        try:
            out_csv.unlink(missing_ok=True)
        except Exception:
            pass
        return 0

    first = _row_values(p, blocks[0][1])
    keys = list(first.keys())
    header = ["host_ts"] + keys

    rows: List[Dict[str, Any]] = []
    for ts, lines in blocks:
        vals = _row_values(p, lines)
        row: Dict[str, Any] = {"host_ts": ts}
        for k in keys:
            row[k] = vals.get(k, "")
        rows.append(row)

    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with out_csv.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=header)
        w.writeheader()
        w.writerows(rows)
    return len(rows)