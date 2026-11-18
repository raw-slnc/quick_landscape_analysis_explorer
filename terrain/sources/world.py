# -*- coding: utf-8 -*-
"""Global: AWS Terrain Tiles (Mapzen Terrarium).

Terrarium is chosen by tile zoom level: the pixel size depends on the
latitude and the real detail on the regional source data (about 30 m SRTM
in Japan), so no metre value is shown. The keys keep their old names
because analysis manifests store them."""

CODE = None
NAME = None

_GLOBAL = ("Global tiles", "全球のタイル")
_DETAIL = ("Detail differs by region.", "解像度は地域で差があります。")
_FACTS = {
    "provider": "AWS Terrain Tiles (Mapzen Terrarium)",
    "attribution": "Terrain Tiles: Mapzen / AWS Open Data and their data sources",
    "service": "https://registry.opendata.aws/terrain-tiles/",
}

GROUPS = [
    {
        "label": ("Global (AWS Terrarium)", "全球（AWS Terrarium）"),
        "region": None,
        "sources": [
            {"key": "terrarium:2m", "label": ("Terrarium zoom 14 (small areas)", "Terrarium ズーム14（狭い範囲向け）"),
             "note": (_GLOBAL, _DETAIL), "facts": _FACTS},
            {"key": "terrarium:5m", "label": ("Terrarium zoom 13", "Terrarium ズーム13"),
             "note": (_GLOBAL, _DETAIL), "facts": _FACTS},
            {"key": "terrarium:10m", "label": ("Terrarium zoom 12 (wide areas)", "Terrarium ズーム12（広い範囲向け）"),
             "note": (_GLOBAL, _DETAIL), "facts": _FACTS},
        ],
    },
]
