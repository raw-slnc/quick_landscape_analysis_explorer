# -*- coding: utf-8 -*-
"""The Netherlands: AHN4 (PDOK)."""

CODE = "NL"
NAME = ("Netherlands", "オランダ")

NETHERLANDS_BBOX = (3.2, 50.7, 7.3, 53.6)

# The regional analysis: the DEM in the colours of PDOK's AHN map
# (netherlands_nap.py).
OPTION = {
    "key": "nap_colouring",
    "label": ("NAP colouring", "段彩（NAP基準）"),
    "tooltip": (
        "Adds layers from Dutch open data (PDOK): the DEM in NAP colours, the "
        "slope on a local scale (prototype), the object height (trees and "
        "buildings), the BRO geomorphological map, the DSM, the DEM with its "
        "holes filled and orthophotos for the base map. They are off at first.",
        "オランダの公開データ（PDOK）のレイヤーを足します：段彩（NAP基準）、"
        "傾斜（範囲階調・試作）、地物の高さ（樹木・建物）、BRO 地形分類図、DSM、"
        "DEM（穴埋め）、ベースマップ用のオルソフォト。最初はオフです。",
    ),
    "route": "netherlands_nap",
    # The DEM for the GeoPDF starts from the DEM with its holes filled from
    # the ground around them (AHN has no heights under buildings, water and
    # dense trees), before the steps every region shares.
    "geopdf_fill_holes": True,
}

GROUPS = [
    {
        "label": ("Netherlands open data", "オランダ オープンデータ"),
        "region": NETHERLANDS_BBOX,
        "sources": [
            {
                "key": "ahn_dtm",
                "label": ("AHN4 DTM 0.5 m", "AHN4 DTM 0.5 m"),
                "note": (("0.5 m", "0.5 m"),
                         ("The PDOK service can be slow.", "PDOKのサービスは遅いことがあります。")),
                "facts": {
                    "provider": "Rijkswaterstaat / PDOK",
                    "dataset": "AHN4 DTM 0.5m",
                    # AHN4 was surveyed over these years (the service's own
                    # description); the year of a place is not given.
                    "survey_years": [2020, 2021, 2022],
                    "licence": "CC0",
                    "service": "https://service.pdok.nl/rws/ahn/wcs/v1_0",
                },
                # The whole country has data: its outline is the mesh. The
                # outline is PDOK's national boundary (terrain/data/regions/
                # ahn_dtm.json, tools/build_regions.py): QGIS's world map is
                # too coarse at the borders (it left out the Vaalserberg).
                "coverage": {"whole_country": "NL"},
                "outline": ("file",),
            },
        ],
    },
]
