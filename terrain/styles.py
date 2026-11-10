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
