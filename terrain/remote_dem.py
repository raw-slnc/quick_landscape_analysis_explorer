# -*- coding: utf-8 -*-
# Adapted from Forestry Operations Lite (same author), dem_worker.py.
"""Open overseas DEMs fetched for an extent (no login required).

Netherlands AHN4 DTM (PDOK WCS), France IGN LiDAR HD MNT (Géoplateforme WMS-R),
New Zealand LINZ 1 m DEM (Cloud Optimised GeoTIFFs on AWS) and the 1 m DGM1 of
two German states, Rhineland-Palatinate and Bavaria (1 km GeoTIFF tiles).

``ctx`` provides ``report(text)``, ``check()`` (raises when cancelled) and
``cancelled()``. Files are written below ``out_dir``, which the caller owns.
"""

import datetime
import gzip
import json
import math
import os
import re
import string
import urllib.error
import urllib.request

import numpy as np

_USER_AGENT = "Quick Landscape Analysis Explorer (QGIS plugin)"

_AHN_WCS = ("https://service.pdok.nl/rws/ahn/wcs/v1_0?service=WCS&request=GetCoverage"
            "&version=2.0.1&coverageId={layer}&subset=x({x0},{x1})&subset=y({y0},{y1})"
            "&format=image/tiff")
_IGN_WMSR = ("https://data.geopf.fr/wms-r/wms?SERVICE=WMS&VERSION=1.3.0&REQUEST=GetMap"
             "&LAYERS={layer}&STYLES=&CRS=EPSG:2154&BBOX={x0},{y0},{x1},{y1}"
             "&WIDTH={w}&HEIGHT={h}&FORMAT=image/geotiff")
_NZ_BASE = "https://nz-elevation.s3.ap-southeast-2.amazonaws.com/new-zealand/new-zealand/dem_1m/2193/"
# German state DGM1: 1 km tiles in ETRS89 / UTM 32N named by their lower-left
# corner in km. Rhineland-Palatinate puts the survey year in the file name, so
# its file list is read first; Bavaria's names have no year.
_RLP_DGM1 = "https://www.geobasis-rlp.de/data/dgm1/current/tif/"
_BY_DGM1 = "https://download1.bayernwolke.de/a/dgm/dgm1/"

# Source key -> how to fetch it.
REMOTE_SOURCES = {
    "ahn_dtm": dict(
        kind="ogc", url=_AHN_WCS, layer="dtm_05m", epsg=28992, cell=0.5, chunk_px=1000,
        label="AHN4 DTM 0.5m", prefix="ahn_dtm",
        provider="Rijkswaterstaat / PDOK", licence="CC0",
        service="https://service.pdok.nl/rws/ahn/wcs/v1_0",
    ),
    "ign_mnt": dict(
        kind="ogc", url=_IGN_WMSR,
        layer="IGNF_LIDAR-HD_MNT_ELEVATION.ELEVATIONGRIDCOVERAGE.LAMB93",
        epsg=2154, cell=0.5, chunk_px=2000,
        label="IGN LiDAR HD MNT 0.5m", prefix="ign_lidarhd_mnt",
        provider="IGN (Institut national de l'information géographique et forestière)",
        licence="Licence Ouverte / Open Licence (Etalab 2.0)",
        service="https://data.geopf.fr/wms-r/wms",
    ),
    "linz_dem": dict(
        kind="nzcog", epsg=2193, cell=1.0,
        label="LINZ NZ DEM 1m", prefix="linz_dem",
        provider="Toitū Te Whenua Land Information New Zealand and the survey licensors",
        licence="CC BY 4.0",
        service=_NZ_BASE,
    ),
    "rlp_dgm1": dict(
        kind="de_tiles", state="rlp", epsg=25832, cell=1.0,
        label="Rhineland-Palatinate DGM1 1m", prefix="rlp_dgm1",
        provider="GeoBasis-RLP (Landesamt für Vermessung und Geobasisinformation Rheinland-Pfalz)",
        licence="dl-de/by-2-0",
        # The form LVermGeo RLP asks for (lvermgeo.rlp.de, Open Data); the
        # year is the year the data was obtained. The plugin changes the data
        # (clipped, regridded, derived maps), so "[Daten bearbeitet]".
        credit="©GeoBasis-DE / LVermGeoRP {year}, dl-de/by-2-0, www.lvermgeo.rlp.de "
               "[Daten bearbeitet]",
        service=_RLP_DGM1,
    ),
    "by_dgm1": dict(
        kind="de_tiles", state="by", epsg=25832, cell=1.0,
        label="Bavaria DGM1 1m", prefix="by_dgm1",
        provider="Bayerische Vermessungsverwaltung (geodaten.bayern.de)",
        licence="CC BY 4.0",
        # The form of the Bavarian terms of use, with the licence and the
        # statement that the data was changed, which the terms ask for.
        credit="Bayerische Vermessungsverwaltung – www.geodaten.bayern.de, CC BY 4.0 "
               "(Daten verändert)",
        service=_BY_DGM1,
    ),
}

LERC_MISSING_MESSAGE = (
    "This QGIS's GDAL cannot read LERC-compressed GeoTIFFs, which the LINZ data uses. "
    "Use a QGIS build whose GDAL supports LERC.")


class RemoteDemError(Exception):
    """A message for the user."""


def _http_download(ctx, url, dest, timeout=180, accept=("tif",)):
    """Save url to dest, checking for cancellation every 64 KB."""
    if not url.startswith("https://"):
        raise RemoteDemError(f"Invalid URL: {url}")
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r, open(dest, "wb") as f:  # nosec B310
        ctype = r.headers.get("Content-Type", "")
        while True:
            ctx.check()
            chunk = r.read(65536)
            if not chunk:
                break
            f.write(chunk)
    if not any(a in ctype.lower() for a in accept):
        try:
            with open(dest, "rb") as f:
                head = f.read(300).decode("utf-8", "replace")
        except OSError:
            head = ""
        raise RemoteDemError(f"The service returned an error instead of elevation data: {head}")


def snap_bbox(bbox, cell):
    """Grow bbox outwards onto the cell grid."""
    x0, y0, x1, y1 = bbox
    return (math.floor(x0 / cell) * cell, math.floor(y0 / cell) * cell,
            math.ceil(x1 / cell) * cell, math.ceil(y1 / cell) * cell)


def check_size(bbox, cell):
    """Refuse empty extents; return the grid size at this cell size.

    There is no upper limit: the dialog asks before large analyses
    (dialog._confirm_dem_size).
    """
    x0, y0, x1, y1 = snap_bbox(bbox, cell)
    w_px = int(round((x1 - x0) / cell))
    h_px = int(round((y1 - y0) / cell))
    if w_px <= 0 or h_px <= 0:
        raise RemoteDemError("The analysis area is empty.")
    return w_px, h_px


def _finish_mosaic(ctx, sources, out_dir, prefix, epsg, bbox=None):
    """Combine sources into one GeoTIFF; all NoData means outside coverage.

    The CRS is set from epsg: IGN's WMS-R GeoTIFFs carry an incomplete
    definition without an EPSG code.
    """
    from osgeo import gdal

    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    tif_path = os.path.join(out_dir, f"{prefix}_{ts}.tif")
    vrt = gdal.BuildVRT("", sources)
    if vrt is None:
        raise RemoteDemError("Could not combine the downloaded tiles.")

    def _cb(complete, _msg, _data):
        ctx.report(f"Reading elevation data… {int(complete * 100)}%")
        return 0 if ctx.cancelled() else 1

    opts = dict(format="GTiff", creationOptions=["COMPRESS=DEFLATE", "TILED=YES", "BIGTIFF=IF_SAFER"],
                outputSRS=f"EPSG:{epsg}", callback=_cb)
    if bbox is not None:
        x0, y0, x1, y1 = bbox
        opts.update(projWin=[x0, y1, x1, y0])
    out = gdal.Translate(tif_path, vrt, **opts)
    vrt = None
    ctx.check()
    if out is None:
        raise RemoteDemError("Could not write the elevation GeoTIFF.")
    band = out.GetRasterBand(1)
    nodata = band.GetNoDataValue()
    arr = band.ReadAsArray()
    out = None
    valid = np.isfinite(arr)
    if nodata is not None:
        valid &= arr != nodata
    # AHN uses the float32 maximum as NoData on its 0.5 m grid.
    valid &= np.abs(arr) < 1e30
    if not valid.any():
        raise RemoteDemError("No elevation data for this area (outside the source coverage).")
    return tif_path


def _fetch_ogc(ctx, spec, bbox, out_dir):
    """Query WCS / WMS in chunks and combine them into one GeoTIFF."""
    cell = spec["cell"]
    check_size(bbox, cell)
    x0, y0, x1, y1 = snap_bbox(bbox, cell)
    step = spec["chunk_px"] * cell
    xs = []
    x = x0
    while x < x1 - 1e-9:
        xs.append((x, min(x + step, x1)))
        x += step
    ys = []
    y = y0
    while y < y1 - 1e-9:
        ys.append((y, min(y + step, y1)))
        y += step
    parts_dir = os.path.join(out_dir, f"parts_{spec['prefix']}")
    os.makedirs(parts_dir, exist_ok=True)
    paths = []
    n = len(xs) * len(ys)
    k = 0
    for cy0, cy1 in ys:
        for cx0, cx1 in xs:
            ctx.check()
            k += 1
            ctx.report(f"Fetching {spec['label']}… {k}/{n}")
            url = spec["url"].format(
                layer=spec["layer"], x0=cx0, y0=cy0, x1=cx1, y1=cy1,
                w=int(round((cx1 - cx0) / cell)), h=int(round((cy1 - cy0) / cell)))
            dest = os.path.join(parts_dir, f"part_{k:04d}.tif")
            _http_download(ctx, url, dest)
            paths.append(dest)
    ctx.report("Combining…")
    return _finish_mosaic(ctx, paths, out_dir, spec["prefix"], spec["epsg"])


_TOPO50_LETTERS = "".join(c for c in string.ascii_uppercase if c not in "IO")


def _nz_tile_names(bbox):
    """Topo50 sheet names (e.g. BP35) touching an NZTM2000 (EPSG:2193) bbox.

    Sheets are 24 km × 36 km. Column = (xmin - 988000) / 24000; the row is the
    two-letter order without I and O, counted from AS, with
    ymax = 6234000 - row × 36000.
    """
    x0, y0, x1, y1 = bbox
    c0 = int(math.floor((x0 - 988000) / 24000))
    c1 = int(math.floor((x1 - 988000 - 1e-6) / 24000))
    r0 = int(math.floor((6234000 - y1) / 36000))
    r1 = int(math.floor((6234000 - y0 - 1e-6) / 36000))
    base = _TOPO50_LETTERS.index("A") * 24 + _TOPO50_LETTERS.index("S")
    names = []
    for r in range(r0, r1 + 1):
        code = base + r
        if code < 0:
            continue
        a, b = divmod(code, 24)
        if a >= len(_TOPO50_LETTERS):
            continue
        for c in range(c0, c1 + 1):
            if 0 <= c <= 99:
                names.append(f"{_TOPO50_LETTERS[a]}{_TOPO50_LETTERS[b]}{c:02d}")
    return names


def gdal_has_lerc():
    """Whether this GDAL reads LERC-compressed GeoTIFFs (LINZ COGs use LERC).

    Some builds lack LERC (for example the Flatpak QGIS).
    """
    from osgeo import gdal
    drv = gdal.GetDriverByName("GTiff")
    opts = drv.GetMetadataItem("DMD_CREATIONOPTIONLIST") if drv is not None else None
    return bool(opts) and "<Value>LERC</Value>" in opts


def _nz_available_tiles(ctx):
    """Tile names that exist, from the nationwide 1 m DEM collection.json."""
    ctx.report("Checking LINZ tile index…")
    req = urllib.request.Request(_NZ_BASE + "collection.json",
                                 headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as r:  # nosec B310
        col = json.loads(r.read().decode("utf-8"))
    return {link["href"][2:-5] for link in col.get("links", [])
            if link.get("rel") == "item" and link.get("href", "").startswith("./")}


def _fetch_nzcog(ctx, spec, bbox, out_dir):
    """Read only the needed parts of the LINZ COG tiles and combine them."""
    from osgeo import gdal
    cell = spec["cell"]
    check_size(bbox, cell)
    bbox = snap_bbox(bbox, cell)
    if not gdal_has_lerc():
        raise RemoteDemError(LERC_MISSING_MESSAGE)
    wanted = _nz_tile_names(bbox)
    available = _nz_available_tiles(ctx)
    tiles = [n for n in wanted if n in available]
    if not tiles:
        raise RemoteDemError("No elevation data for this area (outside the source coverage).")
    ctx.check()
    # Partial reads of COGs on S3 (settings for this thread only).
    gdal.SetThreadLocalConfigOption("GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR")
    gdal.SetThreadLocalConfigOption("CPL_VSIL_CURL_ALLOWED_EXTENSIONS", ".tiff,.tif")
    try:
        sources = [f"/vsicurl/{_NZ_BASE}{n}.tiff" for n in tiles]
        ctx.report(f"Reading {spec['label']} ({len(tiles)} tile(s))…")
        return _finish_mosaic(ctx, sources, out_dir, spec["prefix"], spec["epsg"], bbox=bbox)
    finally:
        gdal.SetThreadLocalConfigOption("GDAL_DISABLE_READDIR_ON_OPEN", None)
        gdal.SetThreadLocalConfigOption("CPL_VSIL_CURL_ALLOWED_EXTENSIONS", None)


def _de_tile_keys(bbox):
    """(east km, north km) of the 1 km tiles touching an EPSG:25832 bbox."""
    x0, y0, x1, y1 = bbox
    return [
        (east, north)
        for north in range(int(math.floor(y0 / 1000)), int(math.floor((y1 - 1e-6) / 1000)) + 1)
        for east in range(int(math.floor(x0 / 1000)), int(math.floor((x1 - 1e-6) / 1000)) + 1)
    ]


def _rlp_years(ctx):
    """{(east km, north km): [year, ...] newest first} from the RLP file list.

    The list is an HTML page of about 11 MB (about 0.5 MB sent compressed).
    """
    ctx.report("Checking the Rhineland-Palatinate tile list…")
    req = urllib.request.Request(
        _RLP_DGM1, headers={"User-Agent": _USER_AGENT, "Accept-Encoding": "gzip"}
    )
    with urllib.request.urlopen(req, timeout=120) as r:  # nosec B310
        data = r.read()
        if r.headers.get("Content-Encoding", "").lower() == "gzip":
            data = gzip.decompress(data)
    ctx.check()
    years = {}
    for east, north, year in set(re.findall(
        r"dgm1_32_(\d+)_(\d+)_1_rp_(\d{4})\.tif", data.decode("utf-8", "replace")
    )):
        years.setdefault((int(east), int(north)), []).append(int(year))
    for tile_years in years.values():
        tile_years.sort(reverse=True)
    return years


def _tile_coverage(path):
    """Share of cells holding values in a downloaded tile."""
    from osgeo import gdal
    dataset = gdal.Open(path)
    if dataset is None:
        return 0.0
    band = dataset.GetRasterBand(1)
    arr = band.ReadAsArray()
    nodata = band.GetNoDataValue()
    dataset = None
    valid = np.isfinite(arr) & (np.abs(arr) < 1e30)
    if nodata is not None:
        valid &= arr != nodata
    return float(valid.mean()) if arr.size else 0.0


def _normalised_tile(path, epsg, cell):
    """The tile as a VRT on one grid: CRS, cell edges and NoData made the same.

    The tiles of one state differ by year: RLP's 2022 tiles are 1001 × 1001
    cells shifted by half a cell with the CRS set, its 2026 tiles 1000 × 1000
    without a CRS, and the NoData values differ. Unlike tiles left as they are
    would be dropped or misplaced when combined.
    """
    from osgeo import gdal
    source = gdal.Open(path)
    nodata = source.GetRasterBand(1).GetNoDataValue() if source is not None else None
    source = None
    vrt_path = os.path.splitext(path)[0] + "_grid.vrt"
    out = gdal.Warp(
        vrt_path, path, format="VRT",
        srcSRS=f"EPSG:{epsg}", dstSRS=f"EPSG:{epsg}",
        srcNodata=nodata, dstNodata=-9999.0,
        xRes=cell, yRes=cell, targetAlignedPixels=True,
        resampleAlg="bilinear", outputType=gdal.GDT_Float32,
    )
    if out is None:
        raise RemoteDemError("Could not combine the downloaded tiles.")
    out = None
    return vrt_path


def _fetch_de_tiles(ctx, spec, bbox, out_dir):
    """Download the German state's 1 km DGM1 tiles and combine them."""
    cell = spec["cell"]
    check_size(bbox, cell)
    bbox = snap_bbox(bbox, cell)
    keys = _de_tile_keys(bbox)
    if spec["state"] == "rlp":
        years = _rlp_years(ctx)
        wanted = {key: years[key] for key in keys if key in years}

        def urls(key):
            east, north = key
            return [f"{_RLP_DGM1}dgm1_32_{east}_{north}_1_rp_{year}.tif" for year in wanted[key]]
    else:
        wanted = {key: [None] for key in keys}

        def urls(key):
            east, north = key
            return [f"{_BY_DGM1}{east}_{north}.tif"]
    if not wanted:
        raise RemoteDemError("No elevation data for this area (outside the source coverage).")
    parts_dir = os.path.join(out_dir, f"parts_{spec['prefix']}")
    os.makedirs(parts_dir, exist_ok=True)
    layered = []  # (priority, vrt path): the newest survey drawn last, on top
    survey_years = set()
    total = len(wanted)
    for index, key in enumerate(sorted(wanted), start=1):
        ctx.check()
        ctx.report(f"Fetching {spec['label']}… {index}/{total}")
        # The newest survey first; older ones only fill what it leaves empty.
        for rank, url in enumerate(urls(key)):
            dest = os.path.join(parts_dir, url.rsplit("/", 1)[1])
            try:
                _http_download(ctx, url, dest)
            except urllib.error.HTTPError as error:
                if error.code == 404:  # no tile here (outside the state)
                    break
                raise
            layered.append((rank, _normalised_tile(dest, spec["epsg"], cell)))
            if wanted[key][rank] is not None:
                survey_years.add(wanted[key][rank])
            if _tile_coverage(dest) >= 0.999:
                break
    if not layered:
        raise RemoteDemError("No elevation data for this area (outside the source coverage).")
    # Later sources of a VRT are drawn over earlier ones: oldest first.
    sources = [path for _rank, path in sorted(layered, key=lambda item: -item[0])]
    ctx.report("Combining…")
    path = _finish_mosaic(ctx, sources, out_dir, spec["prefix"], spec["epsg"], bbox=bbox)
    info = {"credit": spec["credit"].format(year=datetime.date.today().year)}
    if survey_years:  # Rhineland-Palatinate: the survey years in the file names
        info["survey_years"] = sorted(survey_years)
    return path, info


def fetch_remote_dem(ctx, key, bbox, out_dir):
    """Fetch an overseas DEM for bbox (in the source CRS).

    Returns (GeoTIFF path, extra source facts for the analysis record): the
    German states give their required credit line and, for
    Rhineland-Palatinate, the survey years used; the others give none.
    """
    spec = REMOTE_SOURCES[key]
    os.makedirs(out_dir, exist_ok=True)
    if spec["kind"] == "nzcog":
        return _fetch_nzcog(ctx, spec, bbox, out_dir), {}
    if spec["kind"] == "de_tiles":
        return _fetch_de_tiles(ctx, spec, bbox, out_dir)
    return _fetch_ogc(ctx, spec, bbox, out_dir), {}
