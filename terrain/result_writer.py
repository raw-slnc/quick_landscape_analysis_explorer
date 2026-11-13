# -*- coding: utf-8 -*-
"""GeoTIFF writers adapted from Forestry Operations Lite (GPL-2.0-or-later)."""

"""
解析結果の保存モジュール
  save_raster          : numpy配列 → GeoTIFF
  save_rgba_raster     : RGBA配列 → GeoTIFF
  mask_to_polygons     : バイナリマスク → ポリゴン GPKG
  values_to_points     : 閾値超セル → ポイント GPKG
  mask_to_centroids    : 連結成分ごとの重心 → ポイント GPKG

  overwrite=True のとき固定ファイル名で上書き保存。
  overwrite=False（デフォルト）のときタイムスタンプ付きで追記保存。
"""
import os
import numpy as np
from datetime import datetime

try:
    from osgeo import gdal, ogr, osr
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
    ds = drv.Create(path, cols, rows, 1, gdal.GDT_Float32,
                    options=["COMPRESS=LZW", "TILED=YES"])
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
    ds = drv.Create(path, cols, rows, bands, gdal.GDT_Byte, options=create_opts)
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

