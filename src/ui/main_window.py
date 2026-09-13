"""expf_ui 主窗：模板库 + 配置树工作台 + 运行队列/审计看板。

架构边界（与 runner/exp_lock 的协议）：
- 启动实验只通过子进程调用 `python -m exp_framework.experiment.runner`，
  runner 自己 claim 锁（enqueue_on_busy=False 语义），GUI 不替代 runner
- 排队语义：GUI 维护自己的 FIFO（queue_jobs 目录持久化），监视线程在
  (domain, serial) 空闲（exp_lock_get 无条目或终态 done/failed）时才 spawn；
  被 CLI 抢先（rejected:busy）则回队重试
- 写 run_cursor.json 一律走 exp_lock API；GUI 只读它做看板
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from PySide6.QtCore import (QTimer, Qt, QUrl, Signal)
from PySide6.QtGui import QAction, QDesktopServices
from PySide6.QtWidgets import (QApplication, QComboBox, QDialog,
                               QDialogButtonBox, QFileDialog, QFormLayout,
                               QHBoxLayout, QInputDialog, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QMainWindow,
                               QMenu, QMessageBox, QPlainTextEdit,
                               QPushButton, QSplitter, QTabWidget, QToolBar,
                               QToolButton, QTreeView, QVBoxLayout, QWidget)

from exp_framework.ui.config_model import (ConfigDelegate, ConfigFilterProxy,
                                           ConfigTreeModel,
                                           run_batch_dialog)
from exp_framework.ui.queue_model import QueueTab
from exp_framework.ui.audit import AuditTab
from exp_framework.utils.exp_lock import exp_lock_get

REPO_ROOT = Path(__file__).resolve().parents[3]
SRC_DIR = Path(__file__).resolve().parents[2]
JOB_ROOT = Path.home() / ".worklog" / "expf" / "queue_jobs"

_REG_CACHE: Optional[Dict[str, Any]] = None


def backend_registry() -> Dict[str, Any]:
    """已注册后端表（import 副作用注册；失败返回空表并在 UI 标注）。"""
    global _REG_CACHE
    if _REG_CACHE is None:
        try:
            import exp_framework.backend  # noqa: F401 (注册副作用)
            from exp_framework.experiment.experiment import REGISTRY
            _REG_CACHE = dict(REGISTRY)
        except Exception:
            _REG_CACHE = {}
    return _REG_CACHE


def sanitize_name(s: str, fallback: str = "run") -> str:
    s = re.sub(r"[^A-Za-z0-9_-]", "", str(s))[:48]
    return s or fallback


class SettingsDialog(QDialog):
    """首选项：编辑器命令 / 模板目录 / 默认输出目录（QSettings 持久化）。"""

    def __init__(self, parent, settings):
        super().__init__(parent)
        self.setWindowTitle("expf_ui 首选项")
        self.settings = settings
        form = QFormLayout(self)
        self.editor_edit = QLineEdit(settings.value("editor_cmd", "code"))
        form.addRow("编辑器命令(可带参数)", self.editor_edit)
        self.tpl_edit = QLineEdit(settings.value(
            "template_dir", str(REPO_ROOT / "config")))
        form.addRow("模板目录", self.tpl_edit)
        self.out_edit = QLineEdit(settings.value(
            "out_base", str(Path.home() / "expf_out")))
        browse = QPushButton("...")
        browse.clicked.connect(self._browse)
        row = QHBoxLayout()
        row.addWidget(self.out_edit, 1)
        row.addWidget(browse)
        form.addRow("默认输出基础目录", row)
        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        form.addRow(btns)

    def _browse(self):
        d = QFileDialog.getExistingDirectory(self, "输出基础目录",
                                             self.out_edit.text())
        if d:
            self.out_edit.setText(d)

    def accept(self):
        self.settings.setValue("editor_cmd", self.editor_edit.text().strip())
        self.settings.setValue("template_dir", self.tpl_edit.text().strip())
        self.settings.setValue("out_base", self.out_edit.text().strip())
        super().accept()


class MainWindow(QMainWindow):
    """三栏主窗。"""

    devices_ready = Signal(list)  # adb devices 刷新完成（子线程 -> 主线程）

    def __init__(self):
        super().__init__()
        self.setWindowTitle("expf_ui — exp_framework 本地实验看板")
        self.resize(1600, 950)
        self.settings = self._settings()
        self.model = ConfigTreeModel()
        self.proxy = ConfigFilterProxy(self.model)
        self.jobs: List[Dict[str, Any]] = []
        self.current_template_path: Optional[str] = None
        self._tpl_default: Dict[str, str] = {}
        self._load_jobs()

        self._build_toolbar()
        self._build_body()
        self._load_templates()
        self._load_job_queue_view()

        self.preview_timer = QTimer(self)
        self.preview_timer.setSingleShot(True)
        self.preview_timer.setInterval(200)
        self.preview_timer.timeout.connect(self._refresh_preview)
        self.model.edited.connect(self._on_edited)

        self.filter_timer = QTimer(self)
        self.filter_timer.setSingleShot(True)
        self.filter_timer.setInterval(150)
        self.filter_timer.timeout.connect(self._apply_filter)

        self._tpl_mtime: Optional[float] = None
        self._tpl_dir_sig: Optional[tuple] = None
        self._detach_dialog: Optional[QDialog] = None
        self._center_split_index = 1
        self.monitor = QTimer(self)
        self.monitor.setInterval(2000)
        self.monitor.timeout.connect(self._monitor_tick)
        self.monitor.start()
        self.devices_ready.connect(self._on_devices)
        self._monitor_tick()

    # ---------------- 设置 / 作业持久化 ----------------

    @staticmethod
    def _settings():
        from PySide6.QtCore import QSettings
        return QSettings("expf", "ui")

    def _load_jobs(self) -> None:
        if not JOB_ROOT.is_dir():
            return
        for d in sorted(JOB_ROOT.iterdir()):
            jf = d / "job.json"
            if not jf.is_file():
                continue
            try:
                job = json.loads(jf.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if job.get("state") == "started":
                pid = job.get("pid")
                alive = False
                try:
                    if pid:
                        os.kill(int(pid), 0)
                        alive = True
                except (OSError, ValueError, TypeError):
                    alive = False
                if not alive:
                    job["state"] = "failed"
                    job["note"] = "GUI 重启前进程已退出"
                    self._write_job(job)
            self.jobs.append(job)

    def _write_job(self, job: Dict[str, Any]) -> None:
        d = JOB_ROOT / job["submission_id"]
        d.mkdir(parents=True, exist_ok=True)
        job["updated_at"] = time.strftime("%H:%M:%S")
        (d / "job.json").write_text(
            json.dumps(job, ensure_ascii=False, indent=1) + "\n",
            encoding="utf-8")

    # ---------------- UI 构建 ----------------

    def _build_toolbar(self) -> None:
        tb = QToolBar("main")
        self.addToolBar(tb)

        tb.addWidget(QLabel(" 设备 "))
        self.serial_combo = QComboBox()
        self.serial_combo.setEditable(True)
        self.serial_combo.setMinimumWidth(180)
        tb.addWidget(self.serial_combo)
        refresh_btn = QToolButton()
        refresh_btn.setText("⟳")
        refresh_btn.setToolTip("刷新 adb devices -l")
        refresh_btn.clicked.connect(self._refresh_devices)
        tb.addWidget(refresh_btn)

        tb.addWidget(QLabel(" 实验域 "))
        self.domain_combo = QComboBox()
        self.domain_combo.setEditable(True)
        self.domain_combo.addItems(["pixel", "cuttlefish"])
        self.domain_combo.setMinimumWidth(110)
        tb.addWidget(self.domain_combo)

        tb.addWidget(QLabel(" 实验名 "))
        self.exp_name_edit = QLineEdit()
        self.exp_name_edit.setMaximumWidth(180)
        self.exp_name_edit.setPlaceholderText("exp_id 前缀")
        tb.addWidget(self.exp_name_edit)

        tb.addWidget(QLabel(" 输出 "))
        self.out_edit = QLineEdit(self.settings.value(
            "out_base", str(Path.home() / "expf_out")))
        self.out_edit.setMinimumWidth(200)
        tb.addWidget(self.out_edit)
        browse = QToolButton()
        browse.setText("...")
        browse.clicked.connect(self._browse_out)
        tb.addWidget(browse)

        tb.addSeparator()
        act_editor = QAction("用编辑器打开", self)
        act_editor.triggered.connect(self._open_editor)
        tb.addAction(act_editor)
        act_reload = QAction("⟳ 重新加载", self)
        act_reload.setToolTip("从磁盘重载当前模板（勾选态保留，值以磁盘为准）")
        act_reload.triggered.connect(self._reload_template_from_disk)
        tb.addAction(act_reload)
        act_save = QAction("另存为模板", self)
        act_save.triggered.connect(self._save_as_template)
        tb.addAction(act_save)
        act_settings = QAction("⚙ 设置", self)
        act_settings.triggered.connect(self._open_settings)
        tb.addAction(act_settings)
        tb.addSeparator()
        act_run = QAction("入队运行", self)
        act_run.triggered.connect(self._enqueue)
        tb.addAction(act_run)

        self.serial_combo.currentTextChanged.connect(self._toolbar_changed)
        self.domain_combo.currentTextChanged.connect(self._toolbar_changed)
        self.exp_name_edit.textChanged.connect(self._toolbar_changed)
        self.out_edit.editingFinished.connect(
            lambda: self.settings.setValue("out_base", self.out_edit.text().strip()))

    def _build_body(self) -> None:
        central = QWidget()
        lay = QHBoxLayout(central)
        lay.setContentsMargins(4, 4, 4, 4)
        split = QSplitter(Qt.Horizontal)
        self._split = split  # detach/retach 用
        lay.addWidget(split)

        left = QWidget()
        llay = QVBoxLayout(left)
        llay.setContentsMargins(0, 0, 0, 0)
        llay.addWidget(QLabel("模板库 (config/*.json)"))
        self.tpl_list = QListWidget()
        self.tpl_list.currentRowChanged.connect(self._on_template_selected)
        self.tpl_list.itemClicked.connect(self._on_template_clicked)
        self.tpl_list.setToolTip("双击/单击载入；Ctrl+单击 = 用编辑器打开该模板")
        llay.addWidget(self.tpl_list, 1)
        split.addWidget(left)

        center = QWidget()
        clay = QVBoxLayout(center)
        clay.setContentsMargins(0, 0, 0, 0)
        filter_row = QHBoxLayout()
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText(
            "过滤字段（键名/路径/值，防抖 150ms）")
        self.filter_edit.textChanged.connect(lambda: self.filter_timer.start())
        filter_row.addWidget(self.filter_edit, 1)
        self.field_count_label = QLabel("字段: -")
        filter_row.addWidget(self.field_count_label)
        self._detach_btn = QToolButton()
        self._detach_btn.setText("⧉ 独立编辑区")
        self._detach_btn.setToolTip("把过滤+配置树+JSON预览独立成大窗口，可再返回主窗")
        self._detach_btn.clicked.connect(self._toggle_detach)
        filter_row.addWidget(self._detach_btn)
        clay.addLayout(filter_row)

        csplit = QSplitter(Qt.Vertical)
        self.tree = QTreeView()
        self.tree.setModel(self.proxy)
        self.tree.setItemDelegate(ConfigDelegate(self.tree))
        self.tree.setUniformRowHeights(True)   # 展开滚动性能关键：均匀行高
        self.tree.setAnimated(False)
        self.tree.setSelectionBehavior(QTreeView.SelectRows)
        self.tree.header().resizeSection(0, 46)
        self.tree.header().resizeSection(1, 340)
        self.tree.header().resizeSection(2, 260)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._context_menu)
        csplit.addWidget(self.tree)

        preview_panel = QWidget()
        play = QVBoxLayout(preview_panel)
        play.setContentsMargins(0, 0, 0, 0)
        play.addWidget(QLabel("最终 JSON 预览（提交即此内容）"))
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setMaximumHeight(220)
        play.addWidget(self.preview)
        csplit.addWidget(preview_panel)
        csplit.setSizes([620, 220])
        clay.addWidget(csplit, 1)
        self._center_widget = center  # detach/retach 复用同一实例
        split.addWidget(center)

        self.tabs = QTabWidget()
        self.queue_tab = QueueTab()
        self.audit_tab = AuditTab()
        self.tabs.addTab(self.queue_tab, "运行队列")
        self.tabs.addTab(self.audit_tab, "运行审计")
        split.addWidget(self.tabs)

        split.setSizes([280, 760, 540])
        self.setCentralWidget(central)

        self.queue_tab.stop_requested.connect(self._stop_selected)
        self.queue_tab.open_path_requested.connect(self._open_path)
        self.queue_tab.audit_requested.connect(self._audit_load)
        self.audit_tab.open_editor_requested.connect(self._open_editor_path)
        self.audit_tab.open_path_requested.connect(self._open_path)

    # ---------------- 模板库 ----------------

    def _template_dir(self) -> Path:
        d = self.settings.value("template_dir", str(REPO_ROOT / "config"))
        return Path(str(d))

    def _load_templates(self) -> None:
        """重建模板列表。全程 blockSignals：不触发 currentRowChanged，
        避免后台目录刷新时意外重载模板、丢掉内存中的勾选态。"""
        cur_path = None
        it = self.tpl_list.currentItem()
        if it is not None:
            cur_path = it.data(Qt.UserRole)
        self.tpl_list.blockSignals(True)
        try:
            self.tpl_list.clear()
            tdir = self._template_dir()
            reg = backend_registry()
            for p in sorted(tdir.glob("*.json")):
                data: Dict[str, Any] = {}
                badge, tip = "✗ 空模板", "JSON 顶层应为对象 {..}"
                try:
                    loaded = json.loads(p.read_text(encoding="utf-8-sig"))
                    if isinstance(loaded, dict):
                        data = loaded
                    elif loaded is not None:
                        badge, tip = "✗ 非对象JSON", \
                            f"顶层类型: {type(loaded).__name__}"
                except (OSError, json.JSONDecodeError) as e:
                    badge, tip, data = "✗ 解析失败", str(e), {}
                if data:
                    if "config" not in data:
                        badge = "片段(非独立模板)"
                    else:
                        name = (data.get("config") or {}).get(
                            "backend", {}).get("name", "?")
                        badge = (f"✓{name}" if name in reg
                                 else f"✗后端未注册({name})" if reg
                                 else f"?{name}")
                    tip = str(data.get("_template", ""))[:300]
                    n_leaf = json.dumps(data).count(":")
                    tip = f"约{n_leaf} 个键\n{tip}"
                item = QListWidgetItem(f"{p.name}   · {badge}")
                item.setToolTip(tip)
                item.setData(Qt.UserRole, str(p))
                self.tpl_list.addItem(item)
            if cur_path:  # 原选中项仍存在则恢复高亮（不触发载入）
                for i in range(self.tpl_list.count()):
                    if self.tpl_list.item(i).data(Qt.UserRole) == cur_path:
                        self.tpl_list.setCurrentRow(i)
                        break
        finally:
            self.tpl_list.blockSignals(False)
        self._tpl_dir_sig = self._template_dir_sig()

    def _template_dir_sig(self) -> tuple:
        """模板目录轻量指纹：(文件名, mtime)，逐文件容错（半写/被删不炸）。"""
        sig = []
        try:
            for p in self._template_dir().glob("*.json"):
                try:
                    sig.append((p.name, p.stat().st_mtime))
                except OSError:
                    sig.append((p.name, -1.0))
        except OSError:
            pass
        return tuple(sorted(sig))

    def _poll_template_dir(self) -> None:
        """后台（agent/模型）往模板目录新增/修改配置时，自动刷新列表。"""
        sig = self._template_dir_sig()
        if sig == self._tpl_dir_sig:
            return
        self._load_templates()  # 内部已更新 _tpl_dir_sig
        self.statusBar().showMessage("模板库已刷新（检测到后台配置变更）", 4000)

    def _on_template_selected(self, row: int) -> None:
        item = self.tpl_list.item(row)
        if item is None:
            return
        path = item.data(Qt.UserRole)
        self._load_template_path(str(path))

    def _load_template_path(self, path: str, keep_checks: bool = False) -> None:
        """载入模板。keep_checks=True（磁盘优先重载）：勾选态保留，值以磁盘为准。"""
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as e:
            QMessageBox.critical(self, "模板读取失败", f"{path}\n{e}")
            return
        checks = None
        if keep_checks:
            checks, _ = self.model.export_state()
        self.model.load(data)
        if checks:
            self.model.reapply_state(checks, {})
        self.current_template_path = path
        try:
            self._tpl_mtime = os.path.getmtime(path)
        except OSError:
            self._tpl_mtime = None
        self._apply_template_defaults(data)
        self._restore_expansion(path)
        self._toolbar_changed()
        self.statusBar().showMessage(f"已载入模板: {Path(path).name}", 4000)

    def _apply_template_defaults(self, data: Dict[str, Any]) -> None:
        """工具栏默认值 = 模板 exp_ctx；用户改过则不覆盖。"""
        ec = (data.get("config") or {}).get("exp_ctx") or {}
        new_domain = str(ec.get("domain") or "pixel")
        new_name = str(ec.get("exp_name") or "")
        cur_domain = self.domain_combo.currentText()
        cur_name = self.exp_name_edit.text()
        if cur_domain in ("", self._tpl_default.get("domain", "")):
            self.domain_combo.setCurrentText(new_domain)
        if cur_name in ("", self._tpl_default.get("exp_name", "")):
            self.exp_name_edit.setText(new_name)
        self._tpl_default = {"domain": new_domain, "exp_name": new_name}

    def _default_collapse(self) -> None:
        """叶数>20 的分组默认折叠。"""
        self.tree.collapseAll()

        def walk(node, index):
            for i, c in enumerate(node.children):
                ci = self.proxy.index(i, 0, index)
                if c.children and c.leaf_count() > 20:
                    self.tree.setExpanded(ci, False)
                else:
                    self.tree.setExpanded(ci, True)
                walk(c, ci)

        for i in range(self.model.rowCount()):
            idx = self.proxy.index(i, 0)
            node = self.model.node_from_index(self.model.index(i, 0))
            if node is None:
                continue
            self.tree.setExpanded(idx, not (node.children and node.leaf_count() > 20))
            walk(node, idx)

    def _restore_expansion(self, path: str) -> None:
        self._default_collapse()
        saved = self.settings.value(f"expand/{Path(path).name}")
        if not saved:
            return
        for p in [x for x in str(saved).split("|") if x]:
            node = self.model.find_path(p)
            if node is None:
                continue
            idx = self.proxy.mapFromSource(self._source_index_of(node))
            if idx.isValid():
                self.tree.expand(idx)

    def _source_index_of(self, node):
        return self.model.index(node.row, 0, self._parent_index(node))

    def _parent_index(self, node):
        p = node.parent
        if p is None or p.key == "\0root":
            from PySide6.QtCore import QModelIndex
            return QModelIndex()
        return self.model.index(p.row, 0, self._parent_index(p))

    # ---------------- 过滤 / 预览 ----------------

    def _apply_filter(self) -> None:
        needle = self.filter_edit.text()
        self.proxy.set_needle(needle)
        if needle.strip():
            self.tree.expandAll()
        else:
            if self.current_template_path:
                self._restore_expansion(self.current_template_path)

    def _on_edited(self) -> None:
        self.preview_timer.start()

    def _toolbar_changed(self) -> None:
        self.model.sync_runtime_params(
            self.serial_combo.currentText().strip(),
            self.domain_combo.currentText().strip(),
            self.exp_name_edit.text().strip())
        self.preview_timer.start()

    def _refresh_preview(self) -> None:
        cfg = self.build_submit_config()
        try:
            text = json.dumps(cfg, ensure_ascii=False, indent=1)
        except (TypeError, ValueError) as e:
            text = f"(预览序列化失败: {e})"
        self.preview.setPlainText(text)
        self.field_count_label.setText(f"字段: {text.count(':')}")

    # ---------------- 提交协议 ----------------

    def build_submit_config(self) -> Dict[str, Any]:
        """修剪树 + 注入运行参数（serial/exp_ctx）。预览与提交共用。"""
        cfg = self.model.prune_to_config()
        serial = self.serial_combo.currentText().strip()
        domain = self.domain_combo.currentText().strip() or "pixel"
        exp_name = self.exp_name_edit.text().strip()
        cfg["serial"] = serial
        ec = cfg.setdefault("config", {}).setdefault("exp_ctx", {})
        ec["domain"] = domain
        ec["serial"] = serial
        if exp_name:
            ec["exp_name"] = exp_name
        return cfg

    def _enqueue(self) -> None:
        cfg = self.build_submit_config()
        serial = self.serial_combo.currentText().strip()
        out_base = self.out_edit.text().strip()
        if not serial:
            QMessageBox.critical(self, "无法入队", "设备 serial 为空（下拉或手填）")
            return
        if not out_base:
            QMessageBox.critical(self, "无法入队", "输出基础目录为空")
            return
        backend = ((cfg.get("config") or {}).get("backend") or {})
        name = backend.get("name")
        reg = backend_registry()
        if not name:
            QMessageBox.critical(
                self, "无法入队",
                "最终配置缺少 config.backend.name（检查 backend 是否被勾选）")
            return
        if name not in reg:
            QMessageBox.critical(
                self, "无法入队",
                f"后端 {name!r} 未注册。已注册: {sorted(reg) or '(注册失败)'}")
            return
        exp_name = self.exp_name_edit.text().strip()
        domain = self.domain_combo.currentText().strip() or "pixel"
        ts = time.strftime("%Y%m%d_%H%M%S") + f"_{int(time.time() * 1000) % 1000:03d}"
        submission_id = f"{sanitize_name(exp_name)}_{ts}"
        job = {"submission_id": submission_id, "exp_name": exp_name,
               "domain": domain, "serial": serial, "out_base": out_base,
               "state": "pending", "submitted_at": ts, "pid": None,
               "run_dir": None, "exp_id": None, "note": ""}
        d = JOB_ROOT / submission_id
        d.mkdir(parents=True, exist_ok=True)
        (d / "input_config.json").write_text(
            json.dumps(cfg, ensure_ascii=False, indent=1) + "\n",
            encoding="utf-8")
        self._write_job(job)
        self.jobs.append(job)
        self._load_job_queue_view()
        self.tabs.setCurrentWidget(self.queue_tab)
        self.statusBar().showMessage(f"已入队: {submission_id}", 5000)
        self._monitor_tick()

    def _load_job_queue_view(self) -> None:
        self.queue_tab.refresh_jobs(self.jobs)

    def _monitor_tick(self) -> None:
        """2s 循环：启动可跑作业 / 跟踪子进程 / 模板文件与目录热刷新 / 看板。"""
        try:
            self._tick_jobs()
            self._poll_template_mtime()
            self._poll_template_dir()
        finally:
            self.queue_tab.refresh_cursor()
            self._load_job_queue_view()
            self._update_audit_candidates()

    def _tick_jobs(self) -> None:
        by_key: Dict[tuple, List[Dict[str, Any]]] = {}
        for j in self.jobs:
            if j.get("state") in ("pending", "started"):
                by_key.setdefault((j["domain"], j["serial"]), []).append(j)
        for (domain, serial), group in by_key.items():
            group.sort(key=lambda j: j.get("submitted_at", ""))
            job = group[0]
            if job["state"] == "pending":
                e = self._lock_entry(domain, serial)
                claimable = e is None or e.get("state") in ("done", "failed")
                if claimable:
                    self._spawn(job)
            if job["state"] == "started":
                proc = job.get("_proc")
                if proc is not None and proc.poll() is not None:
                    self._on_proc_exit(job, proc.returncode)
                elif job.get("run_dir") is None:
                    e = self._lock_entry(domain, serial)
                    if e and e.get("state") == "running" \
                            and e.get("exp_id", "").startswith(
                                sanitize_name(job.get("exp_name", ""))):
                        job["exp_id"] = e.get("exp_id")
                        job["run_dir"] = e.get("run_dir")
                        job["pid"] = e.get("pid")
                        self._write_job(job)

    def _lock_entry(self, domain: str, serial: str) -> Optional[Dict[str, Any]]:
        try:
            return exp_lock_get(domain, serial)
        except Exception:
            return None

    def _spawn(self, job: Dict[str, Any]) -> None:
        d = JOB_ROOT / job["submission_id"]
        env = dict(os.environ)
        env["PYTHONPATH"] = str(SRC_DIR) + os.pathsep + env.get("PYTHONPATH", "")
        cmd = [sys.executable, "-m", "exp_framework.experiment.runner",
               "--from-config", str(d / "input_config.json"),
               "--serial", job["serial"],
               "--out-dir", job["out_base"]]
        log = open(d / "runner.log", "ab")
        proc = subprocess.Popen(cmd, cwd=str(REPO_ROOT), env=env,
                                stdout=log, stderr=subprocess.STDOUT)
        job["_proc"] = proc
        job["pid"] = proc.pid
        job["state"] = "started"
        self._write_job(job)
        self.statusBar().showMessage(
            f"启动实验 {job['submission_id']} (pid={proc.pid})", 5000)

    def _on_proc_exit(self, job: Dict[str, Any], rc: int) -> None:
        log_text = ""
        try:
            log_text = (JOB_ROOT / job["submission_id"] / "runner.log") \
                .read_text(encoding="utf-8", errors="replace")[-4000:]
        except OSError:
            pass
        if rc == 0:
            job["state"] = "finished"
            job["note"] = "runner 正常退出"
        elif "已被占用" in log_text or "rejected:busy" in log_text:
            job["state"] = "pending"
            job["note"] = "被抢先占用，已重新排队"
        else:
            job["state"] = "failed"
            job["note"] = f"runner 退出码 {rc}（详见 runner.log）"
        self._write_job(job)

    # ---------------- 停止 / 打开 ----------------

    def _stop_selected(self, kind: str, ident: str) -> None:
        pid = None
        if kind == "job":
            for j in self.jobs:
                if j.get("submission_id") == ident:
                    pid = j.get("pid")
                    break
        elif kind == "cursor":
            for key in (ident,):
                try:
                    domain, serial = key.split(":", 1)
                    e = self._lock_entry(domain, serial)
                    pid = (e or {}).get("pid")
                except (ValueError, AttributeError):
                    pid = None
        if not pid:
            QMessageBox.warning(self, "停止", "未找到可停止的 pid")
            return
        try:
            os.kill(int(pid), 2)  # SIGINT：runner 信号处理器执行清理
            self.statusBar().showMessage(f"已向 pid={pid} 发送 SIGINT", 4000)
        except (OSError, ValueError) as e:
            QMessageBox.critical(self, "停止失败", f"pid={pid}: {e}")

    def _open_path(self, path: str) -> None:
        if path and Path(path).exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _audit_load(self, run_dir: str) -> None:
        self.audit_tab.update_candidates([run_dir])
        self.audit_tab.load_run(run_dir)
        self.tabs.setCurrentWidget(self.audit_tab)

    def _update_audit_candidates(self) -> None:
        dirs = []
        for j in self.jobs:
            if j.get("run_dir"):
                dirs.append(j["run_dir"])
        try:
            data = exp_lock_list()
            for e in (data.get("entries") or {}).values():
                if e.get("run_dir"):
                    dirs.append(e["run_dir"])
        except Exception:
            pass
        self.audit_tab.update_candidates(dirs)

    # ---------------- 编辑器联动 ----------------

    def _editor_cmd(self) -> str:
        return str(self.settings.value("editor_cmd", "")
                   or os.environ.get("VISUAL")
                   or os.environ.get("EDITOR")
                   or "code")

    def _open_editor(self) -> None:
        if not self.current_template_path:
            QMessageBox.information(self, "用编辑器打开", "请先选择一个模板")
            return
        self._open_editor_path(self.current_template_path)

    def _open_editor_path(self, path: str) -> None:
        try:
            parts = shlex.split(self._editor_cmd())
        except ValueError:
            parts = [self._editor_cmd()]
        if not parts:
            QMessageBox.critical(self, "用编辑器打开", "编辑器命令为空（⚙ 设置）")
            return
        try:
            subprocess.Popen(parts + [str(path)])
        except OSError as e:
            QMessageBox.critical(self, "用编辑器打开",
                                 f"{self._editor_cmd()}: {e}")

    def _reload_template_from_disk(self) -> None:
        """磁盘优先重载：勾选态保留，字段值以磁盘为准（丢弃未保存的内存编辑）。"""
        path = self.current_template_path
        if not path:
            return
        self._load_template_path(path, keep_checks=True)
        self.statusBar().showMessage(
            f"模板已在磁盘被修改，已重载（勾选态保留，值以磁盘为准）: "
            f"{Path(path).name}", 5000)

    def _poll_template_mtime(self) -> None:
        """2s 轮询当前模板 mtime（不依赖 inotify，配额耗尽的机器也能热重载）。"""
        path = self.current_template_path
        if not path:
            return
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return  # 编辑器原子替换的瞬间文件暂缺，下轮再查
        if self._tpl_mtime is not None and mtime != self._tpl_mtime:
            self._reload_template_from_disk()
        self._tpl_mtime = mtime

    def _on_template_clicked(self, item: QListWidgetItem) -> None:
        """Ctrl+单击模板项 = 直接用编辑器打开（普通单击照常载入）。"""
        if QApplication.keyboardModifiers() & Qt.ControlModifier:
            self._open_editor_path(str(item.data(Qt.UserRole)))

    def _toggle_detach(self) -> None:
        if self._detach_dialog is None:
            self._detach()
        else:
            self._retach()

    def _detach(self) -> None:
        """把中央编辑区（过滤+树+预览）摘到独立大窗口；原位放占位保持布局。"""
        if self._detach_dialog is not None or self._center_widget is None:
            return
        dlg = QDialog(self, Qt.Window)
        dlg.setWindowTitle(
            f"配置编辑区 — {Path(self.current_template_path or '').name}")
        dlg.resize(980, 980)
        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(0, 0, 0, 0)
        self._center_split_index = self._split.indexOf(self._center_widget)
        self._split.replaceWidget(self._center_split_index, QWidget())
        lay.addWidget(self._center_widget)
        back = QPushButton("← 返回主窗")
        back.clicked.connect(self._retach)
        lay.addWidget(back)
        dlg.finished.connect(self._retach)  # X 关闭同样回主窗
        self._detach_dialog = dlg
        self._detach_btn.setText("⧉ 返回主窗")
        self._center_widget.show()
        dlg.show()

    def _retach(self) -> None:
        dlg = self._detach_dialog
        if dlg is None or self._center_widget is None:
            return
        self._detach_dialog = None  # 先清标记，防 finished 信号递归
        dlg.finished.disconnect(self._retach)
        dlg.layout().removeWidget(self._center_widget)
        self._center_widget.setParent(self)
        dlg.deleteLater()
        self._split.replaceWidget(self._center_split_index,
                                  self._center_widget)
        self._center_widget.show()
        self._detach_btn.setText("⧉ 独立编辑区")

    # ---------------- 右键菜单 / 批量编辑 ----------------

    def _context_menu(self, pos) -> None:
        idx = self.tree.indexAt(pos)
        if not idx.isValid():
            return
        src = self.proxy.mapToSource(idx)
        node = self.model.node_from_index(src)
        if node is None:
            return
        menu = QMenu(self)
        s0 = self.proxy.mapToSource(idx.siblingAtColumn(0))
        v0 = self.proxy.mapToSource(idx.siblingAtColumn(2))
        if node.ntype in ("list_scalar", "list_obj"):
            menu.addAction("批量编辑(逐行)", lambda: self._batch_scalar(node)
                           if node.ntype == "list_scalar" else None)
            menu.addAction("批量编辑(JSON)", lambda: self._batch_json(node))
            menu.addAction("添加一项", lambda: self.model.add_list_item(node))
        if node.parent is not None and node.parent.ntype in (
                "list_scalar", "list_obj"):
            menu.addAction("上移", lambda: self.model.move_item(node, -1))
            menu.addAction("下移", lambda: self.model.move_item(node, +1))
        if node.parent is not None and self.model.can_remove(node):
            menu.addAction("删除该节点", lambda: self.model.remove_path(node))
        menu.addSeparator()
        menu.addAction("全部勾选", lambda: self.model.set_checked(
            node, Qt.Checked))
        menu.addAction("全部取消", lambda: self.model.set_checked(
            node, Qt.Unchecked))
        if node.children:
            menu.addSeparator()
            menu.addAction("展开子树", lambda: self.tree.expandRecursively(
                self.proxy.mapFromSource(s0)))
            menu.addAction("折叠子树", lambda: self.tree.collapse(
                self.proxy.mapFromSource(s0)))
        menu.addSeparator()
        menu.addAction("复制路径", lambda: QApplication.clipboard().setText(
            node.path()))
        if not node.children:
            menu.addAction("复制值", lambda: QApplication.clipboard().setText(
                str(node.value)))
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    def _batch_scalar(self, node) -> None:
        lines = "\n".join(str(c.value) for c in node.children)
        text = run_batch_dialog(self, f"批量编辑: {node.path()}", lines)
        if text is not None:
            self.model.batch_set_scalar_items(
                node, [ln for ln in text.splitlines() if True])

    def _batch_json(self, node) -> None:
        text = run_batch_dialog(
            self, f"批量编辑(JSON): {node.path()}",
            json.dumps([c.value for c in node.children],
                       ensure_ascii=False, indent=1),
            json_mode=True)
        if text is not None:
            if not self.model.batch_set_json_items(node, text):
                QMessageBox.critical(self, "批量编辑", "JSON 非法或不是数组")

    # ---------------- 杂项 ----------------

    def _browse_out(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "输出基础目录",
                                             self.out_edit.text() or str(
                                                 Path.home()))
        if d:
            self.out_edit.setText(d)

    def _save_as_template(self) -> None:
        """当前工作台（勾选+编辑后的修剪树，保留元数据）另存为 config/ 下模板。"""
        if self.model.rowCount() == 0:
            QMessageBox.information(self, "另存为模板", "请先载入一个模板")
            return
        name, ok = QInputDialog.getText(
            self, "另存为模板",
            "模板名（写入模板目录，[A-Za-z0-9_-]）:")
        if not ok:
            return
        name = sanitize_name(name, "")
        if not name:
            QMessageBox.warning(self, "另存为模板", "名字非法")
            return
        path = self._template_dir() / f"{name}.json"
        if path.exists() and QMessageBox.question(
                self, "另存为模板",
                f"{path.name} 已存在，覆盖？") != QMessageBox.Yes:
            return
        data = self.model.prune_to_config(strip_underscore=False)
        try:
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8")
        except OSError as e:
            QMessageBox.critical(self, "另存为模板", f"写入失败: {e}")
            return
        self._load_templates()
        for i in range(self.tpl_list.count()):
            if self.tpl_list.item(i).data(Qt.UserRole) == str(path):
                self.tpl_list.setCurrentRow(i)
                break
        self.statusBar().showMessage(f"模板已保存: {path.name}", 5000)

    def _open_settings(self) -> None:
        dlg = SettingsDialog(self, self.settings)
        if dlg.exec():
            self._load_templates()

    def _refresh_devices(self) -> None:
        def work():
            try:
                out = subprocess.run(["adb", "devices", "-l"],
                                     capture_output=True, text=True,
                                     timeout=10).stdout
            except Exception:
                out = ""
            serials = []
            for ln in out.splitlines()[1:]:
                parts = ln.split()
                if len(parts) >= 2 and parts[1] == "device":
                    serials.append(parts[0])
            self.devices_ready.emit(serials)

        threading.Thread(target=work, daemon=True).start()

    def _on_devices(self, serials: List[str]) -> None:
        cur = self.serial_combo.currentText()
        for s in serials:
            if self.serial_combo.findText(s) < 0:
                self.serial_combo.addItem(s)
        if serials and cur not in serials:
            self.serial_combo.setCurrentText(serials[0])

    def capture_screenshot(self, path: str, select_template: str = "") -> None:
        if select_template:
            for i in range(self.tpl_list.count()):
                item = self.tpl_list.item(i)
                if Path(item.data(Qt.UserRole)).name == select_template:
                    self.tpl_list.setCurrentRow(i)
                    break
        QApplication.processEvents()
        pm = self.grab()
        pm.save(path)
        self.statusBar().showMessage(f"截图已保存: {path}", 3000)


def _preflight_qt(platform: str) -> None:
    """Qt 平台插件预检：xcb 缺系统库时进程会直接 abort（无法捕获），
    先在子进程探测一次，失败时给出可读修复提示。"""
    env = dict(os.environ)
    if platform:
        env["QT_QPA_PLATFORM"] = platform
    probe = subprocess.run(
        [sys.executable, "-c",
         "from PySide6.QtWidgets import QApplication; "
         "a = QApplication([]); print(a.platformName())"],
        capture_output=True, text=True, timeout=30, env=env)
    if probe.returncode != 0:
        tail = [ln for ln in (probe.stderr or probe.stdout or "")
                .strip().splitlines() if ln.strip()][-3:]
        print("Qt 平台插件初始化失败（预检）：", file=sys.stderr)
        print("\n".join(tail), file=sys.stderr)
        print("常见修复：\n"
              "  sudo apt install libxcb-cursor0     # xcb 提示缺 cursor 库\n"
              "  QT_QPA_PLATFORM=offscreen python3 scripts/expf_ui.py   # 无显示环境\n"
              "  或用 --platform wayland|offscreen 指定平台",
              file=sys.stderr)
        raise SystemExit(2)


def _install_sig_handlers(app: QApplication) -> None:
    """Ctrl+C 立即退出：Qt 事件循环 app.exec() 是单个 C++ 调用，Python 的
    信号处理器只在字节码间隙执行——主线程陷在 C++ 里时 SIGINT 永远排队。
    两个配合：① 自装 handler 只做 app.quit()（干净退出，无 KeyboardInterrupt 栈）；
    ② 200ms 心跳 QTimer 强制把控制权周期性交还 Python 层，让 handler 得以运行。
    """
    def _handler(sig, _frame):
        app.quit()

    signal.signal(signal.SIGINT, _handler)
    signal.signal(signal.SIGTERM, _handler)
    keeplier = QTimer()
    keeplier.timeout.connect(lambda: None)
    keeplier.start(200)
    app._sig_keepalive = keeplier  # 防 GC 回收（无 parent 的 QTimer）


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="expf_ui 本地 GUI（Qt）：配置工作台 + 运行队列/审计")
    parser.add_argument("--screenshot", default=None, metavar="PATH",
                        help="渲染主窗保存 PNG 后退出（无头调试用）")
    parser.add_argument("--wait-ms", type=int, default=800)
    parser.add_argument("--select-template", default="", metavar="NAME")
    parser.add_argument("--platform", default="", metavar="NAME",
                        help="Qt 平台（xcb/wayland/offscreen），默认自动")
    args = parser.parse_args(argv)

    if args.platform:
        os.environ["QT_QPA_PLATFORM"] = args.platform
    if args.screenshot:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    try:
        from PySide6.QtWidgets import QApplication  # noqa: F811
    except ImportError:
        print("缺少 PySide6：pip install PySide6", file=sys.stderr)
        return 2
    if os.environ.get("QT_QPA_PLATFORM") != "offscreen":
        _preflight_qt(os.environ.get("QT_QPA_PLATFORM", ""))

    app = QApplication.instance() or QApplication([])
    _install_sig_handlers(app)
    win = MainWindow()
    win.show()
    if args.screenshot:
        def _shot():
            win.capture_screenshot(args.screenshot, args.select_template)
            app.quit()
        QTimer.singleShot(args.wait_ms, _shot)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
