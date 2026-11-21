# -*- coding: utf-8 -*-
"""New Zealand: LINZ DEM (1 m, on AWS)."""

CODE = "NZ"
NAME = ("New Zealand", "ニュージーランド")

NEW_ZEALAND_BBOX = (166.0, -47.5, 179.0, -34.0)

GROUPS = [
    {
        "label": ("New Zealand open data", "ニュージーランド オープンデータ"),
        "region": NEW_ZEALAND_BBOX,
        "sources": [
            {
                "key": "linz_dem",
                "label": ("LINZ DEM 1 m", "LINZ DEM 1 m"),
                "note": (
                    ("1 m", "1 m"),
                    (
                        "The AWS copy may lag LINZ releases.",
                        "AWSの複製は更新が遅れることがあります。",
                    ),
                ),
                "facts": {
                    "provider": ('Toitū Te Whenua Land Information New '
                                 'Zealand and the survey licensors'),
                    "dataset": "LINZ NZ DEM 1m",
                    "licence": "CC BY 4.0",
                    "service": ('https://nz-elevation.s3.ap-southeast-2.am'
                                'azonaws.com/new-zealand/new-zealand/dem_1'
                                'm/2193/'),
                },
                # The Topo50 sheets (24 km × 36 km) in NZTM2000.
                "coverage": {
                    "epsg": 2193,
                    "cell_w": 24000.0,
                    "cell_h": 36000.0,
                    "origin": (988000.0, 6234000.0),
                    "check_days": 30,
                    "ask_first": False,
                },
                "outline": ("country", "NZ", (165.0, -48.0, 179.5, -34.0)),
            },
        ],
    },
]
