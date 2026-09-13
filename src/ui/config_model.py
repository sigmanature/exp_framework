"""配置树模型：模板 JSON <-> 勾选/编辑三态工作台（Qt Model/View）。

角色与语义：
- 每个叶子一行：勾选(包含/排除) + 键名 + 值；容器行：三态勾选 + 折叠
- 最终配置 = 修剪树：只保留勾选节点（顶层 "_" 开头的元数据键提交时剔除）
- 运行时记录字段(start_host_ts/status/packages_resolved/samples/...)：只读、默认不勾
- 工具栏联动字段(serial, config.exp_ctx.{domain,exp_name,serial})：值列只读，随工具栏刷新

性能约定（展开滚动是压测大头）：
- QAbstractItemModel + internalPointer 直接持 Node，index()/parent()/rowCount() 全 O(1)
- 不用 QTreeWidget.setItemWidget；委托只负责编辑器，绘制走默认（无自绘）
- 视图侧 setUniformRowHeights(True)（main_window 负责），滚动时模型零信号
- dataChanged 按"受影响子树/祖先链"精确发射，不做全模型信号
- path()/tooltip 惰性缓存；列表重编号时子树缓存与元素级编辑记录整体失效
- find_path 贪心匹配含 "." 的键（如 sysctl 参数名做键）
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

from PySide6.QtCore import (QAbstractItemModel, QModelIndex, QObject,
                            QRegularExpression, QLocale,
                            QSortFilterProxyModel, Qt, Signal)
from PySide6.QtGui import QBrush, QColor, QDoubleValidator
from PySide6.QtGui import QRegularExpressionValidator
from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QLabel,
                               QLineEdit, QPlainTextEdit,
                               QStyledItemDelegate)

# ---- 顶层运行时记录字段（结果档案，不是输入配置）：GUI 不建节点 ----
RUNTIME_TOP_KEYS = {"start_host_ts", "end_host_ts", "status",
                    "packages_resolved", "samples", "sample_errors"}

# ---- 采样主开关路径：取消勾选 = 输出 {"enabled": false}（框架级关断）----
MASTER_GATE_PATH = "sample_config"

# ---- 工具栏联动字段：path -> 说明 ----
SYNC_PATHS = {
    "serial": "设备 serial（工具栏管理）",
    "config.exp_ctx.domain": "实验域（工具栏管理）",
    "config.exp_ctx.exp_name": "实验名（工具栏管理）",
    "config.exp_ctx.serial": "exp_ctx.serial（工具栏管理）",
}

_TYPE_LABEL = {"dict": "对象", "list_scalar": "列表", "list_obj": "对象列表",
               "bool": "布尔", "int": "整数", "float": "浮点", "str": "字符串",
               "null": "空"}

_SKIP = object()  # 修剪时表示"剔除"


class Node:
    """树节点：key/值/类型/勾选态；row=在父 children 中的序号（O(1) parent）。"""
    __slots__ = ("key", "value", "ntype", "parent", "children", "checked",
                 "runtime", "sync", "gated", "is_element", "row", "_path")

    def __init__(self, key: str, value: Any, ntype: str, parent: "Node",
                 is_element: bool = False):
        self.key = key
        self.value = value
        self.ntype = ntype
        self.parent = parent
        self.children: List["Node"] = []
        self.checked = Qt.Checked
        self.runtime = False
        self.sync = ""
        self.gated = False  # 开关型容器：勾选态 ↔ enabled 值联动（sample_config 域）
        self.is_element = is_element
        self.row = 0
        self._path: Optional[str] = None

    def path(self) -> str:
        if self._path is None:
            if self.parent is None or self.parent.key == "\0root":
                self._path = self.key
            else:
                self._path = f"{self.parent.path()}.{self.key}"
        return self._path

    def invalidate_paths(self) -> None:
        self._path = None
        for c in self.children:
            c.invalidate_paths()

    def leaf_count(self) -> int:
        if not self.children:
            return 1
        return sum(c.leaf_count() for c in self.children)


def _detect_type(value: Any) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    return "null"


def build_node(key: str, value: Any, parent: Optional[Node],
               is_element: bool = False) -> Node:
    """JSON 值 -> 节点（dict/list 递归；列表元素 key="[i]"，is_element 全程传递）。"""
    if isinstance(value, dict):
        node = Node(key, None, "dict", parent, is_element=is_element)
        for i, (k, v) in enumerate(value.items()):
            child = build_node(str(k), v, node)
            child.row = i
            node.children.append(child)
        return node
    if isinstance(value, list):
        scalar = all(not isinstance(v, (dict, list)) for v in value)
        node = Node(key, None, "list_scalar" if scalar else "list_obj",
                    parent, is_element=is_element)
        for i, v in enumerate(value):
            child = build_node(f"[{i}]", v, node, is_element=True)
            child.row = i
            node.children.append(child)
        return node
    return Node(key, value, _detect_type(value), parent, is_element=is_element)


class ConfigTreeModel(QAbstractItemModel):
    """配置树模型。列：0=包含 1=键/路径 2=值 3=类型。"""

    COL_CHECK, COL_KEY, COL_VALUE, COL_TYPE = range(4)
    edited = Signal()  # 任何勾选/编辑/结构变化（供 JSON 预览防抖刷新）

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._root = Node("\0root", None, "dict", None)
        self._edits: Dict[str, Any] = {}
        self._gray = QBrush(QColor(120, 120, 120))
        self._blue = QBrush(QColor(40, 90, 160))

    # ---------------- 装载 ----------------

    def load(self, data: Dict[str, Any]) -> None:
        """整棵模板重建（模板切换/外部编辑器保存后调用）。"""
        self.beginResetModel()
        self._root = Node("\0root", None, "dict", None)
        children = []
        for k, v in data.items():
            if k in RUNTIME_TOP_KEYS:
                continue  # 运行时记录字段不进工作台（提交/另存天然不带）
            child = build_node(str(k), v, self._root)
            child.row = len(children)
            children.append(child)
        self._root.children = children
        self._mark_sync(self._root, "")
        self._mark_gated(self._root, "")
        self._edits = {}
        self.endResetModel()

    def _mark_gated(self, node: Node, prefix: str) -> None:
        """标记开关型容器（沿传路径串，不预缓存全树 path()）。

        范围（对应 sample.py 的域级 enabled 守卫）：
        - sample_config 本体（主开关，runner 读 enabled 决定是否采样）
        - sample_config 的直接子域（tasktime/trace/power/lock_stat/cycle_sample）
        - cycle_sample 的直接子域（counters/vmstat/... 采样域）
        - 任意位置直接含布尔 enabled 子节点的 dict（如 vmstat.buddyinfo）
        enabled 叶子值保留模板原值；用户切换容器勾选时才联动（set_checked）。
        """
        for c in node.children:
            path = f"{prefix}.{c.key}" if prefix else c.key
            if c.ntype == "dict" and c.children and not c.is_element:
                has_enabled = any(x.key == "enabled" and x.ntype == "bool"
                                  for x in c.children)
                if path == MASTER_GATE_PATH:
                    c.gated = True
                elif path.startswith(MASTER_GATE_PATH + "."):
                    rel = path[len(MASTER_GATE_PATH) + 1:].split(".")
                    if len(rel) == 1 or (rel[0] == "cycle_sample"
                                         and len(rel) == 2):
                        c.gated = True  # 直接子域 / cycle 采样域
                if has_enabled:
                    c.gated = True
                if c.gated:
                    for x in c.children:
                        if x.key == "enabled" and x.ntype == "bool":
                            x.sync = "enabled（随容器勾选联动，勿手改）"
            if c.children:
                self._mark_gated(c, path)

    def _gated_enabled_value(self, node: Node) -> Any:
        """gated 容器输出用 enabled 值：未勾=必 false；勾了=用模板/联动值。"""
        if node.checked == Qt.Unchecked:
            return False
        for x in node.children:
            if x.key == "enabled" and x.ntype == "bool":
                return x.value
        return True  # 模板未携带 enabled 的合成场景（勾选=开）

    def _sync_gated_values(self, node: Node) -> None:
        """用户切换勾选后，把子树内 gated 容器的 enabled 叶子值对齐新状态。"""
        stack = [node]
        while stack:
            n = stack.pop()
            if n.gated:
                val = n.checked != Qt.Unchecked
                for x in n.children:
                    if x.key == "enabled" and x.ntype == "bool":
                        x.value = val
            stack.extend(n.children)

    def can_remove(self, node: Node) -> bool:
        """右键菜单用：gated/runtime/sync 子树不可删（关断语义走取消勾选）。"""
        p = node.parent
        if p is None or node.runtime or node.sync or node.gated \
                or (p is self._root and node.key in ("config", "sample_config")) \
                or self._subtree_has_locked(node):
            return False
        return True

    def _mark_sync(self, node: Node, prefix: str) -> None:
        """递归标记联动字段；沿传路径串，不预缓存全树 path()。"""
        for c in node.children:
            path = f"{prefix}.{c.key}" if prefix else c.key
            if path in SYNC_PATHS:
                c.sync = SYNC_PATHS[path]
            if c.children:
                self._mark_sync(c, path)

    def node_from_index(self, index: QModelIndex) -> Optional[Node]:
        if not index.isValid():
            return None
        return index.internalPointer()

    def find_path(self, path: str) -> Optional[Node]:
        """按 "." 分段贪心匹配（兼容键名本身含 "." 的情况）。"""
        return self._find(self._root, path.split("."))

    def _find(self, node: Node, segs: List[str]) -> Optional[Node]:
        if not segs:
            return None
        for take in range(len(segs), 0, -1):
            cand = ".".join(segs[:take])
            for c in node.children:
                if c.key == cand:
                    if take == len(segs):
                        return c
                    hit = self._find(c, segs[take:])
                    if hit is not None:
                        return hit
        return None

    # ---------------- QAbstractItemModel 基本面（全部 O(1)）----------------

    def index(self, row: int, col: int, parent: QModelIndex = QModelIndex()) -> QModelIndex:
        pnode = parent.internalPointer() if parent.isValid() else self._root
        if 0 <= row < len(pnode.children):
            return self.createIndex(row, col, pnode.children[row])
        return QModelIndex()

    def parent(self, index: QModelIndex) -> QModelIndex:
        node = index.internalPointer() if index.isValid() else None
        if node is None or node.parent is None or node.parent is self._root:
            return QModelIndex()
        return self.createIndex(node.parent.row, 0, node.parent)

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        node = parent.internalPointer() if parent.isValid() else self._root
        return len(node.children)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 4

    def hasChildren(self, parent: QModelIndex = QModelIndex()) -> bool:
        node = parent.internalPointer() if parent.isValid() else self._root
        return bool(node.children)

    def headerData(self, section: int, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return ("包含", "键 / 路径", "值", "类型")[section]
        return None

    def flags(self, index: QModelIndex):
        base = Qt.ItemIsEnabled | Qt.ItemIsSelectable
        node = self.node_from_index(index)
        if index.column() == self.COL_CHECK:
            if node is not None and (node.runtime or node.sync):
                return base  # 运行时/联动字段：勾选框锁定（开关语义在容器行）
            return base | Qt.ItemIsUserCheckable
        if (index.column() == self.COL_VALUE and node is not None
                and not node.children and not node.runtime and not node.sync
                and node.ntype not in ("dict", "list_scalar", "list_obj")):
            return base | Qt.ItemIsEditable
        return base

    # ---------------- data ----------------

    def data(self, index: QModelIndex, role=Qt.DisplayRole):
        node = self.node_from_index(index)
        if node is None:
            return None
        col = index.column()
        if role == Qt.CheckStateRole:
            return node.checked if col == self.COL_CHECK else None
        if role == Qt.EditRole and col == self.COL_VALUE:
            return node.value
        if role == Qt.ToolTipRole:
            if col == self.COL_KEY:
                tip = node.path()
                if node.sync:
                    tip += f"\n[联动] {node.sync}"
                if node.runtime:
                    tip += "\n[只读] 运行时记录字段，默认不进入最终配置"
                return tip
            if col == self.COL_VALUE and isinstance(node.value, str):
                return node.value[:400]
            return None
        if role == Qt.ForegroundRole:
            if col == self.COL_VALUE and node.sync:
                return self._blue  # 联动字段值=工具栏镜像，蓝显
            if node.runtime or node.checked == Qt.Unchecked:
                return self._gray
            return None
        if role != Qt.DisplayRole:
            return None
        if col == self.COL_KEY:
            return node.key
        if col == self.COL_TYPE:
            return _TYPE_LABEL.get(node.ntype, node.ntype)
        if col == self.COL_VALUE:
            if node.sync:
                return str(node.value) if node.value is not None else "(空)"
            if node.ntype == "dict":
                tip = "· ◉开关" if node.gated else ""
                return f"〔对象 · {len(node.children)} 项{tip}〕"
            if node.ntype in ("list_scalar", "list_obj"):
                return f"〔列表 {len(node.children)} 项〕"
            if node.ntype == "bool":
                return "true" if node.value else "false"
            if node.ntype == "null":
                return "null"
            return str(node.value)
        return None

    def setData(self, index: QModelIndex, value, role=Qt.EditRole) -> bool:
        node = self.node_from_index(index)
        if node is None:
            return False
        if index.column() == self.COL_CHECK and role == Qt.CheckStateRole:
            self.set_checked(node, Qt.CheckState(value))
            self.edited.emit()
            return True
        if index.column() == self.COL_VALUE and role == Qt.EditRole:
            if not self._set_leaf_value(node, value):
                return False
            self._edits[node.path()] = node.value
            self._emit_node(node)
            self._emit_ancestors(node)
            self.edited.emit()
            return True
        return False

    def _set_leaf_value(self, node: Node, value) -> bool:
        if node.children or node.runtime or node.sync \
                or node.ntype in ("dict", "list_scalar", "list_obj"):
            return False
        try:
            if node.ntype == "bool":
                node.value = bool(value) if isinstance(value, bool) \
                    else str(value).strip().lower() in ("true", "1", "yes")
            elif node.ntype == "int":
                node.value = int(str(value).strip())
            elif node.ntype == "float":
                node.value = float(str(value).strip())
            elif node.ntype == "null":
                s = str(value)
                node.value = None if s == "" else s
                if node.value is not None:
                    node.ntype = "str"  # null 编辑为文本后类型随之升级
            else:
                node.value = str(value)
        except (TypeError, ValueError):
            return False
        return True

    # ---------------- 勾选传播 ----------------

    def set_checked(self, node: Node, state: Qt.CheckState) -> None:
        """勾选/取消：向下传播到全部后代，向上重算三态；按受影响范围发信号。"""
        self._set_subtree(node, state)
        p = node.parent
        while p is not None and p is not self._root:
            p.checked = self._derive(p)
            p = p.parent
        self._sync_gated_values(node)  # 用户切换 = 开关意图，联动 enabled 值
        if node.children:
            self._emit_node(node)
            self._emit_subtree(node)
        else:
            self._emit_node(node)
        self._emit_ancestors(node)
        self.edited.emit()  # 勾选即编辑：JSON 预览必须刷新

    def _set_subtree(self, node: Node, state: Qt.CheckState) -> None:
        node.checked = state
        for c in node.children:
            self._set_subtree(c, state)

    def _derive(self, node: Node) -> Qt.CheckState:
        if node.gated:
            # 开关型容器的勾选是用户直接控制的独立开关，
            # 不从子项派生（否则"唯一子域被关"会把主开关连带翻转）
            return node.checked
        states = [c.checked for c in node.children]
        if not states:
            return node.checked
        if all(s == Qt.Checked for s in states):
            return Qt.Checked
        if all(s == Qt.Unchecked for s in states):
            return Qt.Unchecked
        return Qt.PartiallyChecked

    def _rederive_all(self) -> None:
        """自底向上重算全部容器三态（reapply 批量回放后调用一次）。"""
        def rec(node: Node) -> None:
            for c in node.children:
                rec(c)
            if node.children:
                node.checked = self._derive(node)
        rec(self._root)

    def _rederive_up(self, node: Node) -> None:
        """结构编辑后：自 node 向上重算容器三态并刷新对应行。"""
        p = node
        while p is not None and p is not self._root:
            if p.children:
                p.checked = self._derive(p)
            self._emit_node(p)
            p = p.parent

    # ---------------- 信号发射（精确范围）----------------

    def _idx(self, node: Node, col: int = 0) -> QModelIndex:
        return self.createIndex(node.row, col, node)

    def _emit_node(self, node: Node) -> None:
        if node.parent is None:
            return
        idx = self._idx(node, 0)
        self.dataChanged.emit(idx, self._idx(node, 3))

    def _emit_subtree(self, node: Node) -> None:
        for c in node.children:
            self._emit_node(c)
            if c.children:
                self._emit_subtree(c)

    def _emit_ancestors(self, node: Node) -> None:
        p = node.parent
        while p is not None and p is not self._root:
            self._emit_node(p)
            p = p.parent

    # ---------------- 修剪导出 / 状态导出回放 ----------------

    def prune_to_config(self, strip_underscore: bool = True) -> Dict[str, Any]:
        """勾选语义 -> 最终配置（dict 部分勾选=保留勾选子项；空容器剔除）。"""
        out: Dict[str, Any] = {}
        for c in self._root.children:
            if strip_underscore and c.key.startswith("_"):
                continue
            v = self._prune(c)
            if v is not _SKIP:
                out[c.key] = v
        return out

    def _prune(self, node: Node) -> Any:
        if node.gated:
            # 开关型容器：永不因自身未勾而剔除（深合并回落默认=违背关断意图）。
            # 未勾 = 框架级关断，只留 {"enabled": false} 骨架，不下钻子树；
            # 勾选 = enabled 用模板/联动值，子项按各自勾选输出。
            d: Dict[str, Any] = {"enabled": self._gated_enabled_value(node)}
            if node.checked == Qt.Unchecked:
                return d
            for c in node.children:
                if c.key == "enabled" and c.ntype == "bool":
                    continue  # 值已显式输出
                v = self._prune(c)
                if v is not _SKIP:
                    d[c.key] = v
            return d
        if node.checked == Qt.Unchecked:
            return _SKIP
        if node.ntype == "dict":
            d = {}
            for c in node.children:
                v = self._prune(c)
                if v is not _SKIP:
                    d[c.key] = v
            # 整组显式勾选 -> 保留（含空对象）；部分勾选且无命中 -> 剔除
            return d if (d or node.checked == Qt.Checked) else _SKIP
        if node.ntype in ("list_scalar", "list_obj"):
            items = [v for c in node.children if (v := self._prune(c)) is not _SKIP]
            return items if (items or node.checked == Qt.Checked) else _SKIP
        return node.value

    def export_state(self) -> Tuple[Dict[str, int], Dict[str, Any]]:
        """(checks: path->state, edits: path->value)；外部编辑器保存后回放用。"""
        checks: Dict[str, int] = {}
        stack = list(self._root.children)
        while stack:
            n = stack.pop()
            checks[n.path()] = int(n.checked.value)
            stack.extend(n.children)
        return checks, dict(self._edits)

    def reapply_state(self, checks: Dict[str, int], edits: Dict[str, Any]) -> None:
        """回放勾选与编辑（外部重载后恢复工作台状态）。"""
        self._edits = {}
        # 按路径深度升序回放：父先于子，set_subtree 不会覆盖已回放的子状态
        for path in sorted(checks, key=lambda p: p.count(".")):
            state = checks[path]
            node = self.find_path(path)
            if node is not None:
                self._set_subtree(node, Qt.CheckState(state))
        self._rederive_all()
        for path, value in edits.items():
            node = self.find_path(path)
            if node is None:
                continue
            if node.ntype in ("list_scalar", "list_obj") and isinstance(value, list):
                self._rebuild_list(node, value)
            elif not node.children:
                self._set_leaf_value(node, value)
                self._edits[path] = node.value
        self._rederive_all()
        self._emit_subtree(self._root)
        self.edited.emit()

    # ---------------- 工具栏联动 ----------------

    def sync_runtime_params(self, serial: str, domain: str, exp_name: str) -> None:
        """工具栏值写入联动节点（只改已存在节点；缺节点由提交注入兜底）。"""
        mapping = {"serial": serial, "config.exp_ctx.serial": serial,
                   "config.exp_ctx.domain": domain,
                   "config.exp_ctx.exp_name": exp_name}
        for path, val in mapping.items():
            if not val:
                continue
            node = self.find_path(path)
            if node is not None and node.value != val:
                node.value = val
                self._emit_node(node)

    # ---------------- 结构编辑（右键菜单）----------------

    def add_list_item(self, node: Node) -> None:
        """列表尾部追加一项（标量按旧类型推默认值；对象列表按末项节点克隆结构）。"""
        if node.ntype == "list_scalar":
            if node.children and all(c.ntype == "int" for c in node.children):
                val = 0
            elif node.children and all(c.ntype == "float" for c in node.children):
                val = 0.0
            else:
                val = ""
        else:
            val = self._blank_from_node(node.children[-1]) if node.children else {}
        pidx = self._container_idx(node)
        self.beginInsertRows(pidx, len(node.children), len(node.children))
        child = build_node(f"[{len(node.children)}]", val, node, is_element=True)
        child.row = len(node.children)
        # 新元素继承父列表勾选态，避免"父未勾、子已勾"的不变量破坏
        child.checked = Qt.Unchecked if node.checked == Qt.Unchecked else Qt.Checked
        node.children.append(child)
        self.endInsertRows()
        self._rederive_up(node)
        self._edits[node.path()] = self._list_values(node)
        self.edited.emit()

    def _blank_from_node(self, node: Node) -> Any:
        """按节点结构生成空白克隆值（dict 结构保留键，标量给类型默认值）。"""
        if node.ntype == "dict":
            return {c.key: self._blank_from_node(c) for c in node.children}
        if node.ntype in ("list_scalar", "list_obj"):
            return [self._blank_from_node(c) for c in node.children]
        if node.ntype == "int":
            return 0
        if node.ntype == "float":
            return 0.0
        if node.ntype == "bool":
            return False
        if node.ntype == "null":
            return None
        return ""

    def remove_path(self, node: Node) -> bool:
        """删除该节点（元素/子树均可；根/顶层骨架/运行时/联动字段防误删）。"""
        p = node.parent
        if p is None or node.runtime or node.sync \
                or (p is self._root and node.key in ("config", "sample_config")) \
                or self._subtree_has_locked(node):
            return False
        removed_prefix = node.path() + "."
        removed_row = node.row
        pidx = self._container_idx(p)
        self.beginRemoveRows(pidx, removed_row, removed_row)
        p.children.pop(removed_row)
        if removed_row < len(p.children):
            self._renumber(p)  # 行号/键必须在 endRemoveRows 前就位（Qt 契约）
            self._drop_child_edits(p)  # 移位后兄弟元素级编辑路径整体失义
        self.endRemoveRows()
        for k in [k for k in self._edits
                  if k == removed_prefix[:-1] or k.startswith(removed_prefix)]:
            del self._edits[k]
        # 祖先链上的列表整体值全部刷新（嵌套列表内删除不能留旧值）
        anc = p
        while anc is not None and anc is not self._root:
            if anc.ntype in ("list_scalar", "list_obj"):
                self._edits[anc.path()] = self._list_values(anc)
            anc = anc.parent
        self._rederive_up(p)
        self.edited.emit()
        return True

    def move_item(self, node: Node, delta: int) -> bool:
        """列表元素上移/下移一格（Qt beginMoveRows 目标行语义已对齐）。"""
        p = node.parent
        if p is None or p.ntype not in ("list_scalar", "list_obj"):
            return False
        new_row = node.row + (1 if delta > 0 else -1)
        if not (0 <= new_row < len(p.children)):
            return False
        pidx = self._container_idx(p)
        dest = new_row + 1 if delta > 0 else new_row
        self.beginMoveRows(pidx, node.row, node.row, pidx, dest)
        item = p.children.pop(node.row)
        p.children.insert(new_row, item)
        self._renumber(p)  # endMoveRows 前行号/键必须就位
        self.endMoveRows()
        self._drop_child_edits(p)
        self._edits[p.path()] = self._list_values(p)
        self.edited.emit()
        return True

    def batch_set_scalar_items(self, node: Node, lines: List[str]) -> bool:
        if node.ntype != "list_scalar":
            return False
        vals: List[Any] = []
        for ln in lines:
            s = ln.rstrip("\n")
            try:
                vals.append(int(s))
            except ValueError:
                try:
                    vals.append(float(s))
                except ValueError:
                    vals.append(s)
        self._rebuild_list(node, vals)
        self._edits[node.path()] = vals
        self.edited.emit()
        return True

    def batch_set_json_items(self, node: Node, text: str) -> bool:
        if node.ntype not in ("list_scalar", "list_obj"):
            return False
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return False
        if not isinstance(data, list):
            return False
        self._rebuild_list(node, data)
        self._edits[node.path()] = data
        self.edited.emit()
        return True

    def _rebuild_list(self, node: Node, values: List[Any]) -> None:
        pidx = self._container_idx(node)
        if node.children:
            self.beginRemoveRows(pidx, 0, len(node.children) - 1)
            node.children = []
            self.endRemoveRows()
        if values:
            self.beginInsertRows(pidx, 0, len(values) - 1)
            for i, v in enumerate(values):
                child = build_node(f"[{i}]", v, node, is_element=True)
                child.row = i
                node.children.append(child)
            self.endInsertRows()
        # JSON 批量替换可能改变 scalar/obj 属性，重算类型
        node.ntype = "list_scalar" if all(
            not isinstance(v, (dict, list)) for v in values) else "list_obj"
        node.invalidate_paths()
        self._drop_child_edits(node)
        self._edits[node.path()] = self._list_values(node)
        self._rederive_up(node)

    def _list_values(self, node: Node) -> List[Any]:
        return [c.value for c in node.children]

    def _renumber(self, node: Node) -> None:
        for i, c in enumerate(node.children):
            c.row = i
            if c.is_element:
                c.key = f"[{i}]"
        node.invalidate_paths()

    def _drop_child_edits(self, node: Node) -> None:
        """列表重编号后，元素级编辑记录路径已失义，整体失效。"""
        prefix = node.path() + ".["
        for k in [k for k in self._edits if k.startswith(prefix)]:
            del self._edits[k]

    def _idx_parent(self, node: Node) -> QModelIndex:
        p = node.parent
        return QModelIndex() if p is None or p is self._root else self._idx(p)

    def _container_idx(self, node: Node) -> QModelIndex:
        """结构编辑的父容器索引：root -> QModelIndex()（顶层行的合法父）。"""
        if node is self._root or node.parent is None:
            return QModelIndex()
        return self.createIndex(node.row, 0, node)

    def _subtree_has_locked(self, node: Node) -> bool:
        stack = [node]
        while stack:
            n = stack.pop()
            if n.runtime or n.sync:
                return True
            stack.extend(n.children)
        return False


# ---------------- 过滤代理 ----------------

class ConfigFilterProxy(QSortFilterProxyModel):
    """键名/路径/值 子串过滤（Qt 递归过滤：命中叶子的祖先链自动保留）。"""

    def __init__(self, model: ConfigTreeModel):
        super().__init__()
        self.setSourceModel(model)
        self.setRecursiveFilteringEnabled(True)
        self._needle = ""

    def set_needle(self, text: str) -> None:
        self._needle = text.strip().lower()
        self.invalidateFilter()

    def filterAcceptsRow(self, src_row: int, src_parent: QModelIndex) -> bool:
        if not self._needle:
            return True
        m: ConfigTreeModel = self.sourceModel()
        node = m.node_from_index(m.index(src_row, 0, src_parent))
        if node is None:
            return False
        if self._needle in node.key.lower():
            return True
        if self._needle in node.path().lower():
            return True
        return (not node.children and node.value is not None
                and self._needle in str(node.value).lower())


# ---------------- 值编辑委托 ----------------

class _MultiLineEdit(QPlainTextEdit):
    """多行文本编辑器（长字符串/_template），失焦即提交。"""


class ConfigDelegate(QStyledItemDelegate):
    """按 ntype 出编辑器：bool=下拉、int/float=校验行编辑、长 str=多行框。"""

    def _node(self, index: QModelIndex) -> Optional[Node]:
        model = index.model()
        src = model.mapToSource(index) if hasattr(model, "mapToSource") else index
        sm = model.sourceModel() if hasattr(model, "sourceModel") else model
        return sm.node_from_index(src)

    def createEditor(self, parent, option, index):
        node = self._node(index)
        if node is None or node.children or node.runtime or node.sync:
            return None
        if node.ntype == "bool":
            combo = QComboBox(parent)
            combo.addItems(["false", "true"])
            combo.currentIndexChanged.connect(
                lambda _i, ed=combo: self.commitData.emit(ed))
            return combo
        if node.ntype == "str" and ("\n" in str(node.value) or len(str(node.value)) > 120):
            return _MultiLineEdit(parent)
        line = QLineEdit(parent)
        if node.ntype == "int":
            line.setValidator(QRegularExpressionValidator(
                QRegularExpression(r"^-?\d+$"), line))
        elif node.ntype == "float":
            dv = QDoubleValidator(line)
            dv.setLocale(QLocale.c())
            line.setValidator(dv)
        return line

    def setEditorData(self, editor, index):
        node = self._node(index)
        value = node.value if node is not None else index.data(Qt.EditRole)
        if isinstance(editor, QComboBox):
            editor.setCurrentIndex(1 if value else 0)
        elif isinstance(editor, _MultiLineEdit):
            editor.setPlainText("" if value is None else str(value))
        elif isinstance(editor, QLineEdit):
            editor.setText("" if value is None else str(value))
        else:
            super().setEditorData(editor, index)

    def setModelData(self, editor, model, index):
        if isinstance(editor, QComboBox):
            model.setData(index, editor.currentText() == "true", Qt.EditRole)
        elif isinstance(editor, _MultiLineEdit):
            model.setData(index, editor.toPlainText(), Qt.EditRole)
        elif isinstance(editor, QLineEdit):
            model.setData(index, editor.text(), Qt.EditRole)
        else:
            super().setModelData(editor, model, index)


# ---------------- 批量编辑对话框 ----------------

def run_batch_dialog(parent, title: str, text: str,
                     json_mode: bool = False) -> Optional[str]:
    """返回新文本；取消返回 None。json_mode 时 OK 前做 JSON 数组校验。"""
    dlg = QDialog(parent)
    dlg.setWindowTitle(title)
    dlg.resize(680, 460)
    lay = QVBoxLayout(dlg)
    lay.addWidget(QLabel("逐行一项（可直接粘贴）" if not json_mode
                         else "JSON 数组（保存前校验）"))
    edit = QPlainTextEdit()
    edit.setPlainText(text)
    lay.addWidget(edit, 1)
    err = QLabel("")
    err.setStyleSheet("color: #b00;")
    lay.addWidget(err)
    btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
    btns.rejected.connect(dlg.reject)
    lay.addWidget(btns)

    def _accept():
        t = edit.toPlainText()
        if json_mode:
            try:
                data = json.loads(t)
                if not isinstance(data, list):
                    err.setText("需要 JSON 数组，例如 [\"a\",\"b\"]")
                    return
            except json.JSONDecodeError as e:
                err.setText(f"JSON 解析失败: {e}")
                return
        dlg.accept()

    btns.accepted.connect(_accept)
    return edit.toPlainText() if dlg.exec() else None
