# -*- coding: utf-8 -*-
"""Arrows from the analysis checks (run order) to the layer order.

The checks list the analyses in the order they are made; the layers come out
in drawing order (top is drawn on top). This widget sits to the right of the
checks, lists the resulting layers and joins each check to its layer with a
curved arrow, so the reordering can be seen. It follows the checks as they
change, moving the layers smoothly.
"""

from qgis.PyQt.QtCore import QEvent, QPointF, QVariantAnimation, Qt
from qgis.PyQt.QtGui import QColor, QPainter, QPainterPath, QPalette, QPen, QPolygonF
from qgis.PyQt.QtWidgets import QSizePolicy, QWidget

ARROW_ZONE = 44      # width of the arrow band (px)
LABEL_GAP = 6        # gap between an arrow tip and its layer name
MOVED_COLOR = QColor("#2b6cb0")  # arrows whose layer changes position


class LayerOrderArrows(QWidget):
    """Layer order to the right of the analysis checks.

    ``checks``  : [(key, QCheckBox)] in run (check) order
    ``layers``  : [(key, name)] in layer (drawing) order; a key without a
                  check (the DEM) is always made and shown without an arrow
    """

    def __init__(self, checks, layers, parent=None):
        super().__init__(parent)
        self._checks = list(checks)
        self._layers = list(layers)
        self._from = {}
        self._to = {}
        self._progress = 1.0
        self._animation = QVariantAnimation(self)
        self._animation.setDuration(260)
        self._animation.setStartValue(0.0)
        self._animation.setEndValue(1.0)
        self._animation.valueChanged.connect(self._animate)
        # The layer names sit at the right edge; the arrows take the rest.
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        for _key, check in self._checks:
            check.toggled.connect(self.refresh)
            check.installEventFilter(self)
        self._to = self._targets()
        self._from = dict(self._to)

    # --- layout -----------------------------------------------------------

    def _text_width(self):
        metrics = self.fontMetrics()
        return max(
            (metrics.horizontalAdvance(name) for _key, name in self._layers), default=0
        )

    def minimumSizeHint(self):
        return self._size(self._text_width())

    def sizeHint(self):
        return self.minimumSizeHint()

    def _size(self, text_width):
        from qgis.PyQt.QtCore import QSize

        return QSize(ARROW_ZONE + LABEL_GAP + text_width + 2, 0)

    def _active(self, check):
        return check.isEnabled() and check.isChecked()

    def _row_ys(self):
        """Centre heights of the check rows in this widget, plus one more."""
        ys = []
        parent = self.parentWidget()
        for _key, check in self._checks:
            centre = check.geometry().center()
            ys.append(float(self.mapFrom(parent, centre).y()))
        step = (ys[-1] - ys[0]) / (len(ys) - 1) if len(ys) > 1 else 20.0
        while len(ys) < len(self._layers):
            ys.append(ys[-1] + step)
        return ys

    def _targets(self):
        """Row (0 = top) of each layer that will be made."""
        active = {key for key, check in self._checks if self._active(check)}
        checked_keys = {key for key, _check in self._checks}
        rows = {}
        for key, _name in self._layers:
            if key in active or key not in checked_keys:
                rows[key] = len(rows)
        return rows

    # --- updates ----------------------------------------------------------

    def refresh(self, *args):
        del args
        targets = self._targets()
        if targets == self._to:
            self.update()
            return
        current = self._current_rows()
        self._from = {key: current.get(key, row) for key, row in targets.items()}
        self._to = targets
        self._animation.stop()
        self._progress = 0.0
        self._animation.start()

    def _animate(self, value):
        self._progress = float(value)
        self.update()

    def _current_rows(self):
        p = self._progress
        return {
            key: self._from.get(key, row) + (row - self._from.get(key, row)) * p
            for key, row in self._to.items()
        }

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.Type.Move, QEvent.Type.Resize,
                            QEvent.Type.EnabledChange):
            if event.type() == QEvent.Type.EnabledChange:
                self.refresh()
            else:
                self.update()
        return super().eventFilter(watched, event)

    # --- drawing ----------------------------------------------------------

    def paintEvent(self, event):
        del event
        if not self._checks:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        ys = self._row_ys()
        rows = self._current_rows()
        text_color = self.palette().color(QPalette.ColorRole.WindowText)
        muted = QColor(text_color)
        muted.setAlpha(110)
        check_rows = {key: index for index, (key, _check) in enumerate(self._checks)}
        checks = dict(self._checks)
        names = dict(self._layers)
        metrics = self.fontMetrics()
        label_x = float(max(ARROW_ZONE + LABEL_GAP, self.width() - self._text_width() - 2))

        def y_at(row):
            low = max(0, min(int(row), len(ys) - 1))
            high = min(low + 1, len(ys) - 1)
            return ys[low] + (ys[high] - ys[low]) * (row - low)

        for key, row in rows.items():
            y_end = y_at(row)
            # Layer name.
            painter.setPen(text_color if key in check_rows else muted)
            painter.drawText(
                QPointF(label_x, y_end + (metrics.ascent() - metrics.descent()) / 2.0),
                names[key],
            )
            if key not in check_rows or not self._active(checks[key]):
                continue
            start_row = check_rows[key]
            y_start = ys[start_row]
            moved = start_row != self._to.get(key)
            color = QColor(MOVED_COLOR) if moved else QColor(muted)
            pen = QPen(color, 1.4 if moved else 1.0)
            painter.setPen(pen)
            tip_x = label_x - 3
            path = QPainterPath(QPointF(1.0, y_start))
            path.cubicTo(
                QPointF(tip_x * 0.55, y_start), QPointF(tip_x * 0.45, y_end),
                QPointF(tip_x - 5.0, y_end),
            )
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(path)
            painter.setBrush(color)
            painter.drawPolygon(QPolygonF([
                QPointF(tip_x, y_end),
                QPointF(tip_x - 6.0, y_end - 3.2),
                QPointF(tip_x - 6.0, y_end + 3.2),
            ]))
        painter.end()
