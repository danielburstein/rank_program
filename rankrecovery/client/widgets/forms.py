"""Small reusable dialogs and form helpers."""

from __future__ import annotations

from typing import Any

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)


class NoteDialog(QDialog):
    """Asks for a mandatory note/reason. ``value`` is None when cancelled."""

    def __init__(self, title: str, prompt: str, *, parent: QWidget | None = None, extra_fields: dict[str, Any] | None = None, placeholder: str = "Reason (required)"):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(460)
        layout = QVBoxLayout(self)
        label = QLabel(prompt)
        label.setWordWrap(True)
        layout.addWidget(label)
        self.fields: dict[str, QWidget] = {}
        if extra_fields:
            form = QFormLayout()
            for name, widget in extra_fields.items():
                form.addRow(name, widget)
                self.fields[name] = widget
            layout.addLayout(form)
        self.note = QPlainTextEdit()
        self.note.setPlaceholderText(placeholder)
        self.note.setFixedHeight(90)
        layout.addWidget(self.note)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.value: str | None = None

    def _accept(self) -> None:
        text = self.note.toPlainText().strip()
        if not text:
            QMessageBox.warning(self, "Note required", "Please enter a reason; it is stored in the audit trail.")
            return
        self.value = text
        self.accept()

    @classmethod
    def ask(cls, parent: QWidget | None, title: str, prompt: str, **kwargs: Any) -> str | None:
        dialog = cls(title, prompt, parent=parent, **kwargs)
        return dialog.value if dialog.exec() == QDialog.DialogCode.Accepted else None


class ConfirmDialog(NoteDialog):
    """Confirmation with a required note and an explicit typed confirmation for destructive actions."""

    def __init__(self, title: str, prompt: str, *, parent: QWidget | None = None, typed_confirmation: str | None = None, **kwargs: Any):
        super().__init__(title, prompt, parent=parent, **kwargs)
        self.typed = typed_confirmation
        if typed_confirmation:
            self.confirm_edit = QLineEdit()
            self.confirm_edit.setPlaceholderText(f"type {typed_confirmation} to confirm")
            self.layout().insertWidget(self.layout().count() - 1, self.confirm_edit)

    def _accept(self) -> None:
        if self.typed and self.confirm_edit.text().strip() != self.typed:
            QMessageBox.warning(self, "Confirmation", f"Type {self.typed} to confirm this action.")
            return
        super()._accept()


def choose(parent: QWidget | None, title: str, prompt: str, options: list[str], *, note_required: bool = True) -> tuple[str, str] | None:
    combo = QComboBox()
    combo.addItems(options)
    dialog = NoteDialog(title, prompt, parent=parent, extra_fields={"Choice": combo})
    if not note_required:
        dialog.note.setPlainText("-")
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return None
    return combo.currentText(), dialog.value or ""


def error_box(parent: QWidget | None, message: str, title: str = "Error") -> None:
    QMessageBox.critical(parent, title, message)


def info_box(parent: QWidget | None, message: str, title: str = "Done") -> None:
    QMessageBox.information(parent, title, message)


class KeyValueForm(QGroupBox):
    """Displays dict entries as label/value rows."""

    def __init__(self, title: str = "", parent: QWidget | None = None):
        super().__init__(title, parent)
        self.form = QFormLayout(self)
        self.form.setLabelAlignment(__import__("PySide6.QtCore", fromlist=["Qt"]).Qt.AlignmentFlag.AlignRight)

    def set_values(self, values: dict[str, Any], *, order: list[str] | None = None) -> None:
        while self.form.rowCount():
            self.form.removeRow(0)
        keys = order or list(values)
        for key in keys:
            value = values.get(key)
            if isinstance(value, list):
                text = ", ".join(str(v) for v in value) if value else "-"
            elif isinstance(value, bool):
                text = "yes" if value else "no"
            elif value is None:
                text = "-"
            else:
                text = str(value)
            label = QLabel(text)
            label.setWordWrap(True)
            label.setTextInteractionFlags(__import__("PySide6.QtCore", fromlist=["Qt"]).Qt.TextInteractionFlag.TextSelectableByMouse)
            self.form.addRow(f"{key}:", label)


def checkbox(label: str, checked: bool = False, tooltip: str = "") -> QCheckBox:
    box = QCheckBox(label)
    box.setChecked(checked)
    if tooltip:
        box.setToolTip(tooltip)
    return box
