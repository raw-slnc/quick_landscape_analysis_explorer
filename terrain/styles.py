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
    """Catchment polygons: no fill, outline as Morizon Next's catchment results.

    Morizon Next (processes/raster_styler/aggregate.py) draws the catchment
    aggregation polygons with a 0.08 mm dark navy outline.
    """

    qml = """<!DOCTYPE qgis PUBLIC 'http://mrcc.com/qgis.dtd' 'SYSTEM'>
<qgis version="3.34" styleCategories="Symbology">
  <renderer-v2 type="singleSymbol" symbollevels="0" enableorderby="0" forceraster="0">
    <symbols>
      <symbol type="fill" name="0" alpha="1" clip_to_extent="1" force_rhr="0">
        <layer class="SimpleFill" enabled="1" locked="0" pass="0">
          <Option type="Map">
            <Option type="QString" name="color" value="0,0,0,0"/>
            <Option type="QString" name="style" value="no"/>
            <Option type="QString" name="outline_color" value="31,78,156,255"/>
            <Option type="QString" name="outline_style" value="solid"/>
            <Option type="QString" name="outline_width" value="0.08"/>
            <Option type="QString" name="outline_width_unit" value="MM"/>
            <Option type="QString" name="joinstyle" value="round"/>
          </Option>
        </layer>
      </symbol>
    </symbols>
  </renderer-v2>
  <blendMode>0</blendMode>
  <featureBlendMode>0</featureBlendMode>
  <layerGeometryType>2</layerGeometryType>
</qgis>
"""
    Path(path).write_text(qml, encoding="utf-8")
    return str(path)


def write_contour_qml(path):
    """Contours styled like the user's GSI basic-map contour layer.

    Same as the project's "等高線-20250701～20261001" style: dark brown,
    10 m lines (10, 30, 50, ...) very thin, 20 m lines (20, 40, ...) thin,
    100 m lines thick, each with its own largest visible scale, and the layer
    drawn at 60 % opacity. Built with the QGIS API and saved by QGIS itself,
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
    layer.setOpacity(0.6)
    document = QDomDocument()
    layer.exportNamedStyle(document)
    Path(path).write_text(document.toString(), encoding="utf-8")
    return str(path)
