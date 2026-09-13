"""运行审计页：按实验目录展示 run_manifest / exit_code / stop_reason / 文件清单。

数据全部来自产物文件（只读），不做任何推断：
- run_manifest.json：状态/样本数/起止/stop_reason + 运行锁 + 配置摘要
- state/exit_code、state/stop_reason.json：干净收尾语义的权威
- state/restart_ledger.tsv：重启台账（如有）
- 文件清单：run_dir 两层深度内的文件（大小），点选预览前 100 行
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (QComboBox, QHBoxLayout, QHeaderView, QLabel,
                               QPlainTextEdit, QPushButton, QSplitter,
                               QTreeWidget, QTreeWidgetItem, QVBoxLayout,
                               QWidget)

_PREVIEW_MAX_BYTES = 200 * 1024
_PREVIEW_LINES = 100
_GRAY = QBrush(QColor(120, 120, 120))


def _fmt_ts(ts: Any) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(ts)))
    except (TypeError, ValueError):
        return "-"


def _fmt_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024.0
    return f"{n:.1f}GB"


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


class AuditTab(QWidget):
    """审计页。open_editor_requested -> 主窗用编辑器打开所选文件。"""

    open_editor_requested = Signal(str)
    open_path_requested = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)

        top = QHBoxLayout()
        top.addWidget(QLabel("实验目录:"))
        self.dir_combo = QComboBox()
        self.dir_combo.setEditable(True)
        top.addWidget(self.dir_combo, 1)
        self.btn_load = QPushButton("载入")
        self.btn_open_dir = QPushButton("打开目录")
        self.btn_editor = QPushButton("用编辑器打开所选文件")
        top.addWidget(self.btn_load)
        top.addWidget(self.btn_open_dir)
        top.addWidget(self.btn_editor)
        lay.addLayout(top)

        split = QSplitter(Qt.Horizontal)
        self.info_tree = QTreeWidget()
        self.info_tree.setHeaderLabels(["项目", "内容"])
        self.info_tree.header().setSectionResizeMode(1, QHeaderView.Stretch)
        split.addWidget(self.info_tree)
        right = QWidget()
        rlay = QVBoxLayout(right)
        rlay.setContentsMargins(0, 0, 0, 0)
        self.preview_label = QLabel("文件预览（左侧点选文件）")
        rlay.addWidget(self.preview_label)
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        rlay.addWidget(self.preview, 1)
        split.addWidget(right)
        split.setSizes([460, 560])
        lay.addWidget(split, 1)

        self.btn_load.clicked.connect(lambda: self.load_run(
            self.dir_combo.currentText().strip()))
        self.btn_open_dir.clicked.connect(lambda: self.open_path_requested.emit(
            self.dir_combo.currentText().strip()))
        self.btn_editor.clicked.connect(self._emit_editor)
        self.info_tree.currentItemChanged.connect(self._on_selection)

        self._file_items: List[QTreeWidgetItem] = []
        self._current_dir = ""

    # ---------------- 载入 ----------------

    def update_candidates(self, dirs: List[str]) -> None:
        """刷新目录候选（保留当前文本）。"""
        cur = self.dir_combo.currentText()
        seen: List[str] = []
        for d in [cur] + list(dirs):
            if d and d not in seen:
                seen.append(d)
        self.dir_combo.blockSignals(True)
        self.dir_combo.clear()
        self.dir_combo.addItems(seen)
        self.dir_combo.setCurrentText(cur)
        self.dir_combo.blockSignals(False)

    def load_run(self, run_dir: str) -> None:
        self.info_tree.clear()
        self._file_items = []
        self.preview.clear()
        self.preview_label.setText("文件预览（左侧点选文件）")
        run_dir = str(run_dir or "").strip()
        if not run_dir:
            self._add_group("提示", [("说明", "先在上方填/选实验目录再载入")])
            return
        root = Path(run_dir)
        if not root.is_dir():
            self._add_group("错误", [("目录不存在", run_dir)])
            return
        self._current_dir = run_dir
        manifest = _read_json(root / "run_manifest.json") or {}
        state_dir = root / "state"
        exit_code = None
        try:
            exit_code = (state_dir / "exit_code").read_text(
                encoding="utf-8").strip()
        except OSError:
            pass
        stop_reason = _read_json(state_dir / "stop_reason.json") or {}

        summary = [("run_dir", run_dir)]
        if manifest:
            summary += [
                ("status", manifest.get("status", "-")),
                ("exit_code", exit_code if exit_code is not None else "缺失"),
                ("样本数", manifest.get("samples", "-")),
                ("采样错误", manifest.get("sample_errors", "-")),
                ("开始", _fmt_ts(manifest.get("start_host_ts"))),
                ("结束", _fmt_ts(manifest.get("end_host_ts"))),
            ]
            if manifest.get("stop_reason"):
                summary.append(("stop_reason", json.dumps(
                    manifest["stop_reason"], ensure_ascii=False)))
        elif exit_code is not None:
            summary.append(("exit_code", exit_code))
        self._add_group("摘要", summary)

        lock = manifest.get("exp_lock") or {}
        if lock:
            self._add_group("运行锁", [
                ("exp_id", lock.get("exp_id", "-")),
                ("claim", lock.get("claim", "-")),
                ("session", lock.get("session_id", "-")),
                ("agent_tool", lock.get("agent_tool", "-")),
                ("domain", lock.get("domain", "-")),
            ])

        if stop_reason:
            self._add_group("stop_reason.json", [
                (k, str(v)) for k, v in stop_reason.items()])

        cfg = manifest.get("config") or {}
        if cfg:
            backend = (cfg.get("backend") or {})
            ec = cfg.get("exp_ctx") or {}
            rows = [("backend.name", backend.get("name", "-")),
                    ("exp_ctx", f"{ec.get('exp_name', '-')} @ {ec.get('domain', '-')} "
                                f"serial={ec.get('serial', '-')}"),
                    ("counters 数", len(cfg.get("counters") or [])),
                    ("interval_s", cfg.get("interval_s", "-")),
                    ("sysctl_nodes 数", len(cfg.get("sysctl_nodes") or [])),
                    ("boot_params 数", len(cfg.get("boot_params") or []))]
            sc = manifest.get("sample_config") or {}
            rows.append(("sample_config 域", ", ".join(sorted(sc.keys())) or "-"))
            self._add_group("配置摘要", rows)

        ledger = state_dir / "restart_ledger.tsv"
        if ledger.exists():
            head = [ln for ln in ledger.read_text(
                encoding="utf-8", errors="replace").splitlines() if ln.strip()][:6]
            self._add_group("重启台账(restart_ledger)",
                            [(f"行{i + 1}", ln[:220]) for i, ln in enumerate(head)])

        files = self._list_files(root)
        if files:
            group = self._add_group(f"文件({len(files)})",
                                    [(rel, _fmt_size(size)) for rel, size in files])
            for i, (rel, size) in enumerate(files):
                item = group.child(i)
                item.setData(0, Qt.UserRole, str(root / rel))
                self._file_items.append(item)

    # ---------------- 渲染辅助 ----------------

    def _add_group(self, title: str, rows) -> QTreeWidgetItem:
        group = QTreeWidgetItem([title, ""])
        self.info_tree.addTopLevelItem(group)
        for k, v in rows:
            child = QTreeWidgetItem([str(k), str(v)])
            if str(v) in ("缺失", "-", ""):
                child.setForeground(1, _GRAY)
            group.addChild(child)
        group.setExpanded(True)
        return group

    @staticmethod
    def _list_files(root: Path) -> List[tuple]:
        out: List[tuple] = []
        for p in sorted(root.rglob("*")):
            rel = p.relative_to(root)
            if len(rel.parts) > 2 or not p.is_file():
                continue
            try:
                out.append((str(rel), p.stat().st_size))
            except OSError:
                continue
        return out

    # ---------------- 交互 ----------------

    def _on_selection(self, cur: Optional[QTreeWidgetItem],
                      _prev: Optional[QTreeWidgetItem]) -> None:
        if cur is None:
            return
        path = cur.data(0, Qt.UserRole)
        if not path:
            return
        p = Path(path)
        try:
            with p.open("rb") as f:  # 只读前 N 字节，避免大日志整读卡界面
                raw = f.read(_PREVIEW_MAX_BYTES)
            text = raw.decode("utf-8", errors="replace")
            truncated = len(raw) == _PREVIEW_MAX_BYTES
            if truncated:
                self.preview.setPlainText(
                    f"(文件超过 {_PREVIEW_MAX_BYTES}B，仅预览开头)\n" + text)
            else:
                lines = text.splitlines()
                shown = "\n".join(lines[:_PREVIEW_LINES])
                if len(lines) > _PREVIEW_LINES:
                    shown += f"\n... (共 {len(lines)} 行)"
                self.preview.setPlainText(shown)
            self.preview_label.setText(f"预览: {p.name}")
        except OSError as e:
            self.preview.setPlainText(f"读取失败: {e}")

    def _emit_editor(self) -> None:
        it = self.info_tree.currentItem()
        path = it.data(0, Qt.UserRole) if it is not None else None
        if path:
            self.open_editor_requested.emit(str(path))
