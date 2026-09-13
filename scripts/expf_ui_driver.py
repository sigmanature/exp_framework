#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""expf_ui 验收驱动：offscreen 下跑通 UI 主路径 + 截图（供视觉检查）。

全部用例在临时模板目录中运行（复制 repo 模板），不污染仓库 config/。
覆盖：
  1. 选模板 -> 树渲染（运行时字段隐藏）-> 截图 stage1
  2. 主开关 sample_config：取消 = {"enabled": false}；勾选恢复
  3. 子域 gated：vmstat 取消 = enabled:false 保留骨架；主开关不被连带翻转
  4. 过滤 'burst' -> 自动展开命中 -> 截图 stage2
  5. 200+ 项长列表展开/滚动压测 -> 截图 stage3
  6. 磁盘优先重载：mtime 变化 -> 值更新、勾选态保留
  7. Ctrl+单击模板 -> 编辑器打开（monkeypatch 断言路径）
  8. 独立编辑区 detach/retach -> 模型状态保留 -> 截图 stage4
"""
import json
import os
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from exp_framework.ui.main_window import MainWindow

OUT = Path("/tmp/opencode/expf_ui")
OUT.mkdir(parents=True, exist_ok=True)
REPO_TPL = Path(__file__).resolve().parents[1] / "config"
failures = []


def check(name: str, cond: bool, detail: str = ""):
    print(f"{'PASS' if cond else 'FAIL'}: {name} {detail}")
    if not cond:
        failures.append(name)


app = QApplication.instance() or QApplication([])
win = MainWindow()
win.resize(1600, 950)
win.show()
app.processEvents()

# 隔离模板目录：复制 repo 模板，所有改动不触碰仓库；退出时恢复 QSettings
tpl_dir = OUT / "templates"
tpl_dir.mkdir(parents=True, exist_ok=True)
shutil.copy(REPO_TPL / "default_memstress_manifest.json", tpl_dir)
shutil.copy(REPO_TPL / "default_sample_config.json", tpl_dir)
_saved_tpl_dir = win.settings.value("template_dir", "")
win.settings.setValue("template_dir", str(tpl_dir))
win._load_templates()
app.processEvents()


def restore_settings():
    win.settings.setValue("template_dir", _saved_tpl_dir)


import atexit
atexit.register(restore_settings)


def select_template(suffix: str) -> int:
    for i in range(win.tpl_list.count()):
        if win.tpl_list.item(i).data(Qt.UserRole).endswith(suffix):
            win.tpl_list.setCurrentRow(i)
            app.processEvents()
            return i
    return -1


# 1. 选模板 + 运行时字段隐藏
row = select_template("default_memstress_manifest.json")
check("template listed", row >= 0)
if row >= 0:
    rect = win.tpl_list.visualItemRect(win.tpl_list.item(row))
    QTest.mouseClick(win.tpl_list.viewport(), Qt.LeftButton, Qt.NoModifier,
                     rect.center())
app.processEvents()
check("template loaded", win.current_template_path is not None and
      win.model.rowCount() > 0, f"rows={win.model.rowCount()}")
QTest.qWait(400)
m = win.model
tops = [m.index(i, 1).data() for i in range(m.rowCount())]
check("runtime fields hidden", not ({"status", "samples", "start_host_ts",
      "end_host_ts", "packages_resolved", "sample_errors"} & set(tops)),
      f"tops={tops}")
check("template meta kept", "_template" in tops)
win.capture_screenshot(str(OUT / "stage1_default.png"))
app.processEvents()

# 2. 主开关
sc = m.find_path("sample_config")
check("master gated", sc is not None and sc.gated)
m.set_checked(sc, Qt.Unchecked)
check("master off = enabled:false",
      win.build_submit_config()["sample_config"] == {"enabled": False})
m.set_checked(sc, Qt.Checked)
check("master on", win.build_submit_config()["sample_config"].get(
    "enabled") is True)

# 3. 子域 gated（自适应：在 sample_config 子树找第一个 gated 子容器；
#    模板结构随实验演进——memstress 现在只有 {interval_s, probes}，无子域则 SKIP）
def dig(d, path):
    cur = d
    for seg in path.split("."):
        cur = cur[seg]
    return cur


sub = None
stack = [sc]
while stack and sub is None:
    n = stack.pop()
    for c in (n.children if n else []):
        if c.gated and c is not sc:
            sub = c
            break
        if c.children:
            stack.append(c)
if sub is None:
    check("subdomain gated (skip)", True, "当前模板 sample_config 无子域开关")
else:
    m.set_checked(sub, Qt.Unchecked)
    cfg_sub = dig(win.build_submit_config(), sub.path())
    check("subdomain off skeleton", cfg_sub == {"enabled": False},
          json.dumps(cfg_sub))
    check("master unaffected by subdomain off",
          dig(win.build_submit_config(), sc.path())["enabled"] is True)
    m.set_checked(sub, Qt.Checked)
    check("subdomain back on",
          dig(win.build_submit_config(), sub.path()).get("enabled") is True)

# 4. 过滤
win.filter_edit.setText("burst")
QTest.qWait(300)
check("filter hits", win.proxy.rowCount() >= 1, f"rows={win.proxy.rowCount()}")
win.capture_screenshot(str(OUT / "stage2_filter.png"))

# 5. 展开滚动压测：合成 250 项大列表模板（确定性，不随 repo 模板结构演进）
big = tpl_dir / "zz_biglist_tmp.json"
big.write_text(json.dumps({
    "_template": "perf fixture", "serial": None,
    "config": {"backend": {"name": "memstress", "config": {}},
               "exp_ctx": {"domain": "pixel"}},
    "sample_config": {},
    "biglist": [f"k{i}" for i in range(250)]}, ensure_ascii=False))
win._poll_template_dir()  # 模拟后台新写配置后列表自动刷新
app.processEvents()
row = select_template("zz_biglist_tmp.json")
check("big template listed", row >= 0 and win.tpl_list.count() >= 2,
      f"count={win.tpl_list.count()}")
bl = m.find_path("biglist")
check("biglist found", bl is not None and len(bl.children) == 250,
      f"n={len(bl.children) if bl else 0}")
t0 = time.perf_counter()
win.tree.expandAll()
app.processEvents()
t1 = time.perf_counter()
check("expand 250 rows", (t1 - t0) < 1.0, f"{(t1 - t0) * 1000:.0f}ms")
sb = win.tree.verticalScrollBar()
sb.setValue(sb.maximum())
app.processEvents()
t2 = time.perf_counter()
sb.setValue(0)
app.processEvents()
sb.setValue(sb.maximum())
app.processEvents()
t3 = time.perf_counter()
check("scroll roundtrip cheap", (t3 - t2) < 0.3, f"{(t3 - t2) * 1000:.0f}ms")
QTest.qWait(350)
win.capture_screenshot(str(OUT / "stage3_biglist.png"))

# 6. 磁盘优先重载（在合成大模板上改：值更新 + 勾选态保留）
tpl_path = Path(win.current_template_path)
data = json.loads(tpl_path.read_text(encoding="utf-8"))
data["biglist"][7] = "CHANGED_ON_DISK"
data["new_top"] = {"a": 1}
tpl_path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
m.set_checked(m.find_path("biglist").children[0], Qt.Unchecked)  # 内存勾选
os.utime(tpl_path, (win._tpl_mtime + 5, win._tpl_mtime + 5))
win._poll_template_mtime()
cfg_bl = win.build_submit_config()["biglist"]
# element[0] 内存中被取消 -> 输出 249 项且索引前移，用成员断言而非下标
check("disk-first reload value", "CHANGED_ON_DISK" in cfg_bl
      and "new_top" in win.build_submit_config() and len(cfg_bl) == 249,
      f"len={len(cfg_bl)}")
bl2 = m.find_path("biglist")
check("reload keeps checks", bl2.children[0].checked == Qt.Unchecked
      and bl2.checked == Qt.PartiallyChecked)
big.unlink()  # 清理合成模板

# 7. Ctrl+单击 -> 编辑器打开（monkeypatch 捕获路径）
opened = []
orig = win._open_editor_path
win._open_editor_path = lambda p: opened.append(p)
row = select_template("default_memstress_manifest.json")
rect = win.tpl_list.visualItemRect(win.tpl_list.item(row))
QTest.mouseClick(win.tpl_list.viewport(), Qt.LeftButton,
                 Qt.ControlModifier, rect.center())
app.processEvents()
win._open_editor_path = orig
check("ctrl+click opens editor", opened == [str(tpl_dir /
      "default_memstress_manifest.json")], f"opened={opened}")

# 8. 独立编辑区 detach/retach（用当前模板存在的节点断言状态保留）
probe = m.find_path("config.backend.config.burst_size")
probe_before = probe.checked
win._toggle_detach()
app.processEvents()
check("detached dialog open", win._detach_dialog is not None and
      win._center_widget.parent() is not None
      and win._center_widget.parent() is not win)
win._detach_dialog.resize(1200, 1000)
app.processEvents()
win._detach_dialog.grab().save(str(OUT / "stage4_detached.png"))
win._toggle_detach()
app.processEvents()
check("retached", win._detach_dialog is None and
      win._center_widget.parent() is win._split)
check("state kept after detach",
      m.find_path("config.backend.config.burst_size").checked == probe_before
      and m.find_path("config.backend.config.hold_ms") is not None)
win.capture_screenshot(str(OUT / "stage5_retached.png"))

# 9. 后台新增配置 -> 模板列表自动刷新（选中保留、当前模板不重载）
list_before = win.tpl_list.count()
probe_before2 = m.find_path("config.backend.config.burst_size").checked
new_tpl = tpl_dir / "zz_backend_new_config.json"
new_tpl.write_text(json.dumps({
    "_template": "后台由模型写入的模板",
    "serial": None,
    "config": {"backend": {"name": "memstress", "config": {}},
               "exp_ctx": {"exp_name": "backend_new", "domain": "pixel"}},
    "sample_config": {}}, ensure_ascii=False, indent=2))
os.utime(new_tpl, (time.time() + 3, time.time() + 3))
win._poll_template_dir()
app.processEvents()
check("dir poll adds new template", win.tpl_list.count() == list_before + 1,
      f"{list_before} -> {win.tpl_list.count()}")
cur = win.tpl_list.currentItem()
check("selection preserved", cur is not None and
      cur.data(Qt.UserRole).endswith("default_memstress_manifest.json"))
check("current model not reloaded by dir poll",
      m.find_path("config.backend.config.burst_size").checked == probe_before2)
badge = cur.text() if cur is not None else ""
check("new item visible with backend badge", any(
    "zz_backend_new_config.json" in win.tpl_list.item(i).text() and
    "✓memstress" in win.tpl_list.item(i).text()
    for i in range(win.tpl_list.count())))
new_tpl.unlink()  # 清理：让退出后的 QSettings 恢复也指向干净目录
win._poll_template_dir()
check("dir poll removes deleted template", win.tpl_list.count() == list_before)

print("=" * 50)
if failures:
    print(f"DRIVER FAILED: {failures}")
    sys.exit(1)
print("DRIVER ALL PASS")
