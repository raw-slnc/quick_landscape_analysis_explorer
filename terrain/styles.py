# -*- coding: utf-8 -*-
"""Portable QGIS styles bundled with each terrain-analysis package."""

from pathlib import Path


SLOPE_COLORS = (
    "#6cabd0",
    "#b5dee4",
    "#ecf7c9",
    "#ffdf99",
    "#feb367",
    "#f08856",
    "#d74043",
)


def write_morizon_slope_qml(path):
    """Write the non-scoring appearance of Morizon Next's 災害リスク/傾斜."""

    items = (
        (20, "&lt;= 20", SLOPE_COLORS[0]),
        (25, "20 - 25", SLOPE_COLORS[1]),
        (30, "25 - 30", SLOPE_COLORS[2]),
        (35, "30 - 35", SLOPE_COLORS[3]),
        (40, "35 - 40", SLOPE_COLORS[4]),
        (45, "40 - 45", SLOPE_COLORS[5]),
        ("inf", "&gt; 45", SLOPE_COLORS[6]),
    )
    item_xml = "\n".join(
        '          <item color="{}" label="{}" value="{}" alpha="255"/>'.format(
            color, label, value
        )
        for value, label, color in items
    )
    qml = """<!DOCTYPE qgis PUBLIC 'http://mrcc.com/qgis.dtd' 'SYSTEM'>
<qgis version="3.34" styleCategories="AllStyleCategories">
  <pipe>
    <rasterrenderer alphaBand="-1" band="1" type="singlebandpseudocolor" opacity="0.65" classificationMin="0" classificationMax="90">
      <rasterTransparency/>
      <minMaxOrigin>
        <limits>MinMax</limits><extent>WholeRaster</extent><statAccuracy>Estimated</statAccuracy>
        <cumulativeCutLower>0.02</cumulativeCutLower><cumulativeCutUpper>0.98</cumulativeCutUpper><stdDevFactor>2</stdDevFactor>
      </minMaxOrigin>
      <rastershader>
        <colorrampshader labelPrecision="4" colorRampType="DISCRETE" classificationMode="1" maximumValue="90" minimumValue="0" clip="0">
{items}
        </colorrampshader>
      </rastershader>
    </rasterrenderer>
    <brightnesscontrast contrast="0" gamma="1" brightness="0"/>
    <huesaturation grayscaleMode="0" colorizeStrength="100" saturation="0" colorizeOn="0"/>
    <rasterresampler maxOversampling="2"/>
    <resamplingStage>resamplingFilter</resamplingStage>
  </pipe>
  <blendMode>0</blendMode>
</qgis>
""".format(items=item_xml)
    Path(path).write_text(qml, encoding="utf-8")
    return str(path)


def write_catchment_qml(path):
    """Catchment polygons: no fill, a purple dashed boundary on a white halo.

    Catchment boundaries run along ridges, so they are kept apart from water
    (blue), the dark brown contours and the slope colours: purple, dashed
    (3 mm dash, 1 mm gap), 0.4 mm wide, wider than the 100 m contours. A
    white line underneath (opaque for whole catchments) keeps them readable
    on dark hillshade or CS-map areas. Sizes are in mm, so they follow the
    paper.

    Each boundary line is drawn once, from the lines a catchment keeps in
    "edges_wkt" (see terrain/watershed.py): drawing every polygon outline
    would put two dashed lines, out of step, on each shared boundary. A
    boundary next to a whole catchment belongs to it; catchments cut by the
    edge of the fetched DEM (complete = 0) draw their own lines thinner and
    fainter. The frame edge is not a catchment boundary and is not drawn.
    """
    from qgis.core import (
        QgsFillSymbol,
        QgsGeometryGeneratorSymbolLayer,
        QgsLineSymbol,
        QgsRuleBasedRenderer,
        QgsSimpleLineSymbolLayer,
        QgsSymbol,
        QgsVectorLayer,
    )
    from qgis.PyQt.QtXml import QDomDocument

    def symbol(width, alpha, halo_width, halo_alpha):
        halo = QgsSimpleLineSymbolLayer.create({
            "line_color": "255,255,255,{}".format(halo_alpha),
            "line_width": halo_width,
            "line_width_unit": "MM",
            "capstyle": "round",
            "joinstyle": "round",
        })
        line = QgsSimpleLineSymbolLayer.create({
            "line_color": "120,40,140,{}".format(alpha),
            "line_width": width,
            "line_width_unit": "MM",
            "use_custom_dash": "1",
            "customdash": "3;1",
            "customdash_unit": "MM",
            "capstyle": "flat",
            "joinstyle": "round",
        })
        generator = QgsGeometryGeneratorSymbolLayer.create({
            "geometryModifier": 'geom_from_wkt("edges_wkt")',
            "SymbolType": "Line",
        })
        generator.setSymbolType(QgsSymbol.SymbolType.Line)
        generator.setSubSymbol(QgsLineSymbol([halo, line]))
        return QgsFillSymbol([generator])

    root = QgsRuleBasedRenderer.Rule(None)
    # (rule, label, line width, line alpha, white width, white alpha)
    for expression, label, width, alpha, halo_width, halo_alpha in (
        ('"complete" = 1', "whole", "0.4", 255, "0.8", 255),
        ('"complete" = 0', "cut by the DEM edge", "0.25", 140, "0.6", 70),
    ):
        root.appendChild(QgsRuleBasedRenderer.Rule(
            symbol(width, alpha, halo_width, halo_alpha), filterExp=expression, label=label
        ))
    layer = QgsVectorLayer(
        "MultiPolygon?field=basin_id:integer&field=complete:integer&field=edges_wkt:string",
        "c", "memory",
    )
    layer.setRenderer(QgsRuleBasedRenderer(root))
    document = QDomDocument()
    layer.exportNamedStyle(document)
    Path(path).write_text(document.toString(), encoding="utf-8")
    return str(path)


def write_contour_qml(path):
    """Contours styled like the user's GSI basic-map contour layer.

    Same as the project's "20160701-等高線" style: dark brown, 10 m lines
    (10, 30, 50, ...) very thin, 20 m lines (20, 40, ...) thin, 100 m lines
    thick, each with its own largest visible scale, and the layer drawn at
    full opacity. Built with the QGIS API and saved by QGIS itself,
    so the rule-based style is always in a form this QGIS reads back.
    """
    from qgis.core import QgsLineSymbol, QgsRuleBasedRenderer, QgsVectorLayer
    from qgis.PyQt.QtXml import QDomDocument

    layer = QgsVectorLayer("MultiLineString?field=elev:double&field=index:integer", "c", "memory")
    root = QgsRuleBasedRenderer.Rule(None)
    # (filter, width mm, label, smallest scale denominator shown)
    for expression, width, label, max_denominator in (
        ('"elev" % 20 = 10', "0.03", "10m", 70000),
        ('"elev" % 20 = 0 AND "elev" % 100 <> 0', "0.1", "20m", 40001),
        ('"elev" % 100 = 0', "0.23", "100m", 50001),
    ):
        symbol = QgsLineSymbol.createSimple({
            "line_color": "58,28,0,255",
            "line_width": width,
            "line_width_unit": "MM",
            "capstyle": "square",
            "joinstyle": "bevel",
        })
        rule = QgsRuleBasedRenderer.Rule(symbol, filterExp=expression, label=label)
        # QGIS: "maximum scale" is the most zoomed-in, "minimum" the most
        # zoomed-out scale at which the rule is drawn.
        rule.setMinimumScale(max_denominator)
        rule.setMaximumScale(1)
        root.appendChild(rule)
    layer.setRenderer(QgsRuleBasedRenderer(root))
    layer.setOpacity(1.0)
    document = QDomDocument()
    layer.exportNamedStyle(document)
    Path(path).write_text(document.toString(), encoding="utf-8")
    return str(path)
