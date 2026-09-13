"""运行队列页：GUI 待跑作业 + 设备锁游标（run_cursor.json）双看板。

- GUI 作业：本程序入队的提交（pending/started/finished/failed），由主窗刷新
- 设备锁游标：exp_lock 的权威状态（running/queued/done/failed/cleanup_failed），
  每 2s 轮询 ~/.worklog/run_cursor.json；内容未变化时跳过重建（不闪选区）
- 心跳停滞 >120s 标红（进程可能僵死）
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (QHBoxLayout, QHeaderView, QLabel, QPushButton,
                               QTreeWidget, QTreeWidgetItem, QVBoxLayout,
                               QWidget)

from exp_framework.utils.exp_lock import CURSOR_FILE, exp_lock_list

_HEARTBEAT_STALE_S = 120

_STATE_TEXT = {"running": "运行中", "done": "完成", "failed": "失败",
               "cleanup_failed": "清理失败(锁占用)"}
_JOB_TEXT = {"pending": "待跑(排队中)", "started": "已启动",
             "finished": "完成", "failed": "失败"}
_RED = QBrush(QColor(190, 60, 40))
_GRAY = QBrush(QColor(120, 120, 120))
_GREEN = QBrush(QColor(40, 130, 60))


def _parse_ts(ts: str) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(ts)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


class QueueTab(QWidget):
    """队列双看板。selection 角色携带 {kind, ident, pid, run_dir}。"""

    stop_requested = Signal(str, str)   # kind("job"|"cursor"), ident
    open_path_requested = Signal(str)   # run_dir / 文件
    audit_requested = Signal(str)       # run_dir

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)

        lay.addWidget(QLabel("本 GUI 作业队列"))
        self.jobs_tree = QTreeWidget()
        self.jobs_tree.setHeaderLabels(
            ["提交 ID", "实验名", "域", "设备", "状态", "更新时间", "说明"])
        self.jobs_tree.setRootIsDecorated(False)
        self.jobs_tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        lay.addWidget(self.jobs_tree, 2)

        lay.addWidget(QLabel(f"设备锁游标 · {CURSOR_FILE}（权威，含 CLI 排队）"))
        self.cursor_tree = QTreeWidget()
        self.cursor_tree.setHeaderLabels(
            ["域 : 设备", "状态", "exp_id", "会话 / 工具", "心跳", "实验目录"])
        self.cursor_tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        lay.addWidget(self.cursor_tree, 3)

        btns = QHBoxLayout()
        self.btn_stop = QPushButton("停止所选")
        self.btn_open = QPushButton("打开实验目录")
        self.btn_audit = QPushButton("查看审计")
        for b in (self.btn_stop, self.btn_open, self.btn_audit):
            btns.addWidget(b)
        btns.addStretch(1)
        lay.addLayout(btns)
        self.btn_stop.clicked.connect(self._emit_stop)
        self.btn_open.clicked.connect(
            lambda: self._emit_path("open_path_requested"))
        self.btn_audit.clicked.connect(
            lambda: self._emit_path("audit_requested"))

        self._last_cursor_blob = ""
        self._jobs: List[Dict[str, Any]] = []

    # ---------------- GUI 作业 ----------------

    def refresh_jobs(self, jobs: List[Dict[str, Any]]) -> None:
        self._jobs = jobs
        tree = self.jobs_tree
        sel = self._sel_id(tree)
        tree.clear()
        for j in jobs:
            it = QTreeWidgetItem([
                j.get("submission_id", ""), j.get("exp_name", ""),
                j.get("domain", ""), j.get("serial", ""),
                _JOB_TEXT.get(j.get("state", ""), j.get("state", "")),
                j.get("updated_at", ""), j.get("note", "")])
            it.setData(0, Qt.UserRole, {"kind": "job",
                                        "ident": j.get("submission_id", ""),
                                        "pid": j.get("pid"),
                                        "run_dir": j.get("run_dir")})
            state = j.get("state")
            if state == "started":
                it.setForeground(4, _GREEN)
            elif state == "failed":
                it.setForeground(4, _RED)
            else:
                it.setForeground(4, _GRAY)
            tree.addTopLevelItem(it)
        self._restore_sel(tree, sel)

    # ---------------- 游标 ----------------

    def refresh_cursor(self) -> None:
        try:
            data = exp_lock_list()
            err = ""
        except Exception as exc:
            data = {"entries": {}}
            err = f"游标读取失败: {exc}"
        now = datetime.now(timezone.utc)
        # 哈希里带"时间分桶"：JSON 不变时心跳陈旧度随时间推进仍能触发重建
        freshness = []
        for key, e in sorted((data.get("entries") or {}).items()):
            hb = _parse_ts(e.get("heartbeat_ts", ""))
            age = (now - hb).total_seconds() if hb else -1.0
            freshness.append(f"{key}:{int(max(age, 0) // 30)}")
        blob = json.dumps(data, sort_keys=True, ensure_ascii=False) + "|" \
            + "|".join(freshness) + f"|err={err}"
        if blob == self._last_cursor_blob:
            return
        self._last_cursor_blob = blob
        tree = self.cursor_tree
        sel = self._sel_id(tree)
        tree.clear()
        if err:
            it = QTreeWidgetItem(["(错误)", err, "", "", "", ""])
            it.setForeground(1, _RED)
            tree.addTopLevelItem(it)
        for key, e in sorted((data.get("entries") or {}).items()):
            state = e.get("state", "?")
            hb = _parse_ts(e.get("heartbeat_ts", ""))
            age = int(max((now - hb).total_seconds(), 0)) if hb else -1
            stale = hb is not None and age > _HEARTBEAT_STALE_S
            if hb is None:
                hb_text = "-"
            elif stale and state == "running":
                hb_text = f"⚠ {age}s 未更新"  # 仅活跃任务的心跳停滞才算异常
            else:
                hb_text = f"{age}s 前"
            it = QTreeWidgetItem([
                key, _STATE_TEXT.get(state, state), e.get("exp_id", ""),
                f"{e.get('session_id') or 'manual'} / {e.get('agent_tool') or '-'}",
                hb_text, e.get("run_dir", "")])
            it.setData(0, Qt.UserRole, {"kind": "cursor", "ident": key,
                                        "pid": e.get("pid"),
                                        "run_dir": e.get("run_dir")})
            if state == "running":
                it.setForeground(1, _GREEN)
            elif state == "cleanup_failed":
                it.setForeground(1, _RED)
            if stale and state == "running":
                it.setForeground(4, _RED)
            queue = e.get("queue") or []
            for i, q in enumerate(queue):
                child = QTreeWidgetItem([
                    f"└ 队列 #{i + 1}", "排队中", q.get("exp_id", ""),
                    f"{q.get('session_id') or 'manual'} / {q.get('agent_tool') or '-'}",
                    f"入队 {q.get('enqueued_at', '-')}", q.get("run_dir", "")])
                it.addChild(child)
            tree.addTopLevelItem(it)
        self._restore_sel(tree, sel)

    # ---------------- 选中与按钮 ----------------

    def _sel_id(self, tree: QTreeWidget) -> str:
        it = tree.currentItem()
        if it is None:
            return ""
        info = it.data(0, Qt.UserRole) or {}
        return f"{info.get('kind', '')}:{info.get('ident', '')}"

    def _restore_sel(self, tree: QTreeWidget, sel_id: str) -> None:
        if not sel_id:
            return
        for i in range(tree.topLevelItemCount()):
            it = tree.topLevelItem(i)
            info = it.data(0, Qt.UserRole) or {}
            if f"{info.get('kind', '')}:{info.get('ident', '')}" == sel_id:
                tree.setCurrentItem(it)
                return

    def _current_info(self) -> Optional[Dict[str, Any]]:
        for tree in (self.jobs_tree, self.cursor_tree):
            it = tree.currentItem()
            while it is not None:  # 队列子项回溯到所属设备条目
                info = it.data(0, Qt.UserRole)
                if info:
                    return info
                it = it.parent()
        return None

    def _emit_stop(self) -> None:
        info = self._current_info()
        if info:
            self.stop_requested.emit(info.get("kind", ""), info.get("ident", ""))

    def _emit_path(self, sig_name: str) -> None:
        info = self._current_info()
        run_dir = (info or {}).get("run_dir") or ""
        if run_dir:
            getattr(self, sig_name).emit(run_dir)
