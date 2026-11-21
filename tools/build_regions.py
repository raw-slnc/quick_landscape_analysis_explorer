# -*- coding: utf-8 -*-
"""Make the region outlines shipped with the plugin (terrain/data/regions/).

For the developer. Reads BKG's VG2500 (Verwaltungsgebiete 1:2 500 000,
GeoPackage in UTM 32N, from https://daten.gdz.bkg.bund.de/produkte/vg/vg2500/)
and writes the outline of each German state with a DEM source, simplified.
The data is under dl-de/by-2-0; README states the credit and that the
outlines were simplified.

The Netherlands: the national boundary (Landgebied) of PDOK's Bestuurlijke
Gebieden (Kadaster, from the BRK), CC BY 4.0, read from its WFS.

Run from the folder holding the plugin's folder:
    python3 -m quick_landscape_analysis_explorer.tools.build_regions \\
        DE_VG2500.gpkg YEAR
    python3 -m quick_landscape_analysis_explorer.tools.build_regions \\
        --netherlands YEAR
YEAR: the year the data was obtained (for the credit).
"""

import json
import os
import re
import sys

from osgeo import ogr

# Source key -> state name (GEN) in VG2500.
STATES = {"rlp_dgm1": "Rheinland-Pfalz", "by_dgm1": "Bayern"}
# Simplified to this many metres: an outline, not a boundary to measure.
TOLERANCE_M = 100.0


def main(path, year):
    folder = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "terrain",
        "data",
        "regions",
    )
    os.makedirs(folder, exist_ok=True)
    source = ogr.Open(path)
    layer = source.GetLayerByName("vg2500_lan")
    for key, name in STATES.items():
        shapes = [
            feature.GetGeometryRef().Clone()
            for feature in layer
            if feature.GetField("GEN") == name
        ]
        layer.ResetReading()
        outline = shapes[0]
        for shape in shapes[1:]:
            outline = outline.Union(shape)
        outline = outline.SimplifyPreserveTopology(TOLERANCE_M)
        # Whole metres are enough for an outline.
        wkt = re.sub(
            r"(\d+)\.\d+", lambda match: match.group(1), outline.ExportToWkt()
        )
        target = os.path.join(folder, key + ".json")
        with open(target, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "key": key,
                    "name": name,
                    "epsg": 25832,
                    "wkt": wkt,
                    "source": ('BKG VG2500 (Verwaltungsgebiete 1:2 500 '
                               '000), simplified'),
                    "credit": "© BKG ({}) dl-de/by-2-0".format(year),
                },
                handle,
            )
        print(
            "{}: {} vertices, {} bytes".format(
                key, wkt.count(","), os.path.getsize(target)
            )
        )


NL_WFS = (
    'https://service.pdok.nl/kadaster/bestuurlijkegebieden/wfs/v1_0?servi'
    'ce=WFS'
    '&version=2.0.0&request=GetFeature&typeNames=bestuurlijkegebieden:Lan'
    'dgebied'
    "&outputFormat=application/json&srsName=EPSG:28992"
)
NL_TOLERANCE_M = 50.0


def netherlands(year):
    import urllib.request

    folder = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "terrain",
        "data",
        "regions",
    )
    request = urllib.request.Request(
        NL_WFS, headers={"User-Agent": "QGIS plugin developer"}
    )
    with urllib.request.urlopen(  # nosec B310
        request, timeout=120
    ) as response:
        data = json.loads(response.read().decode("utf-8"))
    outline = ogr.CreateGeometryFromJson(
        json.dumps(data["features"][0]["geometry"])
    )
    outline = outline.SimplifyPreserveTopology(NL_TOLERANCE_M)
    wkt = re.sub(
        r"(\d+)\.\d+", lambda match: match.group(1), outline.ExportToWkt()
    )
    target = os.path.join(folder, "ahn_dtm.json")
    with open(target, "w", encoding="utf-8") as handle:
        json.dump(
            {
                "key": "ahn_dtm",
                "name": "Nederland",
                "epsg": 28992,
                "wkt": wkt,
                "source": ('PDOK Bestuurlijke Gebieden, Landgebied '
                           '(Kadaster, BRK), simplified'),
                "credit": ('Bestuurlijke Gebieden: Kadaster '
                           '(PDOK), CC BY 4.0 ({})').format(
                    year
                ),
            },
            handle,
        )
    print(
        "ahn_dtm: {} vertices, {} bytes".format(
            wkt.count(","), os.path.getsize(target)
        )
    )


if __name__ == "__main__":
    if sys.argv[1] == "--netherlands":
        netherlands(sys.argv[2])
    else:
        main(sys.argv[1], sys.argv[2])
