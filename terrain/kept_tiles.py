# -*- coding: utf-8 -*-
"""What the folder of kept DEM tiles (core/dem_store.py, chosen in the
advanced settings) holds, for the DEM source "QLAE folder".

The tiles are named by source, year (or zoom level) and code, so where they
lie is known from the names alone: no tile is opened.
"""

import os

from osgeo import ogr

from . import nagano_rinmu, nagano_sabo, vs_lp

# Source key -> (folder in the store, zoom level or None). GSI and
# Terrarium keep their map tiles by zoom level; the others by year (VS,
# Rhineland-Palatinate) or without one.
_FOLDERS = {
    "vs_lp": ("vs_lp", None),
    "nagano_sabo": ("nagano_sabo", None),
    "nagano_rinmu": ("nagano_rinmu", None),
    "rlp_dgm1": ("rlp_dgm1", None),
    "by_dgm1": ("by_dgm1", None),
    # Chunks on a fixed grid (remote_dem._fetch_ogc), named column_row.
    "ahn_dtm": ("ahn_dtm", None),
    "ign_mnt": ("ign_lidarhd_mnt", None),
    "gsi:1m": ("gsi_dem1a", 17),
    "gsi:5m": ("gsi_dem5a", 15),
    "gsi:10m": ("gsi_dem", 14),
    "terrarium:2m": ("terrarium", 14),
    "terrarium:5m": ("terrarium", 13),
    "terrarium:10m": ("terrarium", 12),
}
_GSI_KEYS = ("gsi:1m", "gsi:5m", "gsi:10m")
# The chunk size of the sources read by area (remote_dem: chunk_px × cell).
_CHUNK_M = {"ahn_dtm": 500.0, "ign_mnt": 1000.0}
_YEARS = ("vs_lp", "rlp_dgm1")
WEB_MERCATOR_LIMIT = 20037508.3428


def _tile_names(folder, ext):
    """Codes of the finished tiles directly in ``folder``."""
    try:
        entries = os.listdir(folder)
    except OSError:
        return set()
    return {
        name[: -len(ext)] for name in entries
        if name.endswith(ext) and ".part-" not in name and not name.startswith(".")
    }


def codes(root, key):
    """{code: [year, ...] newest first} of the kept tiles of a source key
    (no year: [None]; GSI and Terrarium: the code is "x_y" at its zoom)."""
    if key == "gsi:best":
        found = {}
        for gsi_key in _GSI_KEYS:
            found.update(codes(root, gsi_key))
        return found
    folder, zoom = _FOLDERS[key]
    base = os.path.join(root, folder)
    if zoom is not None:
        return {code: [None] for code in _tile_names(os.path.join(base, str(zoom)), ".png")}
    found = {}
    if key in _YEARS:
        try:
            years = sorted((int(name) for name in os.listdir(base) if name.isdigit()), reverse=True)
        except OSError:
            years = []
        for year in years:
            for code in _tile_names(os.path.join(base, str(year)), ".tif"):
                found.setdefault(code, []).append(year)
        return found
    return {code: [None] for code in _tile_names(base, ".tif")}


def kept_sources(root):
    """[(source key, number of tiles)] that the folder holds, in the order
    of the DEM source list; GSI "best available" when any GSI is kept."""
    if not root or not os.path.isdir(root):
        return []
    out = []
    gsi_total = 0
    for key in _FOLDERS:
        count = len(codes(root, key))
        if not count:
            continue
        if key in _GSI_KEYS:
            if not gsi_total:
                out.append(["gsi:best", 0])
            gsi_total += count
        out.append([key, count])
    for item in out:
        if item[0] == "gsi:best":
            item[1] = gsi_total
    return [tuple(item) for item in out]


def _tile_bounds(key, code):
    """(xmin, ymin, xmax, ymax) of one kept tile in footprint_epsg(key)."""
    kind = key.partition(":")[0]
    if kind in ("gsi", "terrarium"):
        zoom = _FOLDERS[key][1]
        x, y = (int(part) for part in code.split("_"))
        size = 2.0 * WEB_MERCATOR_LIMIT / (2 ** zoom)
        left = -WEB_MERCATOR_LIMIT + x * size
        top = WEB_MERCATOR_LIMIT - y * size
        return left, top - size, left + size, top
    if key == "vs_lp":
        return vs_lp.tile_bbox(code)
    if key == "nagano_sabo":
        return nagano_sabo.tile_bbox(code)
    if key == "nagano_rinmu":
        return nagano_rinmu.tile_bbox(code)
    if key in _CHUNK_M:
        column, row = (int(part) for part in code.split("_"))
        step = _CHUNK_M[key]
        return column * step, row * step, (column + 1) * step, (row + 1) * step
    east, north = (int(part) for part in code.split("_"))  # German 1 km tiles
    return east * 1000.0, north * 1000.0, east * 1000.0 + 1000.0, north * 1000.0 + 1000.0


def footprint_epsg(key):
    kind = key.partition(":")[0]
    if kind in ("gsi", "terrarium"):
        return 3857
    if kind in ("rlp_dgm1", "by_dgm1"):
        return 25832
    if key == "ahn_dtm":
        return 28992
    if key == "ign_mnt":
        return 2154
    return 6676


def footprint_wkt(root, key):
    """The area the kept tiles of a source key cover, as WKT in
    footprint_epsg(key), or "" when none are kept."""
    keys = _GSI_KEYS if key == "gsi:best" else (key,)
    boxes = ogr.Geometry(ogr.wkbMultiPolygon)
    for one in keys:
        for code in codes(root, one):
            try:
                xmin, ymin, xmax, ymax = _tile_bounds(one, code)
            except (ValueError, KeyError, IndexError):
                continue  # a file not named like a tile
            ring = ogr.Geometry(ogr.wkbLinearRing)
            for x, y in ((xmin, ymin), (xmax, ymin), (xmax, ymax), (xmin, ymax), (xmin, ymin)):
                ring.AddPoint_2D(x, y)
            box = ogr.Geometry(ogr.wkbPolygon)
            box.AddGeometry(ring)
            boxes.AddGeometry(box)
    if boxes.GetGeometryCount() == 0:
        return ""
    return boxes.UnionCascaded().ExportToWkt()
