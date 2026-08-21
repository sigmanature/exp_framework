
# 后台监控与测试脚本运行规范

## 1. 长期运行脚本必须使用 `setsid`

对于长期运行的监控脚本、测试脚本、采集脚本
统一使用：

```bash
setsid <command> >> <log_file>
```

例如：

```bash
setsid bash monitor_net.sh >> logs/monitor_net.log
setsid python3 monitor_temp.py >> logs/monitor_temp.log
```

长期脚本的目标不是“临时后台运行”，而是要尽量脱离当前终端会话，避免 SSH 断开、终端关闭后进程被意外影响。

---

## 2. 启动时必须记录进程信息

所有长期监控脚本启动后，必须记录以下信息：

```text
脚本类型
进程名
PID
启动命令
日志路径
启动时间
```

建议统一写入：

```bash
runs/<run_id>/pids.tsv
```

格式示例：

```text
type    name              pid       log                         cmd                         started_at
monitor monitor_net       12345     logs/monitor_net.log        bash monitor_net.sh         2026-06-18 14:30:00
monitor monitor_temp      12346     logs/monitor_temp.log       python3 monitor_temp.py     2026-06-18 14:30:03
test    stress_cpu        12350     logs/stress_cpu.log         bash stress_cpu.sh          2026-06-18 14:31:10
```

注意：不能只记一个模糊进程名，例如 `python`、`bash`、`java`。  
必须使用唯一业务名，例如：

```text
monitor_net
monitor_temp
stress_cpu
trace_collector
```

清理时优先根据 PID 删除，再根据记录的进程名和命令行签名做补充确认。

---

## 3. 日志不得默认重定向到空

禁止对监控脚本使用：

```bash
> /dev/null 2>&1
```

禁止写法：

```bash
setsid bash monitor.sh > /dev/null 2>&1 &
```

原因是：校验类日志、开关类日志、启动失败原因会被吞掉，出问题时无法快速判断。

标准写法：

```bash
setsid bash monitor.sh >> logs/monitor.log 2>&1 &
```

除非脚本一启动就会产生巨量日志，否则必须保留日志。

---

## 4. 启动后只做轻量日志校验

对于普通监控脚本，启动后不要大量打印日志内容，只检查日志是否正常打开。

推荐检查：

```bash
wc -l logs/monitor.log
```

如果日志行数为 0，需要提示：

```text
[WARN] monitor.log 当前为空，请检查脚本是否正常启动，或脚本是否没有启动日志。
```

对于有明确开关类日志的脚本，必须校验关键字。

例如脚本启动时应输出：

```text
MONITOR_ENABLED=1
CONFIG_OK=1
START_SUCCESS=1
```

校验方式：

```bash
rg "MONITOR_ENABLED=1|CONFIG_OK=1|START_SUCCESS=1" logs/monitor.log
```

如果关键字不存在，需要快速提示用户失败原因，并输出最近几十行日志：

```bash
tail -n 50 logs/monitor.log
```

---

## 5. 大型日志必须按 100MB 分片

对于巨量 trace、链路抓取日志、压测日志、长时间调试日志，不允许无限写入单个大文件。

单个日志文件超过 100MB 后必须分片。

推荐命名：

```text
trace_net_000.log
trace_net_001.log
trace_net_002.log
```

或者：

```text
trace_net.log.000
trace_net.log.001
trace_net.log.002
```

大型日志目录示例：

```text
runs/20260618_143000/
├── logs/
│   ├── monitor_net.log
│   └── monitor_temp.log
├── trace/
│   ├── trace_net_000.log
│   ├── trace_net_001.log
│   └── trace_net_002.log
└── pids.tsv
```

---

## 6. 大型日志检索统一使用 `rg`

巨量日志禁止使用低效的人工打开方式。

推荐使用 `rg` 并行检索：

```bash
rg -n -S -j 0 "ERROR|FAIL|timeout|exception" runs/20260618_143000/logs runs/20260618_143000/trace
```

常用命令：

```bash
rg -n -S -j 0 "关键字" logs/
rg -n -S -j 0 "ERROR|WARN|FAIL" logs/ trace/
rg -n -S -j 0 "case_id=123" runs/
rg -n -S -j 0 --glob "*.log" "timeout" runs/
```

说明：

```text
-n      显示行号
-S      智能大小写
-j 0    自动使用合适的并行线程数
--glob  限制文件类型
```

---

## 7. 必须提供统一 cleanup 脚本

每次启动监控 / 测试脚本时，都必须生成或维护一个统一的清理脚本：

```bash
cleanup.sh
```

`cleanup.sh` 的作用：

```text
1. 读取 pids.tsv
2. 根据 PID 批量终止监控 / 测试脚本
3. 等待进程退出
4. 对未退出进程执行强制删除
5. 再次确认是否仍有残留进程
6. 打印清理结果
```

不允许用户手动一个个 `kill`。

不推荐危险写法：

```bash
pkill -f python
pkill -f bash
killall python3
```

推荐只删除本轮启动时记录过的进程。

---

## 8. cleanup 示例

```bash
#!/usr/bin/env bash
set -Eeuo pipefail

PID_FILE="${1:-pids.tsv}"

if [[ ! -f "$PID_FILE" ]]; then
  echo "[ERROR] PID file not found: $PID_FILE"
  exit 1
fi

echo "[INFO] cleanup by pid file: $PID_FILE"

tail -n +2 "$PID_FILE" | while IFS=$'\t' read -r type name pid log cmd started_at; do
  [[ -z "${pid:-}" ]] && continue

  if kill -0 "$pid" 2>/dev/null; then
    echo "[TERM] type=$type name=$name pid=$pid cmd=$cmd"
    kill "$pid" 2>/dev/null || true
  else
    echo "[SKIP] already stopped: name=$name pid=$pid"
  fi
done

sleep 2

echo "[INFO] checking remaining processes..."

tail -n +2 "$PID_FILE" | while IFS=$'\t' read -r type name pid log cmd started_at; do
  [[ -z "${pid:-}" ]] && continue

  if kill -0 "$pid" 2>/dev/null; then
    echo "[KILL] force kill: type=$type name=$name pid=$pid"
    kill -9 "$pid" 2>/dev/null || true
  fi
done

sleep 1

echo "[INFO] final confirmation:"

has_alive=0

tail -n +2 "$PID_FILE" | while IFS=$'\t' read -r type name pid log cmd started_at; do
  [[ -z "${pid:-}" ]] && continue

  if kill -0 "$pid" 2>/dev/null; then
    echo "[ALIVE] name=$name pid=$pid"
    has_alive=1
  else
    echo "[OK] stopped: name=$name pid=$pid"
  fi
done

echo "[INFO] cleanup finished."
```

---

## 9. `adb reboot` 场景下的脚本存活策略

`adb reboot` 会导致 adb server 状态变化，即使使用 `setsid`，shell 仍可能收到 SIGHUP 被杀。

**标准防御写法**（脚本顶部）：

```bash
#!/usr/bin/env bash
# 必须：忽略 HANGUP，防止 adb reboot 或终端关闭导致脚本被杀
trap '' SIGHUP
# 建议也忽略 PIPE
trap '' SIGPIPE
```

**原因**:

```
adb reboot
  → 设备 USB 断开
  → adb server 内部状态变化
  → 可能向持有 adb 连接的 shell 发送 SIGHUP
  → setsid 只改 sid/pgid，不屏蔽 SIGHUP
  → 脚本被杀
```

单独 `setsid` 不够。必须 `setsid + trap '' SIGHUP` 组合。

---

## 10. `pkill -f` 禁止使用——必定自伤

```bash
# 危险：pkill -f 会匹配到自己的命令行
pkill -f "run_memstress"   # ← 当前 bash 进程包含此字符串，自伤
pkill -f "runner"          # ← 同上
```

**安全替代 A**：基于 PID 文件 kill（见规范第 8 节 cleanup）

**安全替代 B**：`pgrep` + 排除自身 PID

```bash
pgrep -f "run_memstress" | grep -v "$$" | xargs -r kill 2>/dev/null
```

**安全替代 C**：用 `pgrep` 先列出，确认后再 `kill`

```bash
pids=$(pgrep -f "run_memstress" | grep -v "$$" | tr '\n' ' ')
[ -n "$pids" ] && kill $pids 2>/dev/null
```

---

## 11. 简化一次性强制清理

编排脚本需要在启动前清理残留进程，不需要完整 cleanup 流程时：

```bash
kill_previous() {
    local pattern="$1"
    local pids
    pids=$(pgrep -f "$pattern" 2>/dev/null | grep -v "$$" | tr '\n' ' ')
    [ -z "$pids" ] && return 0
    kill $pids 2>/dev/null || true
    sleep 1
    pids=$(pgrep -f "$pattern" 2>/dev/null | grep -v "$$" | tr '\n' ' ')
    [ -n "$pids" ] && kill -9 $pids 2>/dev/null || true
}

kill_previous "run_memstress_and_collect_logs.py"
```

**注意**：`grep -v "$$"` 排除当前 shell 自身，防止 kill 自己导致命令阻塞。

---

## 12. 推荐启动封装函数

建议在项目中提供统一启动函数，例如：

```bash
start_job() {
  local type="$1"
  local name="$2"
  local log="$3"
  shift 3

  mkdir -p "$(dirname "$log")"

  echo "[$(date '+%F %T')] START type=$type name=$name cmd=$*" >> "$log"

  setsid "$@" >> "$log" 2>&1 &
  local pid=$!

  printf "%s\t%s\t%s\t%s\t%s\t%s\n" \
    "$type" "$name" "$pid" "$log" "$*" "$(date '+%F %T')" >> "$PID_FILE"

  echo "[STARTED] type=$type name=$name pid=$pid log=$log"
}
```

使用方式：

```bash
PID_FILE="runs/20260618_143000/pids.tsv"

mkdir -p runs/20260618_143000/logs
printf "type\tname\tpid\tlog\tcmd\tstarted_at\n" > "$PID_FILE"

start_job monitor monitor_net  runs/20260618_143000/logs/monitor_net.log  bash monitor_net.sh
start_job monitor monitor_temp runs/20260618_143000/logs/monitor_temp.log python3 monitor_temp.py
start_job test    stress_cpu   runs/20260618_143000/logs/stress_cpu.log   bash stress_cpu.sh
```

---

## 10. 启动后日志检查函数

```bash
check_log_opened() {
  local name="$1"
  local log="$2"
  local min_lines="${3:-1}"
  local required_pattern="${4:-}"

  if [[ ! -f "$log" ]]; then
    echo "[FAIL] $name log not found: $log"
    return 1
  fi

  local lines
  lines=$(wc -l < "$log" || echo 0)

  echo "[CHECK] $name log lines=$lines log=$log"

  if (( lines < min_lines )); then
    echo "[WARN] $name log line count too small: $lines < $min_lines"
    echo "[WARN] recent log:"
    tail -n 50 "$log" || true
    return 1
  fi

  if [[ -n "$required_pattern" ]]; then
    if ! rg -n "$required_pattern" "$log" >/dev/null; then
      echo "[FAIL] $name missing required startup marker: $required_pattern"
      echo "[FAIL] recent log:"
      tail -n 50 "$log" || true
      return 1
    fi
  fi

  echo "[OK] $name log check passed"
}
```

示例：

```bash
check_log_opened "monitor_net" "runs/20260618_143000/logs/monitor_net.log" 1 "MONITOR_ENABLED=1|START_SUCCESS=1"
```

---