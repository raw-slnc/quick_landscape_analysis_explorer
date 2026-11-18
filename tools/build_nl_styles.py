# -*- coding: utf-8 -*-
"""Make the style of the Dutch geomorphological map shipped with the plugin
(terrain/data/styles/nl_geomorphology.qml) from the official SLD that
comes in PDOK's download of the BRO Geomorfologische kaart (CC0), set to
multiply so it can lie over the terrain maps, with labels of the landforms'
names set up (off).

For the developer. Needs QGIS's Python. Give the unpacked download's
GeoPackage and SLD:
    python3 -m quick_landscape_analysis_explorer.tools.build_nl_styles \\
        bro-geomorfologischekaart.gpkg BRO-GMM-GeomorfologischeKaart_V2025-01.sld
"""

import os
import sys

LABEL_FIELD = "landform_subgroup_description"


def add_labels(layer):
    """Labels with the landform's name (the legend's names): 9 pt, a 2.5 pt
    white buffer; set up but off, a user turns them on."""
    from qgis.core import (
        Qgis,
        QgsPalLayerSettings,
        QgsTextFormat,
        QgsVectorLayerSimpleLabeling,
    )
    from qgis.PyQt.QtGui import QColor

    settings = QgsPalLayerSettings()
    settings.fieldName = LABEL_FIELD
    text_format = QgsTextFormat()
    text_format.setSize(9)
    text_format.setSizeUnit(Qgis.RenderUnit.Points)
    buffer = text_format.buffer()
    buffer.setEnabled(True)
    buffer.setSize(2.5)
    buffer.setSizeUnit(Qgis.RenderUnit.Points)
    buffer.setColor(QColor(255, 255, 255))
    text_format.setBuffer(buffer)
    settings.setFormat(text_format)
    layer.setLabeling(QgsVectorLayerSimpleLabeling(settings))
    layer.setLabelsEnabled(False)


def main(gpkg, sld):
    from qgis.core import QgsApplication, QgsVectorLayer
    from qgis.PyQt.QtGui import QPainter

    app = QgsApplication([], False)
    app.initQgis()
    layer = QgsVectorLayer(gpkg + "|layername=geomorphological_area", "gmm", "ogr")
    message, ok = layer.loadSldStyle(sld)
    if not ok:
        raise SystemExit("The SLD could not be read: {}".format(message))
    layer.setBlendMode(QPainter.CompositionMode.CompositionMode_Multiply)
    layer.setOpacity(0.8)
    add_labels(layer)
    target = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "terrain", "data", "styles", "nl_geomorphology.qml")
    os.makedirs(os.path.dirname(target), exist_ok=True)
    layer.saveNamedStyle(target)
    print("{}: {} rules, {} bytes".format(
        target, len(layer.renderer().rootRule().children()), os.path.getsize(target)))
    # The layer goes before QGIS shuts down (else Python crashes on exit).
    del layer
    app.exitQgis()


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
