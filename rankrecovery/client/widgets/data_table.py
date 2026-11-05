"""A reusable table over a list of dicts: sortable, filterable, copyable, paginated."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt, Signal
from PySide6.QtGui import QAction, QBrush, QColor, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QSpinBox,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from rankrecovery.client.theme import status_colour

Column = tuple[str, str]  # (key, header)


def _display(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:.2f}"
    if isinstance(value, list):
        if value and isinstance(value[0], dict):
            parts = []
            for item in value:
                if "rank" in item and "counted" in item:
                    parts.append(f"{item['rank']} {item['counted']}/{item['required']}")
                else:
                    parts.append(json.dumps(item, sort_keys=True))
            return ", ".join(parts)
        return ", ".join(str(v) for v in value)
    if isinstance(value, dict):
        return json.dumps(value, sort_keys=True)
    return str(value)


class DictTableModel(QAbstractTableModel):
    def __init__(self, columns: list[Column], rows: list[dict] | None = None, colour_key: str | None = None, colour_fn: Callable[[dict], QColor | None] | None = None):
        super().__init__()
        self.columns = columns
        self.rows: list[dict] = rows or []
        self.colour_key = colour_key
        self.colour_fn = colour_fn

    def set_rows(self, rows: list[dict]) -> None:
        self.beginResetModel()
        self.rows = list(rows)
        self.endResetModel()

    def set_columns(self, columns: list[Column]) -> None:
        self.beginResetModel()
        self.columns = columns
        self.endResetModel()

    def rowCount(self, parent: QModelIndex | None = None) -> int:  # noqa: N802
        return 0 if (parent is not None and parent.isValid()) else len(self.rows)

    def columnCount(self, parent: QModelIndex | None = None) -> int:  # noqa: N802
        return 0 if (parent is not None and parent.isValid()) else len(self.columns)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None
        row = self.rows[index.row()]
        key = self.columns[index.column()][0]
        value = row.get(key)
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole):
            return _display(value)
        if role == Qt.ItemDataRole.UserRole:
            return value
        if role == Qt.ItemDataRole.ForegroundRole:
            colour = None
            if self.colour_fn:
                colour = self.colour_fn(row)
            elif self.colour_key and isinstance(row.get(self.colour_key), str):
                colour = status_colour(row[self.colour_key])
            if colour is not None:
                return QBrush(colour)
        if role == Qt.ItemDataRole.TextAlignmentRole and isinstance(value, int | float) and not isinstance(value, bool):
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return None

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole) -> Any:  # noqa: N802
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal and section < len(self.columns):
            return self.columns[section][1]
        return None

    def row_at(self, row: int) -> dict:
        return self.rows[row]


class _Proxy(QSortFilterProxyModel):
    def __init__(self) -> None:
        super().__init__()
        self.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.setFilterKeyColumn(-1)
        self.setSortRole(Qt.ItemDataRole.UserRole)

    def lessThan(self, left: QModelIndex, right: QModelIndex) -> bool:  # noqa: N802
        a = left.data(Qt.ItemDataRole.UserRole)
        b = right.data(Qt.ItemDataRole.UserRole)
        if a is None:
            return b is not None
        if b is None:
            return False
        try:
            return a < b  # type: ignore[operator]
        except TypeError:
            return _display(a) < _display(b)


class DataTable(QWidget):
    """Table widget with filter box, pagination and column visibility."""

    row_activated = Signal(dict)
    selection_changed = Signal(list)
    page_changed = Signal(int, int)  # limit, offset

    def __init__(self, columns: list[Column], *, paginated: bool = False, colour_key: str | None = None, colour_fn: Callable[[dict], QColor | None] | None = None, parent: QWidget | None = None):
        super().__init__(parent)
        self.model = DictTableModel(columns, colour_key=colour_key, colour_fn=colour_fn)
        self.proxy = _Proxy()
        self.proxy.setSourceModel(self.model)
        self.view = QTableView()
        self.view.setModel(self.proxy)
        self.view.setSortingEnabled(True)
        self.view.setAlternatingRowColors(True)
        self.view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.view.verticalHeader().setVisible(False)
        self.view.horizontalHeader().setStretchLastSection(True)
        self.view.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.view.horizontalHeader().setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.view.horizontalHeader().customContextMenuRequested.connect(self._column_menu)
        self.view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self._row_menu)
        self.view.doubleClicked.connect(self._activated)
        self.view.selectionModel().selectionChanged.connect(lambda *_: self.selection_changed.emit(self.selected_rows()))
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filter visible rows…")
        self.filter.setClearButtonEnabled(True)
        self.filter.textChanged.connect(self.proxy.setFilterFixedString)
        self.count_label = QLabel("")
        self.count_label.setObjectName("dim")
        top = QHBoxLayout()
        top.addWidget(self.filter, 1)
        top.addWidget(self.count_label)
        self.paginated = paginated
        self.total = 0
        self.limit_box = QSpinBox()
        self.limit_box.setRange(10, 2000)
        self.limit_box.setValue(200)
        self.offset = 0
        self.prev_btn = QPushButton("◀")
        self.next_btn = QPushButton("▶")
        self.page_label = QLabel("")
        if paginated:
            top.addWidget(QLabel("per page"))
            top.addWidget(self.limit_box)
            top.addWidget(self.prev_btn)
            top.addWidget(self.page_label)
            top.addWidget(self.next_btn)
            self.prev_btn.clicked.connect(lambda: self._page(-1))
            self.next_btn.clicked.connect(lambda: self._page(1))
            self.limit_box.valueChanged.connect(lambda _v: self._page(0))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(top)
        layout.addWidget(self.view, 1)
        copy = QAction("Copy", self)
        copy.setShortcut(QKeySequence.StandardKey.Copy)
        copy.triggered.connect(self.copy_selection)
        self.view.addAction(copy)
        self._extra_actions: list[tuple[str, Callable[[dict], None]]] = []

    # ------------------------------------------------------------------ data
    def set_rows(self, rows: list[dict], total: int | None = None) -> None:
        self.model.set_rows(rows)
        self.total = total if total is not None else len(rows)
        shown = f"{len(rows)} rows" if total is None else f"{len(rows)} of {total}"
        self.count_label.setText(shown)
        if self.paginated:
            page = self.offset // max(self.limit_box.value(), 1) + 1
            pages = max(1, -(-self.total // max(self.limit_box.value(), 1)))
            self.page_label.setText(f"page {page}/{pages}")
            self.prev_btn.setEnabled(self.offset > 0)
            self.next_btn.setEnabled(self.offset + self.limit_box.value() < self.total)
        self.view.resizeColumnsToContents()
        for col in range(len(self.model.columns)):
            if self.view.columnWidth(col) > 360:
                self.view.setColumnWidth(col, 360)

    def rows(self) -> list[dict]:
        return list(self.model.rows)

    def set_columns(self, columns: list[Column]) -> None:
        self.model.set_columns(columns)

    def selected_rows(self) -> list[dict]:
        rows = []
        for index in self.view.selectionModel().selectedRows():
            rows.append(self.model.row_at(self.proxy.mapToSource(index).row()))
        return rows

    def current_row(self) -> dict | None:
        rows = self.selected_rows()
        return rows[0] if rows else None

    def add_row_action(self, label: str, callback: Callable[[dict], None]) -> None:
        self._extra_actions.append((label, callback))

    def visible_columns(self) -> list[str]:
        return [key for i, (key, _h) in enumerate(self.model.columns) if not self.view.isColumnHidden(i)]

    def set_column_visible(self, key: str, visible: bool) -> None:
        for i, (k, _h) in enumerate(self.model.columns):
            if k == key:
                self.view.setColumnHidden(i, not visible)

    # ------------------------------------------------------------- behaviour
    def _page(self, direction: int) -> None:
        limit = self.limit_box.value()
        self.offset = max(0, self.offset + direction * limit) if direction else 0
        self.page_changed.emit(limit, self.offset)

    def _activated(self, index: QModelIndex) -> None:
        self.row_activated.emit(self.model.row_at(self.proxy.mapToSource(index).row()))

    def _column_menu(self, pos) -> None:  # noqa: ANN001
        menu = QMenu(self)
        for i, (_key, header) in enumerate(self.model.columns):
            action = QAction(header, menu)
            action.setCheckable(True)
            action.setChecked(not self.view.isColumnHidden(i))
            action.toggled.connect(lambda checked, col=i: self.view.setColumnHidden(col, not checked))
            menu.addAction(action)
        menu.exec(self.view.horizontalHeader().mapToGlobal(pos))

    def _row_menu(self, pos) -> None:  # noqa: ANN001
        menu = QMenu(self)
        menu.addAction("Copy cell", self.copy_cell)
        menu.addAction("Copy row(s)", self.copy_selection)
        menu.addAction("Copy row(s) as JSON", self.copy_json)
        row = self.current_row()
        if row is not None and self._extra_actions:
            menu.addSeparator()
            for label, callback in self._extra_actions:
                menu.addAction(label, lambda cb=callback, r=row: cb(r))
        menu.exec(self.view.viewport().mapToGlobal(pos))

    def copy_cell(self) -> None:
        index = self.view.currentIndex()
        if index.isValid():
            QApplication.clipboard().setText(index.data() or "")

    def copy_selection(self) -> None:
        lines = []
        for row in self.selected_rows():
            lines.append("\t".join(_display(row.get(k)) for k, _h in self.model.columns if not self.view.isColumnHidden(self.model.columns.index((k, _h)))))
        QApplication.clipboard().setText("\n".join(lines))

    def copy_json(self) -> None:
        QApplication.clipboard().setText(json.dumps(self.selected_rows(), indent=2, default=str))
