# -*- coding: utf-8 -*-
"""GeoTIFF writers.

解析結果の保存モジュール
  save_raster          : numpy配列 → GeoTIFF
  save_rgba_raster     : RGBA配列 → GeoTIFF
  mask_to_polygons     : バイナリマスク → ポリゴン GPKG
  values_to_points     : 閾値超セル → ポイント GPKG
  mask_to_centroids    : 連結成分ごとの重心 → ポイント GPKG

  overwrite=True のとき固定ファイル名で上書き保存。
  overwrite=False（デフォルト）のときタイムスタンプ付きで追記保存。
"""

import math
import os
import numpy as np
from datetime import datetime

try:
    from osgeo import gdal

    gdal.UseExceptions()
    HAS_GDAL = True
except ImportError:
    HAS_GDAL = False

NODATA = -9999.0


def _ts():
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _resolve_path(out_dir, name, ext, overwrite):
    """overwrite=True → 固定名、False → タイムスタンプ付き"""
    if overwrite:
        return os.path.join(out_dir, f"{name}{ext}")
    return os.path.join(out_dir, f"{name}_{_ts()}{ext}")


def save_raster(data, gt, crs_wkt, out_dir, name_prefix, overwrite=False):
    """numpy配列をGeoTIFF(LZW圧縮)として保存。保存パスを返す"""
    if not HAS_GDAL:
        raise RuntimeError("GDAL is not available")
    os.makedirs(out_dir, exist_ok=True)
    path = _resolve_path(out_dir, name_prefix, ".tif", overwrite)
    if overwrite and os.path.exists(path):
        os.remove(path)
    rows, cols = data.shape
    drv = gdal.GetDriverByName("GTiff")
    ds = drv.Create(
        path,
        cols,
        rows,
        1,
        gdal.GDT_Float32,
        options=["COMPRESS=LZW", "TILED=YES"],
    )
    ds.SetGeoTransform(gt)
    ds.SetProjection(crs_wkt)
    arr = data.astype(np.float32)
    arr[np.isnan(arr)] = NODATA
    band = ds.GetRasterBand(1)
    band.WriteArray(arr)
    band.SetNoDataValue(NODATA)
    ds.FlushCache()
    ds = None
    return path


# Metres in a degree of latitude (for cell sizes in a geographic CRS).
METRES_PER_DEGREE = 111320.0
# The DEM for use with the GeoPDF (in the analysis ZIP and the export).
GEOPDF_DEM_NAME = "dem_for_geopdf.tif"
# Cells added around it, so that it covers the frame with a margin.
GEOPDF_DEM_MARGIN_CELLS = 4
# Cells along its longer side at most, as in Avid Maps' field map, which
# fetches its own DEMs up to this size (its 3D view has at most 255 x 255
# points); a finer DEM over a wide area is averaged to coarser cells.
GEOPDF_DEM_MAX_EDGE = 1024


def _srs_with_epsg(crs_wkt):
    """SpatialReference from WKT; from its EPSG code when it has one, so the
    GeoTIFF gets the EPSG code in its keys."""
    from osgeo import osr

    srs = osr.SpatialReference()
    srs.ImportFromWkt(crs_wkt)
    try:
        srs.AutoIdentifyEPSG()
    except RuntimeError:
        pass
    node = "PROJCS" if srs.IsProjected() else "GEOGCS"
    if srs.GetAuthorityName(node) == "EPSG" and srs.GetAuthorityCode(node):
        coded = osr.SpatialReference()
        coded.ImportFromEPSG(int(srs.GetAuthorityCode(node)))
        return coded
    return srs


def save_geopdf_dem(
    dem_path, out_path, crs_wkt, bounds=None, cell=None, fill=None, check=None
):
    """Write the DEM in the GeoPDF's CRS (the preview's) to ``out_path``.

    The GeoPDF is in the preview's CRS, while the analysis DEM is in the
    analysis grid's (for example UTM for GSI tiles). Apps that combine a
    GeoPDF with a DEM (such as Avid Maps' field map) want the same CRS with
    an EPSG code, an uncompressed single-band GeoTIFF, and a DEM covering the
    whole GeoPDF. ``bounds`` (xmin, ymin, xmax, ymax in that CRS; longitude
    first in a geographic one) is the GeoPDF's area, the preview; without it,
    the DEM's own area. A few cells are added around it. The cell size is
    ``cell`` metres, or the DEM's, in the CRS's units (degrees in a
    geographic CRS); coarser when the longer side would have more than
    GEOPDF_DEM_MAX_EDGE cells.
    fill: "gsi" (Japan) or "terrarium" (elsewhere) gives every cell a height
    from those tiles (network; see dem_gaps); None fills only the holes
    surrounded by data. Heights already in the DEM are kept as they are
    (below 0 m too: land below sea level, or Terrarium's sea floor).
    check: called between the tiles fetched (raises to cancel).
    """
    if not HAS_GDAL:
        raise RuntimeError("GDAL is not available")
    from osgeo import osr

    target_srs = _srs_with_epsg(crs_wkt)
    target = target_srs.ExportToWkt()
    source = gdal.Open(dem_path)
    if cell is None:
        # The DEM's cell in metres.
        cell = abs(source.GetGeoTransform()[5])
        source_srs = osr.SpatialReference()
        source_srs.ImportFromWkt(source.GetProjection())
        if source_srs.IsGeographic():
            cell *= METRES_PER_DEGREE
        elif source_srs.IsProjected():
            cell *= source_srs.GetLinearUnits()
    if bounds is None:
        # The DEM's area in the new CRS.
        warped = gdal.Warp("", source, format="VRT", dstSRS=target)
        wgt = warped.GetGeoTransform()
        xmin, ymax = wgt[0], wgt[3]
        xmax = xmin + warped.RasterXSize * wgt[1]
        ymin = ymax + warped.RasterYSize * wgt[5]
        warped = None
    else:
        xmin, ymin, xmax, ymax = bounds
    # The cell in the CRS's units: degrees in a geographic CRS (a degree of
    # longitude is shorter away from the equator), else its linear unit.
    if target_srs.IsGeographic():
        y_res = cell / METRES_PER_DEGREE
        x_res = y_res / max(0.01, math.cos(math.radians((ymin + ymax) / 2.0)))
    else:
        x_res = y_res = cell / (target_srs.GetLinearUnits() or 1.0)
    # At most GEOPDF_DEM_MAX_EDGE cells along the longer side: averaged to
    # coarser cells over a wide area (a 3D view in the field uses no finer).
    resample = "bilinear"
    longest = max((xmax - xmin) / x_res, (ymax - ymin) / y_res)
    room = (
        GEOPDF_DEM_MAX_EDGE - 2 * GEOPDF_DEM_MARGIN_CELLS - 2
    )  # margins, rounding
    if longest > room:
        scale = longest / room
        x_res *= scale
        y_res *= scale
        resample = "average"
    x_margin = x_res * GEOPDF_DEM_MARGIN_CELLS
    y_margin = y_res * GEOPDF_DEM_MARGIN_CELLS
    if os.path.exists(out_path):
        # gdal.Warp would warp into the old file instead of replacing it.
        os.remove(out_path)
    result = gdal.Warp(
        out_path,
        source,
        format="GTiff",
        dstSRS=target,
        outputBounds=(
            xmin - x_margin,
            ymin - y_margin,
            xmax + x_margin,
            ymax + y_margin,
        ),
        xRes=x_res,
        yRes=y_res,
        resampleAlg=resample,
        srcNodata=NODATA,
        dstNodata=NODATA,
        outputType=gdal.GDT_Float32,
        creationOptions=["COMPRESS=NONE", "TILED=NO", "BIGTIFF=NO"],
    )
    source = None
    if result is None:
        raise RuntimeError("cannot write {}".format(out_path))
    result = None
    # Cells without a height are drawn at a made-up height in 3D (spikes,
    # walls). With ``fill`` they are all given one (dem_gaps: in Japan from
    # GSI data, the sea 0 m, land from DEM5A/10B, water from its banks;
    # elsewhere from Terrarium as it is). Without it, or if
    # that fails, holes inside the data are filled from around them and the
    # area outside the data (reaching the edge) is left without data.
    if fill:
        from . import dem_gaps

        try:
            dem_gaps.fill_for_3d(out_path, check, source=fill)
            return out_path
        except dem_gaps.FillIncomplete:
            # Not made: the caller tells why (no network, too wide).
            try:
                os.remove(out_path)
            except OSError:
                pass
            raise
        # cancel goes on; else the simple fill
        except Exception as error:  # noqa: BLE001
            if type(error).__name__ in ("TerrainJobCancelled", "Cancelled"):
                raise
    from . import watershed

    watershed.fill_enclosed_holes(out_path, near_holes=True)
    return out_path


def covers(path, crs_wkt, bounds=None):
    """True if the raster at ``path`` is in the CRS given as WKT and its
    area contains ``bounds`` (xmin, ymin, xmax, ymax; not checked if None)."""
    if not HAS_GDAL or not os.path.exists(path):
        return False
    from osgeo import osr

    dataset = gdal.Open(path)
    if dataset is None:
        return False
    own = osr.SpatialReference()
    own.ImportFromWkt(dataset.GetProjection())
    gt = dataset.GetGeoTransform()
    columns, rows = dataset.RasterXSize, dataset.RasterYSize
    dataset = None
    if not own.IsSame(_srs_with_epsg(crs_wkt)):
        return False
    if bounds is None:
        return True
    xmin, ymax = gt[0], gt[3]
    xmax, ymin = xmin + columns * gt[1], ymax + rows * gt[5]
    slack = abs(gt[1])
    return (
        xmin - slack <= bounds[0]
        and ymin - slack <= bounds[1]
        and bounds[2] <= xmax + slack
        and bounds[3] <= ymax + slack
    )


def save_rgba_raster(data, gt, crs_wkt, out_dir, name_prefix, overwrite=False):
    """RGBA/RGB配列をByte GeoTIFFとして保存。保存パスを返す"""
    if not HAS_GDAL:
        raise RuntimeError("GDAL is not available")
    if data.ndim != 3 or data.shape[2] not in (3, 4):
        raise ValueError("RGBA raster must have shape (rows, cols, 3|4)")
    os.makedirs(out_dir, exist_ok=True)
    path = _resolve_path(out_dir, name_prefix, ".tif", overwrite)
    if overwrite and os.path.exists(path):
        os.remove(path)
    rows, cols, bands = data.shape
    drv = gdal.GetDriverByName("GTiff")
    create_opts = [
        "COMPRESS=LZW",
        "TILED=YES",
        "PHOTOMETRIC=RGB",
        "BIGTIFF=IF_NEEDED",
    ]
    if bands == 4:
        # 4帯目を非乗算アルファとしてタグ付け（EXTRASAMPLES=2）。
        # これがないと GDAL/QGIS がただの余分な帯として扱い透過しない。
        create_opts.append("ALPHA=YES")
    ds = drv.Create(
        path, cols, rows, bands, gdal.GDT_Byte, options=create_opts
    )
    ds.SetGeoTransform(gt)
    ds.SetProjection(crs_wkt)
    arr = data.astype(np.uint8, copy=False)
    color_interp = [gdal.GCI_RedBand, gdal.GCI_GreenBand, gdal.GCI_BlueBand]
    if bands == 4:
        color_interp.append(gdal.GCI_AlphaBand)
    for idx in range(bands):
        band = ds.GetRasterBand(idx + 1)
        band.WriteArray(arr[:, :, idx])
        band.SetColorInterpretation(color_interp[idx])
    ds.FlushCache()
    ds = None
    return path
