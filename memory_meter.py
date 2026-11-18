# -*- coding: utf-8 -*-
"""Memory meter shown at the right of the Status heading while an analysis runs.

The bar is the PC's whole memory; filled is what is used now (other
programs lighter, then QGIS darker), read every second. The line is where
the use is expected to reach at the analysis's peak: set when the analysis
starts and again before its heaviest step. Where the line goes past what
may be used (the use now and the share of the free memory the plugin's
check allows), the bar runs on in red, past the whole memory if need be,
and the shortage is written beside it. The meter only shows; the questions
before the analysis and before that step stop it when memory would run
short.
"""

from qgis.PyQt.QtCore import QEvent, QObject, QRectF, Qt, QTimer
from qgis.PyQt.QtGui import QColor, QPainter, QPalette, QPen
from qgis.PyQt.QtWidgets import QWidget

from .core import memory
from .i18n import tr

BAR_WIDTH = 120
REFRESH_MS = 1000


def _gb(value):
    return "{:.1f}".format(value / 1e9)


class MemoryMeter(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._total = None
        self._used = None
        self._available = None
        self._own = None
        self._line = None
        self._timer = QTimer(self)
        self._timer.setInterval(REFRESH_MS)
        self._timer.timeout.connect(self._refresh)
        self.hide()

    # Analysis start and end.

    def start(self, need):
        """Show the meter; ``need`` bytes are expected on top of the use now."""
        self._refresh(update=False)
        self.set_need(need)
        if self._total is None:
            return  # memory cannot be read here: no meter
        self._fit()
        self.show()
        self._timer.start()

    def set_need(self, need):
        """Move the line to the use now plus ``need`` bytes."""
        self._refresh(update=False)
        if need is None or self._used is None:
            self._line = None
        else:
            self._line = self._used + float(need)
        self._update_tooltip()
        self.update()

    def stop(self):
        self._timer.stop()
        self._line = None
        self.hide()

    # Reading and drawing.

    def _refresh(self, update=True):
        system = memory.system_memory()
        if system is None:
            self._total = self._used = self._available = None
        else:
            self._total, self._available = system
            self._used = max(0, self._total - self._available)
        self._own = memory.process_memory()
        if update:
            self._update_tooltip()
            self.update()

    def _shortage(self):
        """Bytes the line goes past what may be used (the use now and the
        share of the free memory the plugin's check allows), or 0."""
        if self._line is None or self._used is None:
            return 0.0
        limit = self._used + self._available * memory.SAFE_SHARE_OF_FREE
        return max(0.0, self._line - limit)

    def _texts(self):
        """(memory text, shortage text or "")."""
        if self._total is None:
            return "", ""
        text = tr("Memory {} / {} GB", "メモリ {} / {} GB").format(
            _gb(self._used), _gb(self._total))
        shortage = self._shortage()
        short_text = tr("about {} GB short", "約{} GB不足").format(_gb(shortage)) if shortage else ""
        return text, short_text

    def _fit(self):
        metrics = self.fontMetrics()
        sample = (tr("Memory {} / {} GB", "メモリ {} / {} GB").format("88.8", "888.8") + "  "
                  + tr("about {} GB short", "約{} GB不足").format("888.8"))
        self.setFixedSize(metrics.horizontalAdvance(sample) + 8 + BAR_WIDTH, metrics.height())

    def _colors(self):
        """The bar's colours: (frame, other programs, QGIS, red). Other
        programs are drawn half see-through; given here as they look over
        the window colour, for the tooltip's legend."""
        palette = self.palette()
        ink = palette.color(QPalette.ColorRole.WindowText)
        frame = QColor(ink)
        frame.setAlpha(140)
        highlight = palette.color(QPalette.ColorRole.Highlight)
        others = QColor(highlight)
        others.setAlpha(90)
        return frame, others, highlight, QColor(220, 30, 30)

    def _update_tooltip(self):
        if self._total is None:
            self.setToolTip("")
            return
        frame, others, qgis, red = self._colors()
        window = self.palette().color(QPalette.ColorRole.Window)

        def opaque(color):
            # A colour drawn over the window colour, as it looks there.
            alpha = color.alphaF()
            return QColor(*(
                int(round(part * alpha + back * (1.0 - alpha)))
                for part, back in zip(color.getRgb()[:3], window.getRgb()[:3])
            ))

        def row(mark, color, text):
            # No wrapping: a rich-text tooltip is otherwise wrapped at a
            # width Qt picks, which breaks the longer lines.
            return ('<tr><td style="color:{}; white-space:nowrap;">{}</td>'
                    '<td style="white-space:nowrap;">{}</td></tr>').format(
                opaque(color).name(), mark, text)

        rows = [row("□", frame, tr("Whole memory: {} GB", "メモリ全体：{} GB").format(
            _gb(self._total)))]
        if self._own is not None:
            rows.append(row("■", others, tr("Other programs: {} GB", "ほかのプログラム：{} GB").format(
                _gb(max(0.0, self._used - self._own)))))
            rows.append(row("■", qgis, tr("QGIS: {} GB", "QGIS：{} GB").format(_gb(self._own))))
        else:
            rows.append(row("■", others, tr("Used now: {} GB", "使用中：{} GB").format(
                _gb(self._used))))
        if self._line is not None:
            rows.append(row("┃", frame, tr(
                "Expected at the analysis's peak: {} GB",
                "解析で一番多く使うときの見込み：{} GB",
            ).format(_gb(self._line))))
        shortage = self._shortage()
        if shortage:
            rows.append(row("■", red, tr("Short: about {} GB", "不足：約{} GB").format(
                _gb(shortage))))
        self.setToolTip('<table cellspacing="0" cellpadding="1">{}</table>'.format("".join(rows)))

    def paintEvent(self, event):  # noqa: N802 - Qt name
        if self._total is None:
            return
        painter = QPainter(self)
        palette = self.palette()
        red = QColor(220, 30, 30)
        # Over the group box's frame line: paint the window colour first.
        painter.fillRect(self.rect(), palette.color(QPalette.ColorRole.Window))
        area = QRectF(self.width() - BAR_WIDTH, 2, BAR_WIDTH - 1, self.height() - 5)
        text, short_text = self._texts()
        ink = palette.color(QPalette.ColorRole.WindowText)
        text_right = area.left() - 6
        if short_text:
            width = self.fontMetrics().horizontalAdvance(short_text)
            painter.setPen(red)
            painter.drawText(QRectF(text_right - width, 0, width, self.height()),
                             Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, short_text)
            text_right -= width + self.fontMetrics().horizontalAdvance("  ")
        painter.setPen(ink)
        painter.drawText(QRectF(0, 0, text_right, self.height()),
                         Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, text)

        # The scale: the whole memory, or the line when it goes past it (the
        # whole memory's frame then shrinks and the red runs on after it).
        span = max(float(self._total), self._line or 0.0)
        scale = area.width() / span
        bar = QRectF(area.left(), area.top(), self._total * scale, area.height())
        # The whole memory: a faint ground and a frame in the text colour (the
        # theme's Base and Mid can be the same as the heading's background).
        ground = QColor(ink)
        ground.setAlpha(25)
        frame, others, highlight, red = self._colors()
        painter.fillRect(bar, ground)
        used_w = min(bar.width(), self._used * scale)
        painter.fillRect(QRectF(bar.left(), bar.top(), used_w, bar.height()), others)
        if self._own is not None:
            # QGIS last, next to the line: it is what grows in an analysis.
            own_w = min(used_w, self._own * scale)
            painter.fillRect(
                QRectF(bar.left() + used_w - own_w, bar.top(), own_w, bar.height()), highlight)
        shortage = self._shortage()
        if shortage:
            # Red: from what may be used to the line, past the frame if so.
            painter.fillRect(QRectF(area.left() + (self._line - shortage) * scale, area.top(),
                                    shortage * scale, area.height()), red)
        painter.setPen(QPen(frame, 1))
        painter.drawRect(bar)
        if self._line is not None:
            # A mark of the expected use, not a warning.
            x = area.left() + self._line * scale
            painter.setPen(QPen(frame, 2))
            painter.drawLine(int(round(x)), 0, int(round(x)), self.height())
        painter.end()


class _HeadingPlacer(QObject):
    """Keeps the meter at the right end of the group box's heading."""

    def __init__(self, box, meter):
        super().__init__(box)
        self._box = box
        self._meter = meter
        box.installEventFilter(self)
        meter.installEventFilter(self)

    def eventFilter(self, watched, event):  # noqa: N802 - Qt name
        if event.type() in (QEvent.Type.Resize, QEvent.Type.Show):
            self.place()
        return False

    def place(self):
        self._meter.move(self._box.width() - self._meter.width() - 10, 0)
        self._meter.raise_()


def add_to_heading(box):
    """A memory meter placed in ``box``'s heading row, at the right."""
    meter = MemoryMeter(box)
    meter._placer = _HeadingPlacer(box, meter)
    return meter
