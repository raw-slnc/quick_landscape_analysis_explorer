# -*- coding: utf-8 -*-
"""Japan: GSI elevation tiles, VIRTUAL SHIZUOKA, Nagano Prefecture."""

CODE = "JP"
NAME = ("Japan", "日本")

JAPAN_BBOX = (122.0, 20.0, 154.0, 46.0)
SHIZUOKA_BBOX = (137.47410694, 34.57213583, 139.17655861, 35.64595651)
NAGANO_BBOX = (137.30, 35.15, 138.90, 37.05)

_SLOW = (
    "Fetching a large area takes time.",
    "広い範囲の取得は時間がかかります。",
)
_GSI_FACTS = {
    "provider": "Geospatial Information Authority of Japan",
    "attribution": "国土地理院標高タイル",
    "service": "https://maps.gsi.go.jp/development/ichiran.html",
}
_NAGANO = ("Nagano", "長野県")

GROUPS = [
    {
        "label": (
            "GSI elevation tiles (Japan)",
            "国土地理院 標高タイル（日本）",
        ),
        "region": JAPAN_BBOX,
        "sources": [
            {
                "key": "gsi:best",
                "label": (
                    "GSI best available",
                    "国土地理院 利用可能な最高解像度",
                ),
                "facts": _GSI_FACTS,
            },
            {
                "key": "gsi:1m",
                "label": (
                    "GSI DEM1A 1 m (laser survey areas)",
                    "国土地理院 DEM1A 1 m（航空レーザ測量範囲）",
                ),
                "note": (
                    ("1 m tiles", "1 mタイル"),
                    (
                        "Laser survey areas only.",
                        "航空レーザ測量の範囲だけです。",
                    ),
                ),
                "facts": _GSI_FACTS,
            },
            {
                "key": "gsi:5m",
                "label": ("GSI DEM5A 5 m", "国土地理院 DEM5A 5 m"),
                "facts": _GSI_FACTS,
            },
            {
                "key": "gsi:10m",
                "label": ("GSI DEM10B 10 m", "国土地理院 DEM10B 10 m"),
                "facts": _GSI_FACTS,
            },
        ],
    },
    {
        "label": ("VIRTUAL SHIZUOKA (Shizuoka)", "VIRTUAL SHIZUOKA（静岡県）"),
        "region": SHIZUOKA_BBOX,
        "sources": [
            {
                "key": "vs_lp",
                "label": ("VS LP/Grid 0.5 m DTM", "VS LP/Grid 0.5 m DTM"),
                "place": ("Shizuoka", "静岡県"),
                # VS's grid leaves parts of the coast out (the point cloud of
                # the same year has them).
                "note": (
                    ("0.5 m tiles", "0.5 mタイル"),
                    _SLOW,
                    (
                        "Parts of the coast may be missing.",
                        "海岸部には欠けがある場合があります。",
                    ),
                ),
                "facts": {
                    "provider": "Shizuoka Prefecture (VIRTUAL SHIZUOKA)",
                    "dataset": "VS LP/Grid 0.5m DTM",
                    "licence": "CC BY 4.0",
                },
            },
        ],
    },
    {
        "label": ("Nagano Prefecture open data", "長野県オープンデータ"),
        "region": NAGANO_BBOX,
        "sources": [
            {
                "key": "nagano_sabo",
                "label": (
                    "Nagano Sabo R3-4 0.5 m DEM (2021-2025)",
                    "長野県 砂防課 R3-4 0.5 m DEM（2021〜2025）",
                ),
                "place": _NAGANO,
                "note": (("0.5 m tiles", "0.5 mタイル"), _SLOW),
                "facts": {
                    "provider": "Nagano Prefecture (Sabo Division)",
                    "dataset": "Nagano Sabo R3-4 0.5m DEM",
                    "service": ('https://www.geospatial.jp/ckan/dataset/r3'
                                '-4-50cmdem'),
                },
            },
            {
                "key": "nagano_rinmu",
                "label": (
                    "Nagano Rinmu 0.5 m DEM (2013-2014)",
                    "長野県 林務部 0.5 m DEM（2013〜2014）",
                ),
                "place": _NAGANO,
                "note": (("0.5 m tiles", "0.5 mタイル"), _SLOW),
                "facts": {
                    "provider": "Nagano Prefecture (Forestry Department)",
                    "dataset": "Nagano Rinmu 0.5m DEM",
                    "service": ('https://www.geospatial.jp/ckan/dataset/na'
                                'gano-dem'),
                },
            },
        ],
    },
]
