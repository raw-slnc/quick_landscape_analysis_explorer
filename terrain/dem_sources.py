# -*- coding: utf-8 -*-
"""Catalogue of DEM sources offered in the DEM section.

Keys are stored in analysis manifests and window state, so keep them stable.
Labels are (English, Japanese); the dialog picks one with ``tr``.
Regions are WGS84 (lon_min, lat_min, lon_max, lat_max); ``None`` = anywhere.
"""

JAPAN_BBOX = (122.0, 20.0, 154.0, 46.0)
SHIZUOKA_BBOX = (137.47410694, 34.57213583, 139.17655861, 35.64595651)
NAGANO_BBOX = (137.30, 35.15, 138.90, 37.05)
NETHERLANDS_BBOX = (3.2, 50.7, 7.3, 53.6)
FRANCE_BBOX = (-5.5, 41.2, 9.8, 51.2)
NEW_ZEALAND_BBOX = (166.0, -47.5, 179.0, -34.0)

# (group label, region, [(key, label)])
GROUPS = [
    (("Local", "ローカル"), None, [
        ("local", ("Local DEM file", "ローカルDEMファイル")),
    ]),
    (("GSI elevation tiles (Japan)", "国土地理院 標高タイル（日本）"), JAPAN_BBOX, [
        ("gsi:best", ("GSI best available", "国土地理院 利用可能な最高解像度")),
        ("gsi:1m", ("GSI DEM1A 1 m (laser survey areas)", "国土地理院 DEM1A 1 m（航空レーザ測量範囲）")),
        ("gsi:5m", ("GSI DEM5A 5 m", "国土地理院 DEM5A 5 m")),
        ("gsi:10m", ("GSI DEM10B 10 m", "国土地理院 DEM10B 10 m")),
    ]),
    (("VIRTUAL SHIZUOKA (Shizuoka)", "VIRTUAL SHIZUOKA（静岡県）"), SHIZUOKA_BBOX, [
        ("vs_lp", ("VS LP/Grid 0.5 m DTM", "VS LP/Grid 0.5 m DTM")),
    ]),
    (("Nagano Prefecture open data", "長野県オープンデータ"), NAGANO_BBOX, [
        ("nagano_sabo", ("Nagano Sabo R3-4 0.5 m DEM (2021-2025)", "長野県 砂防課 R3-4 0.5 m DEM（2021〜2025）")),
        ("nagano_rinmu", ("Nagano Rinmu 0.5 m DEM (2013-2014)", "長野県 林務部 0.5 m DEM（2013〜2014）")),
    ]),
    (("Netherlands open data", "オランダ オープンデータ"), NETHERLANDS_BBOX, [
        ("ahn_dtm", ("AHN4 DTM 0.5 m", "AHN4 DTM 0.5 m")),
    ]),
    (("France open data", "フランス オープンデータ"), FRANCE_BBOX, [
        ("ign_mnt", ("IGN LiDAR HD MNT 0.5 m", "IGN LiDAR HD MNT 0.5 m")),
    ]),
    (("New Zealand open data", "ニュージーランド オープンデータ"), NEW_ZEALAND_BBOX, [
        ("linz_dem", ("LINZ DEM 1 m", "LINZ DEM 1 m")),
    ]),
    (("Global (AWS Terrarium)", "全球（AWS Terrarium）"), None, [
        ("terrarium:2m", ("Terrarium ~2 m (small areas)", "Terrarium 約2 m（狭い範囲向け）")),
        ("terrarium:5m", ("Terrarium ~5 m", "Terrarium 約5 m")),
        ("terrarium:10m", ("Terrarium ~10 m (wide areas)", "Terrarium 約10 m（広い範囲向け）")),
    ]),
]

DEFAULT_KEY = "gsi:best"


def region_for(key):
    for _group, region, items in GROUPS:
        if any(item_key == key for item_key, _label in items):
            return region
    return None


def overlaps(region, bbox84):
    """Whether a WGS84 bbox touches a region (None region = everywhere)."""
    if region is None or bbox84 is None:
        return True
    return not (
        bbox84[2] < region[0] or bbox84[0] > region[2]
        or bbox84[3] < region[1] or bbox84[1] > region[3]
    )


def key_from_legacy(kind, dataset=None):
    """Map older state/manifest values (kind + GSI dataset) to a key."""
    if kind == "gsi":
        return "gsi:" + (dataset or "best")
    if kind == "terrarium":
        return "terrarium:" + (dataset or "5m")
    if kind == "local":
        return "local"
    return kind or DEFAULT_KEY
