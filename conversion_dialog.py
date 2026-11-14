# -*- coding: utf-8 -*-
"""Converting a DEM without a position, all in one window.

``ConversionDialog`` reads the file, asks which coordinate system its
coordinates are in (the project's by default), how to read them (X/Y
swapped, unit) and, for a CAD drawing with several layers that have
heights, which of them make the DEM. It then converts the file and compares
the DEM with a network DEM, with the progress shown and a Stop button at
every step, and closes; the main window shows the result. ``BackgroundCall``
runs the slow steps off the GUI thread.
"""

import datetime
import os
import time

from qgis.PyQt.QtCore import Qt, QThread, QTimer, pyqtSignal
from qgis.PyQt.QtGui import QPalette
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QStyle,
    QVBoxLayout,
    QWidget,
)
from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsCsException,
    QgsProject,
    QgsRectangle,
)
from qgis.gui import QgsProjectionSelectionWidget

from .i18n import tr
from .terrain import local_convert, world_country
from .terrain.dem_loader import GSITileDEMLoader
from .ui_fonts import use_plugin_font


# Units the file's coordinates and heights can be read in (local_convert.UNITS_M).
UNIT_CHOICES = ("m", "cm", "mm")
# A converted DEM is compared with the network DEM up to this many tiles.
CONVERT_COMPARE_MAX_TILES = 100


class Stopped(Exception):
    """Raised inside a BackgroundCall when it was cancelled."""


class BackgroundCall(QThread):
    """Run ``function(call)`` in a thread; ``done(result, error)`` at the end.

    The function calls ``call.check()`` now and then to stop when cancelled,
    and ``call.report(text, fraction)`` to show its progress (a fraction
    below 0: no measure, the bar just moves).
    """

    done = pyqtSignal(object, object)
    # (step, text shown, fraction): the text can carry a count within a step.
    progress = pyqtSignal(str, str, float)

    def __init__(self, function, parent=None):
        super().__init__(parent)
        self._function = function
        self._cancelled = False
        self._last = None

    def cancel(self):
        self._cancelled = True

    def cancelled(self):
        return self._cancelled

    def check(self):
        if self._cancelled:
            raise Stopped()

    def report(self, text, fraction, step=None):
        """Show ``text`` and ``fraction``; ``step`` names the step when the
        text changes within it (a count), else the text is the step."""

        # Not more often than each percent (GDAL reports many times a second).
        shown = (text, round(fraction, 2))
        if shown != self._last:
            self._last = shown
            self.progress.emit(step or text, text, float(fraction))

    def run(self):
        try:
            result = self._function(self)
        except Exception as error:  # noqa: BLE001 - reported to the dialog
            self.done.emit(None, error)
            return
        self.done.emit(result, None)


def bbox84(crs, extent):
    """A rectangle (xmin, ymin, xmax, ymax) in WGS84 as a QgsRectangle, or
    None when it is not a place on the earth in that CRS."""

    if not crs.isValid() or not extent:
        return None
    try:
        box = QgsCoordinateTransform(
            crs,
            QgsCoordinateReferenceSystem("EPSG:4326"),
            QgsProject.instance().transformContext(),
        ).transformBoundingBox(QgsRectangle(*extent))
    except QgsCsException:
        return None
    values = (box.xMinimum(), box.yMinimum(), box.xMaximum(), box.yMaximum())
    finite = all(value == value and abs(value) != float("inf") for value in values)
    if (
        box.isEmpty()
        or not finite
        or not (-90.0 <= box.yMinimum() and box.yMaximum() <= 90.0)
        or not (-180.0 <= box.xMinimum() and box.xMaximum() <= 180.0)
    ):
        return None
    return box


def local_source_error_text(error):
    """Why a file could not become a local DEM, for the user."""

    if not isinstance(error, local_convert.LocalSourceError):
        return str(error)
    code = str(error)
    texts = {
        "cannot_open": tr("GDAL cannot open it.", "GDALで開けません。"),
        "cannot_read": tr("Its values cannot be read.", "値を読めません。"),
        "eight_bit": tr(
            "It is an 8-bit raster (256 levels), not an elevation raster.",
            "8ビット（256段階）のラスタで、標高のラスタではありません。",
        ),
        "too_large": tr("It is too large to convert.", "大きすぎて変換できません。"),
        "no_values": tr("It holds no values.", "値が入っていません。"),
        "cannot_open_cad": tr(
            "This CAD file cannot be read. For DWG, save it as DXF in the CAD "
            "software or a converter and choose the DXF.",
            "このCADファイルを読めません。DWGは、CADソフトや変換ツールでDXFに"
            "保存し直して、そのDXFを選んでください。",
        ),
        "cad_empty": tr("The drawing holds no shapes.", "図面に図形がありません。"),
        "cad_no_heights": tr(
            "No shape in the drawing (or the chosen layers) has heights (all at 0). "
            "A drawing with heights written only as text cannot be used.",
            "図面（選んだレイヤー）に高さを持つ図形がありません（すべて0）。"
            "高さが文字で書かれているだけの図面は使えません。",
        ),
        "cad_no_extent": tr("The shapes have no extent.", "図形に広がりがありません。"),
        "cad_grid_failed": tr(
            "The heights could not be put on a grid.",
            "高さを格子にできませんでした。",
        ),
        "write_failed": tr("The DEM could not be written.", "DEMを書き出せませんでした。"),
    }
    return texts.get(code, code)


class ConversionDialog(QDialog):
    """Reading, the choices, the conversion and the comparison, in one window.

    Steps shown: 1. Coordinate system (and how the coordinates are read),
    2. CAD layers for the DEM (only for a drawing with a choice: the ground
    is often spread over several layers, buildings and planned surfaces are
    left out), and last what happens after the conversion. Below them the
    progress of the step under way, with a Stop button. Stopping while
    reading closes the window; stopping the conversion or the comparison
    removes the file being made and goes back to the choices. When all is
    done the window closes and ``conversionResult()`` gives the record.
    """

    # Twice the width of a plain message, so each step reads as a row.
    _WIDTH = 840
    # Space between paragraphs of a step.
    _PARAGRAPH_GAP = 6
    # Rows of CAD layer checks (two a row) the list has room for, and the
    # margin below them; only more rows scroll.
    _LAYER_ROWS_SHOWN = 5
    # Space between the two columns of the layer list.
    _COLUMN_GAP = 24
    _LIST_MARGIN = 8
    # Each step stays on screen at least this long (ms), so a quick one is
    # still seen before the next comes or the window closes.
    _MIN_STAGE_MS = 1000
    # Width of the progress bar (left of the step's text).
    _PROGRESS_WIDTH = 200

    def __init__(self, path, crs, target, parent=None):
        super().__init__(parent)
        use_plugin_font(self)
        self.setWindowTitle(tr("Convert to a DEM", "DEMへの変換"))
        self.setMinimumWidth(self._WIDTH)
        self._path = path
        self._target = target
        self._cad = local_convert.is_cad(path)
        self._source = None
        self._heightLayers = []
        self._checks = []
        self._call = None
        self._result = None
        # The step shown and since when; steps waiting for their turn (each
        # [text, fraction]); what to do once all have been seen long enough.
        self._working = False
        self._shownStep = ""
        self._shownAt = 0.0
        self._queued = []
        self._stageTimer = QTimer(self)
        self._stageTimer.setSingleShot(True)
        self._stageTimer.timeout.connect(self._show_queued)
        self._afterShown = None
        self._waitTimer = QTimer(self)
        self._waitTimer.setSingleShot(True)
        self._waitTimer.timeout.connect(self._run_after_shown)
        layout = QVBoxLayout(self)
        self._build_header(layout)
        self._crsBox = self._build_crs_step(crs)
        layout.addWidget(self._crsBox)
        self._layersBox = self._build_layers_step()
        layout.addWidget(self._layersBox)
        self._afterBox = self._build_after_step()
        layout.addWidget(self._afterBox)
        bottom = self._build_progress()

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self
        )
        self._okButton = self._buttons.button(QDialogButtonBox.StandardButton.Ok)
        self._okButton.setText(tr("Convert", "変換する"))
        self._cancelButton = self._buttons.button(QDialogButtonBox.StandardButton.Cancel)
        self._buttons.accepted.connect(self._convert)
        self._buttons.rejected.connect(self.reject)
        # The step under way and its progress on the row of the buttons.
        bottom.addWidget(self._buttons)
        layout.addLayout(bottom)
        self._number_steps()
        self._start_reading()

    # -- building -----------------------------------------------------------

    def _paragraph(self, text, parent=None):
        label = QLabel(text, parent or self)
        label.setWordWrap(True)
        return label

    def _build_header(self, layout):
        name = QLabel(os.path.basename(self._path), self)
        name_font = name.font()
        name_font.setBold(True)
        name.setFont(name_font)
        name.setWordWrap(True)
        layout.addWidget(name)
        # Right of the line under the title: what this conversion is, and how
        # far it has been checked (with drawings made from real terrain, not
        # with real survey drawings).
        status = QLabel(
            tr(
                "Reference implementation (little checked with real drawings)",
                "参考実装（実際の図面での確認は限られています）",
            ),
            self,
        )
        status.setStyleSheet("color: {};".format(self._quiet_colour()))
        status.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        intro = self._paragraph(
            tr(
                "This file has no position. It is converted to a DEM in the steps below.",
                "このファイルには位置情報がありません。次の手順でDEMに変換します。",
            )
        )
        # Indented under the file name, like the contents of the steps.
        intro.setContentsMargins(
            self.style().pixelMetric(QStyle.PixelMetric.PM_LayoutLeftMargin), 0, 0, 0
        )
        intro_row = QHBoxLayout()
        intro_row.setContentsMargins(0, 0, 0, 0)
        intro_row.addWidget(intro, 1)
        intro_row.addWidget(status, 0, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(intro_row)
        layout.addSpacing(self._PARAGRAPH_GAP)

    def _quiet_colour(self):
        """Grey between the text and the background colour: quieter than the
        text, readable in light and dark themes."""

        text = self.palette().color(QPalette.ColorRole.WindowText)
        ground = self.palette().color(QPalette.ColorRole.Window)
        return "rgb({}, {}, {})".format(*(
            round(t * 0.6 + g * 0.4)
            for t, g in zip(
                (text.red(), text.green(), text.blue()),
                (ground.red(), ground.green(), ground.blue()),
            )
        ))

    def _build_crs_step(self, crs):
        box = QGroupBox(self)
        box_layout = QVBoxLayout(box)
        box_layout.addWidget(self._paragraph(
            tr(
                "Choose the coordinate system the file's coordinates are in. They "
                "are used as they are, so place, orientation and size follow from it.",
                "ファイルの座標を、どの座標系の値として読むかを選びます。座標はそのまま"
                "使うので、位置・向き・大きさはこの座標系で決まります。",
            ),
            box,
        ))
        box_layout.addSpacing(self._PARAGRAPH_GAP)
        box_layout.addWidget(self._paragraph(
            tr(
                "Survey drawings are usually in plane rectangular coordinates (one zone).",
                "測量の図面は、ふつう平面直角座標（いずれかの系）で描かれています。",
            ),
            box,
        ))
        box_layout.addWidget(self._paragraph(
            tr(
                "A drawing with X and Y the other way round, or in mm, is read right "
                "with the settings on the right.",
                "XとYが入れ替わった図面やmm単位の図面は、右の設定で読み方を変えます。",
            ),
            box,
        ))
        box_layout.addSpacing(self._PARAGRAPH_GAP)
        # Label and selector as wide as one column of the layer list below
        # (half the step's width), not stretched across the window.
        holder = QWidget(box)
        margin = self.style().pixelMetric(QStyle.PixelMetric.PM_LayoutLeftMargin)
        holder.setFixedWidth((self._WIDTH - 4 * margin - self._COLUMN_GAP) // 2)
        crs_row = QHBoxLayout(holder)
        crs_row.setContentsMargins(0, 0, 0, 0)
        crs_row.addWidget(QLabel(tr("Coordinate system", "座標系"), holder))
        self.crsWidget = QgsProjectionSelectionWidget(holder)
        self.crsWidget.setCrs(crs)
        self.crsWidget.crsChanged.connect(self._update_ok)
        crs_row.addWidget(self.crsWidget, 1)
        # Then how the coordinates are read (X/Y swapped, unit), side by side.
        self.swapCheck = QCheckBox(tr("Swap X and Y", "XとYを入れ替えて読む"), box)
        self.swapCheck.setToolTip(
            tr(
                "For drawings whose survey X (north) and Y (east) were drawn as x "
                "and y as they are.",
                "測量のX（北）とY（東）を、そのままx・yとして描いた図面のためのものです。",
            )
        )
        self.unitCombo = QComboBox(box)
        for unit in UNIT_CHOICES:
            self.unitCombo.addItem(unit, unit)
        self.unitCombo.setToolTip(
            tr(
                "Unit of the file's coordinates and heights.",
                "ファイルの座標と高さの単位です。",
            )
        )
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(holder)
        row.addSpacing(self._COLUMN_GAP)
        row.addWidget(self.swapCheck)
        row.addSpacing(self._COLUMN_GAP)
        row.addWidget(QLabel(tr("Unit", "単位"), box))
        row.addWidget(self.unitCombo)
        row.addStretch(1)
        box_layout.addLayout(row)
        return box

    def _build_layers_step(self):
        """The CAD layer step; its list is filled once the drawing is read."""

        box = QGroupBox(self)
        box_layout = QVBoxLayout(box)
        box_layout.addWidget(self._paragraph(
            tr(
                "Check the layers of the ground: contour lines, spot heights and "
                "the like. Leave out buildings and planned surfaces.",
                "地面の高さ（等高線・標高点など）のレイヤーを選びます。建物や計画面の"
                "レイヤーは外します。",
            ),
            box,
        ))
        box_layout.addSpacing(self._PARAGRAPH_GAP)
        self._flatLabel = self._paragraph("", box)
        self._flatLabel.setVisible(False)
        box_layout.addWidget(self._flatLabel)
        area = QScrollArea(box)
        area.setFrameShape(QFrame.Shape.NoFrame)
        area.setWidgetResizable(True)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        holder = QWidget(area)
        # Two columns, filled row by row.
        self._layerGrid = QGridLayout(holder)
        self._layerGrid.setContentsMargins(0, 0, 0, 0)
        self._layerGrid.setHorizontalSpacing(self._COLUMN_GAP)
        self._layerGrid.setVerticalSpacing(2)
        self._layerGrid.setColumnStretch(0, 1)
        self._layerGrid.setColumnStretch(1, 1)
        self._layerHolder = holder
        area.setWidget(holder)
        # No band of another colour: the rows sit on the group's own ground.
        area.viewport().setAutoFillBackground(False)
        holder.setAutoFillBackground(False)
        # Room for five rows and a margin, whatever the number of layers;
        # only more rows scroll.
        measure = QCheckBox("M")
        measure.setFont(self.font())
        row_height = measure.sizeHint().height()
        measure.deleteLater()
        rows = self._LAYER_ROWS_SHOWN
        area.setFixedHeight(
            rows * row_height + (rows - 1) * self._layerGrid.verticalSpacing()
            + self._LIST_MARGIN
        )
        box_layout.addWidget(area)
        # Check all / uncheck all at the bottom right of the list.
        all_row = QHBoxLayout()
        all_row.setContentsMargins(0, 0, 0, 0)
        all_row.addStretch(1)
        check_all = QPushButton(tr("Check all", "すべて選ぶ"), box)
        clear_all = QPushButton(tr("Uncheck all", "すべて外す"), box)
        check_all.clicked.connect(lambda: self._set_all(True))
        clear_all.clicked.connect(lambda: self._set_all(False))
        all_row.addWidget(check_all)
        all_row.addWidget(clear_all)
        box_layout.addLayout(all_row)
        # Shown for a drawing (filled when read); a raster has no layers.
        box.setVisible(self._cad)
        return box

    def _build_after_step(self):
        box = QGroupBox(self)
        box_layout = QVBoxLayout(box)
        box_layout.addWidget(self._paragraph(
            tr(
                "The DEM is saved beside the project and becomes the local DEM.",
                "変換したDEMはプロジェクトの横に保存し、ローカルDEMにします。",
            ),
            box,
        ))
        box_layout.addSpacing(self._PARAGRAPH_GAP)
        box_layout.addWidget(self._paragraph(
            tr(
                "It is then compared with a network DEM (GSI elevation tiles in Japan, "
                "AWS Terrarium elsewhere). When it is done this window closes; the "
                "agreement and notes are shown under \"Status\". A low agreement means "
                "the coordinate system or the units need checking.",
                "続けてネットワークDEM（日本は国土地理院 標高タイル、ほかは AWS Terrarium）"
                "と比べます。終わるとこの窓は閉じ、一致率と注意事項は「状態」の欄に出ます。"
                "一致率が低いときは、座標系や単位を確かめてください。",
            ),
            box,
        ))
        return box

    def _build_progress(self):
        """The progress and what is being done now, left of the buttons.

        The bar has a fixed width, so it does not move as the text changes.
        """

        row = QHBoxLayout()
        self._stageLabel = QLabel(self)
        self._progressBar = QProgressBar(self)
        self._progressBar.setTextVisible(False)
        self._progressBar.setMaximumHeight(self._stageLabel.sizeHint().height())
        self._progressBar.setFixedWidth(self._PROGRESS_WIDTH)
        row.addWidget(self._progressBar)
        row.addWidget(self._stageLabel, 1)
        row.addSpacing(self._COLUMN_GAP)
        return row

    def _number_steps(self):
        self._crsBox.setTitle(tr("1. Coordinate system", "1. 座標系"))
        step = 2
        if self._layersBox.isVisibleTo(self):
            self._layersBox.setTitle(
                tr("{}. CAD layers for the DEM", "{}. DEMにするレイヤー").format(step)
            )
            step += 1
        self._afterBox.setTitle(tr("{}. After the conversion", "{}. 変換の後").format(step))

    def _fill_layers(self, layers):
        """The drawing's CAD layers: those with heights to check, others counted."""

        self._heightLayers = [(name, count) for name, count, _flat in layers if count]
        flat_count = sum(1 for _name, count, _flat in layers if not count)
        for index, (layer, count) in enumerate(self._heightLayers):
            check = QCheckBox(
                tr("{} ({} shapes with heights)", "{}（高さのある図形 {}）").format(layer, count),
                self._layerHolder,
            )
            check.setProperty("cadLayer", layer)
            check.setChecked(True)
            check.toggled.connect(self._update_ok)
            self._layerGrid.addWidget(check, index // 2, index % 2)
            self._checks.append(check)
        self._layerGrid.setRowStretch(self._layerGrid.rowCount(), 1)
        if flat_count:
            self._flatLabel.setText(
                tr(
                    "{} layers without heights are not listed (they cannot make a DEM).",
                    "高さの無いレイヤー {} は一覧に出していません（DEMにできません）。",
                ).format(flat_count)
            )
            self._flatLabel.setVisible(True)
        # Nothing to choose with one layer (or none): the step is left out.
        self._layersBox.setVisible(len(self._heightLayers) > 1)
        self._number_steps()

    # -- values ---------------------------------------------------------------

    def crs(self):
        return self.crsWidget.crs()

    def swapAxes(self):
        return self.swapCheck.isChecked()

    def unit(self):
        return self.unitCombo.currentData()

    def chosenLayers(self):
        """The CAD layers to use (all with heights when there was no choice)."""

        if len(self._heightLayers) <= 1:
            return [name for name, _count in self._heightLayers]
        return [check.property("cadLayer") for check in self._checks if check.isChecked()]

    def conversionResult(self):
        """{"path": the converted DEM, "record": its record}, once done."""

        return self._result

    def _set_all(self, checked):
        for check in self._checks:
            check.setChecked(checked)

    def _update_ok(self, *args):
        del args
        ok = (
            self._call is None
            and self._source is not None
            and self.crs().isValid()
            and (not self._cad or bool(self.chosenLayers()))
        )
        self._okButton.setEnabled(ok)

    # -- running --------------------------------------------------------------

    def _set_working(self, working, text="", step=None):
        """While a step runs: choices greyed, Cancel is Stop, progress shown.

        A step started shows after the one before has been seen long enough;
        leaving work (done, stopped, a problem) shows at once.
        """

        self._working = working
        for box in (self._crsBox, self._layersBox):
            box.setEnabled(not working)
        self._cancelButton.setText(tr("Stop", "中止") if working else tr("Cancel", "取消"))
        self._cancelButton.setEnabled(True)
        self._update_ok()
        if working:
            self._show_progress(step or text, text, 0.0)
            return
        self._stageTimer.stop()
        self._queued = []
        self._progressBar.setVisible(False)
        self._display(text, text, 0.0)

    def _show_progress(self, step, text, fraction):
        """A step's progress; a new step waits until those before were seen."""

        if self._queued:
            if self._queued[-1][0] == step:
                self._queued[-1][1:] = [text, fraction]
            else:
                self._queued.append([step, text, fraction])
            return
        if step == self._shownStep or self._remaining_ms() <= 0:
            self._display(step, text, fraction)
            return
        self._queued.append([step, text, fraction])
        self._stageTimer.start(self._remaining_ms())

    def _show_queued(self):
        if self._queued:
            self._display(*self._queued.pop(0))
        if self._queued:
            self._stageTimer.start(self._MIN_STAGE_MS)

    def _display(self, step, text, fraction):
        if step != self._shownStep:
            self._shownStep = step
            self._shownAt = time.monotonic()
        self._stageLabel.setText(text)
        if not text:
            return
        self._progressBar.setVisible(self._working)
        if fraction < 0:
            self._progressBar.setRange(0, 0)
        else:
            self._progressBar.setRange(0, 100)
            self._progressBar.setValue(int(round(fraction * 100)))

    def _remaining_ms(self):
        if not self._shownStep:
            return 0
        return max(0, int(self._MIN_STAGE_MS - (time.monotonic() - self._shownAt) * 1000))

    def _after_shown(self, action):
        """Run ``action`` once the step shown (and any waiting) has been seen."""

        self._afterShown = action
        self._run_after_shown()

    def _run_after_shown(self):
        if self._afterShown is None:
            return
        if self._queued or self._remaining_ms() > 0:
            self._waitTimer.start(max(50, self._remaining_ms()))
            return
        action = self._afterShown
        self._afterShown = None
        action()

    def _run(self, function, finished):
        call = BackgroundCall(function, self)
        call.progress.connect(self._show_progress)
        call.done.connect(
            lambda result, error, call=call: self._call_done(call, finished, result, error)
        )
        self._call = call
        self._update_ok()
        call.start()

    def _call_done(self, call, finished, result, error):
        call.wait()
        call.deleteLater()
        self._call = None
        finished(result, error)

    def reject(self):
        """Cancel: close; while a step runs, Stop it first (see the class)."""

        if self._call is not None:
            self._call.cancel()
            self._cancelButton.setEnabled(False)
            self._stageTimer.stop()
            self._queued = []
            stopping = tr("Stopping…", "止めています…")
            self._display(stopping, stopping, 0.0)
            return
        self._afterShown = None
        super().reject()

    def _show_problem(self, text):
        self._set_working(False, text)
        self._stageLabel.setStyleSheet("color: #c0392b;")

    def _remove_target(self):
        try:
            if os.path.exists(self._target):
                os.remove(self._target)
        except OSError:
            pass

    def _start_reading(self):
        path = self._path
        cad = self._cad
        text = (
            tr("Reading the drawing…", "図面を読み込んでいます…")
            if cad
            else tr("Reading the file…", "ファイルを読み込んでいます…")
        )
        self._set_working(True, text)

        def read(call):
            call.report(text, 0.0 if cad else -1.0)
            if cad:
                return local_convert.CadDrawing(
                    path, progress=lambda fraction: call.report(text, fraction), check=call.check
                )
            return local_convert.open_raster(path)

        self._run(read, self._read_done)

    def _read_done(self, source, error):
        if isinstance(error, Stopped):
            super().reject()
            return
        if error is not None:
            self._show_problem(local_source_error_text(error))
            self._okButton.setEnabled(False)
            return
        # The reading stays on screen its second before the choices open.
        self._after_shown(lambda: self._read_shown(source))

    def _read_shown(self, source):
        if isinstance(source, local_convert.CadDrawing):
            self._fill_layers(source.layers())
            if not self._heightLayers:
                self._show_problem(
                    local_source_error_text(local_convert.LocalSourceError("cad_no_heights"))
                )
                self._okButton.setEnabled(False)
                return
        self._source = source
        self._set_working(False)
        self.adjustSize()

    def _convert(self):
        """Convert, then compare; the window closes when both are done."""

        self._stageLabel.setStyleSheet("")
        crs = self.crs()
        crs_wkt = crs.toWkt()
        unit = self.unit()
        swap = self.swapAxes()
        layers = self.chosenLayers()
        source = self._source
        target = self._target
        record = {
            "original_file": os.path.basename(self._path),
            "crs": crs.authid() or crs_wkt,
            "unit": unit,
            "swapped_xy": swap,
            "method": "the file's coordinates taken as they are, in the chosen "
            "coordinate system (nothing placed by hand)",
            "converted_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        grid_text = tr("Making the grid…", "格子にしています…")
        save_text = tr("Saving…", "保存しています…")

        def convert(call):
            call.report(grid_text, 0.0)
            if isinstance(source, local_convert.CadDrawing):
                grid = local_convert.grid_cad(
                    source, layers, check=call.check, unit=unit, swap=swap,
                    progress=lambda fraction: call.report(grid_text, fraction),
                )
            else:
                grid = local_convert.read_grid_as(source, unit, swap)
            call.check()
            call.report(save_text, -1.0)
            local_convert.write_converted(
                grid, crs_wkt, target, dict(record, source=grid.info, cell=grid.cell)
            )
            return local_convert.grid_extent(grid)

        self._set_working(True, grid_text)
        self._run(convert, lambda extent, error: self._converted(crs, extent, error))

    def _converted(self, crs, extent, error):
        if error is not None:
            self._remove_target()
            if isinstance(error, Stopped):
                self._set_working(False, tr("Stopped.", "中止しました。"))
            else:
                self._show_problem(local_source_error_text(error))
            return
        box = bbox84(crs, extent)
        if box is None:
            # Not a place on the earth: nothing to compare with.
            self._finish({"skipped": "no_place"})
            return
        found = world_country.country_at(box.center().x(), box.center().y())
        if found is not None and found[0] == "JP":
            # DEM5A, then DEM10B where there is no 5 m.
            sources = GSITileDEMLoader.TILE_SOURCES[1:]
            kind = "gsi"
        else:
            sources = GSITileDEMLoader.TERRARIUM_SOURCES[1:2]
            kind = "terrarium"
        zoom = sources[0][1]
        x0, y0 = GSITileDEMLoader._lonlat_to_tile(box.xMinimum(), box.yMaximum(), zoom)
        x1, y1 = GSITileDEMLoader._lonlat_to_tile(box.xMaximum(), box.yMinimum(), zoom)
        if (x1 - x0 + 1) * (y1 - y0 + 1) > CONVERT_COMPARE_MAX_TILES:
            self._finish({"reference": kind, "skipped": "too_wide"})
            return
        bounds = (box.xMinimum(), box.yMinimum(), box.xMaximum(), box.yMaximum())
        target = self._target
        fetch_text = tr(
            "Fetching the network DEM ({}/{})…", "ネットワークDEMを取得しています（{}/{}）…"
        )
        compare_text = tr("Comparing…", "比べています…")
        # The count of tiles changes within this one step.
        fetch_step = "fetch"

        def compare(call):
            call.report(fetch_text.format(0, "…"), 0.0, fetch_step)
            loader = GSITileDEMLoader()
            loader.fetch_for_extent(
                *bounds,
                sources=sources,
                cancel_cb=call.cancelled,
                progress_cb=lambda done, total: call.report(
                    fetch_text.format(done, total), done / max(1, total), fetch_step
                ),
            )
            call.check()
            if loader.data is None:
                return None, None
            call.report(compare_text, -1.0)
            fit = local_convert.compare_with_reference(
                target, (loader.data, loader.gt, loader.crs_wkt), check=call.check
            )
            return fit, getattr(loader, "_used_source_label", None)

        self._set_working(True, fetch_text.format(0, "…"), fetch_step)
        self._run(compare, lambda result, error: self._compared(kind, result, error))

    def _compared(self, kind, result, error):
        if isinstance(error, Stopped):
            self._remove_target()
            self._set_working(False, tr("Stopped.", "中止しました。"))
            return
        comparison = {"reference": kind}
        if error is not None:
            comparison["error"] = str(error)
        else:
            fit, used = result
            comparison.update(dataset=used, fit=fit)
            if fit is None and used is None:
                comparison["skipped"] = "no_reference"
        self._finish(comparison)

    def _finish(self, comparison):
        """Keep the comparison in the DEM's record and close."""

        record = local_convert.read_record(self._target) or {}
        record["comparison"] = comparison
        try:
            local_convert.update_record(self._target, record)
        except (OSError, RuntimeError):
            pass
        self._result = {"path": self._target, "record": record}
        # Done: the last step stays on screen its second, then the window
        # closes (nothing left to stop).
        self._cancelButton.setEnabled(False)
        self._after_shown(self.accept)
