"""Zoomable image viewer with detection overlays and region drawing."""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QGraphicsPixmapItem,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsView,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from rankrecovery.client.theme import ACCENT, CONFIRMED, ERROR, MANUAL, PROVISIONAL, WARNING

BBox = list[int]


class _View(QGraphicsView):
    region_drawn = Signal(list)
    detection_clicked = Signal(int)

    def __init__(self) -> None:
        super().__init__()
        self.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setBackgroundBrush(QBrush(QColor("#141418")))
        self.draw_mode = False
        self._origin: QPointF | None = None
        self._rubber: QGraphicsRectItem | None = None
        self.zoom = 1.0

    def wheelEvent(self, event) -> None:  # noqa: ANN001, N802
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        self.scale(factor, factor)
        self.zoom *= factor

    def set_draw_mode(self, enabled: bool) -> None:
        self.draw_mode = enabled
        self.setDragMode(QGraphicsView.DragMode.NoDrag if enabled else QGraphicsView.DragMode.ScrollHandDrag)

    def mousePressEvent(self, event) -> None:  # noqa: ANN001, N802
        if self.draw_mode and event.button() == Qt.MouseButton.LeftButton:
            self._origin = self.mapToScene(event.position().toPoint())
            self._rubber = QGraphicsRectItem(QRectF(self._origin, self._origin))
            self._rubber.setPen(QPen(QColor(ACCENT), 2, Qt.PenStyle.DashLine))
            self.scene().addItem(self._rubber)
            return
        if event.button() == Qt.MouseButton.LeftButton:
            item = self.itemAt(event.position().toPoint())
            if isinstance(item, QGraphicsRectItem) and item.data(0) is not None:
                self.detection_clicked.emit(int(item.data(0)))
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: ANN001, N802
        if self.draw_mode and self._origin is not None and self._rubber is not None:
            current = self.mapToScene(event.position().toPoint())
            self._rubber.setRect(QRectF(self._origin, current).normalized())
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: ANN001, N802
        if self.draw_mode and self._origin is not None and self._rubber is not None:
            rect = self._rubber.rect()
            self.scene().removeItem(self._rubber)
            self._rubber = None
            self._origin = None
            self.finish_region(rect)
            return
        super().mouseReleaseEvent(event)

    def finish_region(self, rect: QRectF) -> list[int] | None:
        """Convert a scene rectangle into an integer bbox [x, y, w, h] and emit it."""
        if rect.width() < 2 or rect.height() < 2:
            return None
        bbox = [int(rect.x()), int(rect.y()), int(rect.width()), int(rect.height())]
        self.region_drawn.emit(bbox)
        return bbox


def _colour_for(detection: dict) -> QColor:
    status = (detection.get("status") or "").lower()
    if detection.get("manual"):
        return QColor(MANUAL)
    if status in ("partial", "unreadable", "rejected"):
        return QColor(ERROR)
    if status == "needs_review":
        return QColor(WARNING)
    if detection.get("staff"):
        return QColor(PROVISIONAL)
    return QColor(CONFIRMED)


class ImageViewer(QWidget):
    """Shows the original image or the server-rendered overlay; draws bboxes locally too."""

    region_drawn = Signal(list)
    detection_selected = Signal(int)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.view = _View()
        self.scene = QGraphicsScene()
        self.view.setScene(self.scene)
        self.pixmap_item: QGraphicsPixmapItem | None = None
        self.detections: list[dict] = []
        self.highlight: str | None = None
        self.original: bytes | None = None
        self.overlay: bytes | None = None
        self.showing_overlay = False
        self._boxes: list[QGraphicsRectItem] = []
        self.view.region_drawn.connect(self.region_drawn)
        self.view.detection_clicked.connect(self.detection_selected)

        self.fit_btn = QPushButton("Fit")
        self.one_btn = QPushButton("1:1")
        self.toggle_btn = QPushButton("Server overlay")
        self.toggle_btn.setCheckable(True)
        self.boxes_btn = QPushButton("Boxes")
        self.boxes_btn.setCheckable(True)
        self.boxes_btn.setChecked(True)
        self.draw_btn = QPushButton("Draw region")
        self.draw_btn.setCheckable(True)
        self.info = QLabel("")
        self.info.setObjectName("dim")
        self.fit_btn.clicked.connect(self.fit)
        self.one_btn.clicked.connect(self.actual_size)
        self.toggle_btn.toggled.connect(self._toggle_overlay)
        self.boxes_btn.toggled.connect(lambda _c: self._redraw_boxes())
        self.draw_btn.toggled.connect(self.view.set_draw_mode)
        bar = QHBoxLayout()
        for w in (self.fit_btn, self.one_btn, self.toggle_btn, self.boxes_btn, self.draw_btn):
            bar.addWidget(w)
        bar.addWidget(self.info, 1)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(bar)
        layout.addWidget(self.view, 1)

    # ----------------------------------------------------------------- image
    def load_bytes(self, data: bytes, *, overlay: bytes | None = None, detections: list[dict] | None = None, highlight: str | None = None, info: str = "") -> bool:
        self.original = data
        self.overlay = overlay
        self.detections = detections or []
        self.highlight = highlight
        self.info.setText(info)
        self.toggle_btn.setEnabled(overlay is not None)
        if self.toggle_btn.isChecked() and overlay is None:
            self.toggle_btn.setChecked(False)
        return self._show(overlay if self.toggle_btn.isChecked() and overlay else data)

    def set_overlay(self, overlay: bytes | None) -> None:
        self.overlay = overlay
        self.toggle_btn.setEnabled(overlay is not None)
        if self.toggle_btn.isChecked():
            self._show(overlay or self.original or b"")

    def _show(self, data: bytes) -> bool:
        image = QImage.fromData(data)
        if image.isNull():
            self.info.setText("image could not be decoded")
            return False
        self.scene.clear()
        self._boxes = []
        self.pixmap_item = self.scene.addPixmap(QPixmap.fromImage(image))
        self.scene.setSceneRect(QRectF(0, 0, image.width(), image.height()))
        self._redraw_boxes()
        self.fit()
        return True

    def _toggle_overlay(self, checked: bool) -> None:
        self.showing_overlay = checked
        if checked and self.overlay:
            self._show(self.overlay)
        elif self.original:
            self._show(self.original)

    def _redraw_boxes(self) -> None:
        for box in self._boxes:
            self.scene.removeItem(box)
        self._boxes = []
        if not self.boxes_btn.isChecked() or self.pixmap_item is None:
            return
        highlight = (self.highlight or "").lower()
        for d in self.detections:
            bbox = d.get("bbox")
            if not bbox or len(bbox) != 4:
                continue
            colour = _colour_for(d)
            width = 2
            if highlight and (d.get("username") or "").lower() == highlight:
                colour = QColor(ACCENT)
                width = 4
            rect = QGraphicsRectItem(QRectF(bbox[0], bbox[1], bbox[2], bbox[3]))
            rect.setPen(QPen(colour, width))
            rect.setData(0, d.get("detection_id"))
            rect.setToolTip(f"{d.get('raw_prefix', '')} {d.get('username', '')} [{d.get('rank') or d.get('staff') or '-'}] {d.get('status', '')} conf={d.get('confidence', 0):.2f}")
            self.scene.addItem(rect)
            self._boxes.append(rect)
            for key, style in (("prefix_bbox", Qt.PenStyle.DotLine), ("username_bbox", Qt.PenStyle.DashLine)):
                sub = d.get(key)
                if sub and len(sub) == 4:
                    sub_rect = QGraphicsRectItem(QRectF(sub[0], sub[1], sub[2], sub[3]))
                    sub_rect.setPen(QPen(colour, 1, style))
                    self.scene.addItem(sub_rect)
                    self._boxes.append(sub_rect)

    def set_highlight(self, username: str | None) -> None:
        self.highlight = username
        self._redraw_boxes()

    def set_detections(self, detections: list[dict]) -> None:
        self.detections = detections
        self._redraw_boxes()

    # ------------------------------------------------------------------ view
    def fit(self) -> None:
        if self.pixmap_item is not None:
            self.view.fitInView(self.scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)

    def actual_size(self) -> None:
        self.view.resetTransform()
        self.view.zoom = 1.0

    def set_draw_mode(self, enabled: bool) -> None:
        self.draw_btn.setChecked(enabled)

    def simulate_region(self, x: int, y: int, w: int, h: int) -> list[int] | None:
        """Testing hook: behave as if the user rubber-banded this rectangle."""
        return self.view.finish_region(QRectF(x, y, w, h))
