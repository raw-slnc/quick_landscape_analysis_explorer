# -*- coding: utf-8 -*-
"""The mesh over an area on the preview (a local DEM, the QLAE folder's
tiles, a network source's coverage): a thin outline and diagonal lines.

The lines are made here and cut to the area, not drawn with a pattern fill:
a pattern fill on a rubber band drew the holes of an area (France's gaps)
filled, twice over. Only the part in view is made, so it stays light; it is
made again shortly after the view changes, keeping the spacing in pixels.
"""

import math

from qgis.core import (
    Qgis,
    QgsCoordinateTransform,
    QgsCsException,
    QgsGeometry,
    QgsLineSymbol,
    QgsPointXY,
    QgsProject,
    QgsSimpleLineSymbolLayer,
)
from qgis.gui import QgsRubberBand
from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtGui import QColor

# Lines this far apart and this wide, in screen pixels.
SPACING_PX = 12
LINE_PX = 0.6
OUTLINE_PX = 1.0
COLOR_LINES = QColor(30, 90, 200, 150)
COLOR_OUTLINE = QColor(30, 90, 200, 170)
# The outline of a source's region: darker and dashed, apart from the mesh.
COLOR_REGION = QColor(20, 45, 120, 220)
REGION_PX = 1.4


def _line_symbol(color, width, dashed=False):
    layer = QgsSimpleLineSymbolLayer(color, width)
    layer.setWidthUnit(Qgis.RenderUnit.Pixels)
    if dashed:
        layer.setPenStyle(Qt.PenStyle.DashLine)
    return QgsLineSymbol([layer])


def region_outline(canvas):
    """A rubber band for a source's region: a dark dashed line."""
    band = QgsRubberBand(canvas, Qgis.GeometryType.Line)
    band.setSymbol(_line_symbol(COLOR_REGION, REGION_PX, dashed=True))
    return band


class MeshOverlay:
    def __init__(self, canvas):
        self._canvas = canvas
        self._lines = QgsRubberBand(canvas, Qgis.GeometryType.Line)
        self._lines.setSymbol(_line_symbol(COLOR_LINES, LINE_PX))
        self._outline = QgsRubberBand(canvas, Qgis.GeometryType.Line)
        self._outline.setSymbol(_line_symbol(COLOR_OUTLINE, OUTLINE_PX))
        self._geometry = None
        self._crs = None
        self._timer = QTimer(canvas)
        self._timer.setSingleShot(True)
        self._timer.setInterval(100)
        self._timer.timeout.connect(self.redraw)
        canvas.extentsChanged.connect(self._timer.start)

    # The rubber band's names, so the window uses it as before.

    def reset(self, *args):
        del args
        self._geometry = None
        self._crs = None
        self._lines.reset(Qgis.GeometryType.Line)
        self._outline.reset(Qgis.GeometryType.Line)

    def setToGeometry(self, geometry, crs):  # noqa: N802 - as QgsRubberBand
        self._geometry = QgsGeometry(geometry)
        self._crs = crs
        self.redraw()

    def asGeometry(self):  # noqa: N802 - as QgsRubberBand
        return (
            QgsGeometry(self._geometry)
            if self._geometry is not None
            else QgsGeometry()
        )

    def redraw(self):
        self._lines.reset(Qgis.GeometryType.Line)
        self._outline.reset(Qgis.GeometryType.Line)
        if self._geometry is None or self._geometry.isEmpty():
            return
        canvas_crs = self._canvas.mapSettings().destinationCrs()
        area = QgsGeometry(self._geometry)
        try:
            if (
                self._crs is not None
                and self._crs.isValid()
                and self._crs != canvas_crs
            ):
                area.transform(
                    QgsCoordinateTransform(
                        self._crs,
                        canvas_crs,
                        QgsProject.instance().transformContext(),
                    )
                )
        except QgsCsException:
            return
        extent = self._canvas.extent()
        unit = self._canvas.mapUnitsPerPixel()
        if extent.isEmpty() or unit <= 0:
            return
        # The part in view, with a margin so its edge is not drawn as outline.
        view = extent.buffered(unit * SPACING_PX * 2)
        area = area.clipped(view)
        if area.isEmpty():
            return
        self._outline.setToGeometry(QgsGeometry(area.constGet().boundary()))
        self._lines.setToGeometry(
            self._hatch(view, unit * SPACING_PX).intersection(area)
        )

    @staticmethod
    def _hatch(box, step):
        """Lines at 45° and 135° across ``box``, ``step`` map units apart
        (measured across the lines), on a fixed grid so they do not slide
        as the view pans."""
        offset_step = step * math.sqrt(2.0)
        lines = []
        # 45°: y = x + c; 135°: y = -x + c.
        for sign in (1.0, -1.0):
            corners = [
                box.yMinimum() - sign * box.xMinimum(),
                box.yMaximum() - sign * box.xMinimum(),
                box.yMinimum() - sign * box.xMaximum(),
                box.yMaximum() - sign * box.xMaximum(),
            ]
            first = math.floor(min(corners) / offset_step)
            last = math.ceil(max(corners) / offset_step)
            for index in range(first, last + 1):
                c = index * offset_step
                lines.append(
                    [
                        QgsPointXY(box.xMinimum(), sign * box.xMinimum() + c),
                        QgsPointXY(box.xMaximum(), sign * box.xMaximum() + c),
                    ]
                )
        return QgsGeometry.fromMultiPolylineXY(lines)
