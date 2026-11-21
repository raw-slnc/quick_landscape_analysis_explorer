# -*- coding: utf-8 -*-
"""Gaps in the DEM for the GeoPDF, filled for 3D viewing.

Apps that combine the GeoPDF with a DEM (such as Avid Maps' field map)
draw the DEM in 3D and
need a height in every cell: a cell without one is drawn at a made-up
height, as a spike in a valley or a wall at the edge. GSI DEMs leave the
sea, lakes and rivers, and gaps in the survey, without heights alike, and
the preview can reach beyond the DEM fetched for the analysis. Here the
cells without a height are sorted out and given one:

- the sea (inside GSI's water areas coded as sea, or where even DEM10B has
  no height): 0 m
- small holes not touching water: interpolated from around them
- land (gaps in the survey, the land beyond the fetched DEM): GSI DEM5A,
  then DEM10B, stretched to the grid
- lakes and rivers: interpolated from their banks (a level surface)

Outside Japan, where there are no GSI data, AWS Terrain Tiles (Mapzen
Terrarium, worldwide, a few metres to tens of metres) are used instead:
small holes from around them, the rest from Terrarium as it is (below
0 m: the sea floor, or land below sea level); anything left from around
it.

When land is left without a height because its tiles could not be read
(no network) or would be too many, FillIncomplete is raised and the DEM
is not made: stretching the edge values over it would make up heights.

The sea and water get heights too: this DEM is for looking at, not for
analysis.
No QGIS API, so it runs in the worker thread.
"""

import math

import numpy as np
from osgeo import gdal, ogr, osr

WATER_TILE_URL = (
    "https://cyberjapandata.gsi.go.jp/xyz/optimal_bvmap-v1/{z}/{x}/{y}.pbf"
)
WATER_TILE_MAX_ZOOM = 16
WATER_TILE_MIN_ZOOM = 12
# vt_code of the water areas (WA) that are the sea; lakes and rivers differ.
SEA_VT_CODE = 5100
# (url, zoom, encoding) of the coarser GSI DEMs used for the land.
COARSE_SOURCES = (
    (
        "https://cyberjapandata.gsi.go.jp/xyz/dem5a_png/{z}/{x}/{y}.png",
        15,
        "gsi",
    ),
    (
        "https://cyberjapandata.gsi.go.jp/xyz/dem_png/{z}/{x}/{y}.png",
        14,
        "gsi",
    ),
)
# Terrarium (outside Japan): the finest zoom used, lowered for wide areas.
TERRARIUM_URL = (
    "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"
)
TERRARIUM_MAX_ZOOM = 14
TERRARIUM_MIN_ZOOM = 10
# Holes not touching water up to this many cells are interpolated.
SMALL_HOLE_MAX_PX = 100
SMALL_HOLE_SEARCH_PX = 20
# Tiles fetched at most for one layer (water areas lower their zoom for
# wider areas; the coarse DEMs are skipped beyond it).
MAX_TILES = 200
TILE_SIZE = 256
WEB_MERCATOR_LIMIT = 20037508.3428
_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; QGIS plugin)"}


class FillIncomplete(Exception):
    """The land without a height could not be given one: ``reason`` is
    "network" (tiles could not be read) or "too_wide" (more tiles than
    MAX_TILES). Its edge values are not stretched over it instead: those
    would be made-up heights over the sea and the land alike."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def fill_for_3d(path, check=None, source="gsi"):
    """Give every cell of the DEM at ``path`` (a file, updated) a height.

    source: "gsi" (Japan) or "terrarium" (elsewhere). check: called between
    tiles (raises to cancel). Returns the number of cells given a height by
    kind.
    """
    dataset = gdal.Open(path, gdal.GA_Update)
    band = dataset.GetRasterBand(1)
    nodata = band.GetNoDataValue()
    data = band.ReadAsArray().astype(np.float64)
    if nodata is not None:
        data[data == nodata] = np.nan
    gt, projection = dataset.GetGeoTransform(), dataset.GetProjection()
    counts = {}
    missing = np.isnan(data)
    if missing.any():
        grid = _Grid(gt, projection, data.shape)
        if source == "terrarium":
            data, counts = _fill_terrarium(data, grid, check)
        else:
            data, counts = _fill(data, grid, check)
        out = data.astype(np.float32)
        out[np.isnan(data)] = nodata if nodata is not None else -9999.0
        band.WriteArray(out)
        band.FlushCache()
    band = None
    dataset = None
    return counts


class _Grid:
    """The DEM's grid and its transformations to and from Web Mercator."""

    def __init__(self, gt, projection, shape):
        self.gt = gt
        self.projection = projection
        self.shape = shape
        own = osr.SpatialReference()
        own.ImportFromWkt(projection)
        own.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        mercator = osr.SpatialReference()
        mercator.ImportFromEPSG(3857)
        mercator.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        self.srs = own
        self.mercator = mercator
        self.to_mercator = osr.CoordinateTransformation(own, mercator)

    def mercator_bounds(self, rows=None, cols=None):
        """(xmin, ymin, xmax, ymax) in Web Mercator of the cells in the
        given row and column ranges (all by default)."""
        r0, r1 = (float(value) for value in (rows or (0, self.shape[0])))
        c0, c1 = (float(value) for value in (cols or (0, self.shape[1])))
        xs, ys = [], []
        for i in range(11):
            for col, row in (
                (c0 + (c1 - c0) * i / 10.0, r0),
                (c0 + (c1 - c0) * i / 10.0, r1),
                (c0, r0 + (r1 - r0) * i / 10.0),
                (c1, r0 + (r1 - r0) * i / 10.0),
            ):
                x, y = gdal.ApplyGeoTransform(self.gt, col, row)
                mx, my, _ = self.to_mercator.TransformPoint(x, y)
                xs.append(mx)
                ys.append(my)
        return min(xs), min(ys), max(xs), max(ys)

    def raster(self, data_type):
        rows, cols = self.shape
        out = gdal.GetDriverByName("MEM").Create("", cols, rows, 1, data_type)
        out.SetGeoTransform(self.gt)
        out.SetProjection(self.projection)
        return out


def _tile_range(bounds, zoom):
    """Tiles (x0, y0, x1, y1) at ``zoom`` covering Web Mercator bounds."""
    tile_m = 2.0 * WEB_MERCATOR_LIMIT / (2**zoom)
    xmin, ymin, xmax, ymax = bounds
    x0 = int(math.floor((xmin + WEB_MERCATOR_LIMIT) / tile_m))
    x1 = int(math.floor((xmax + WEB_MERCATOR_LIMIT) / tile_m))
    y0 = int(math.floor((WEB_MERCATOR_LIMIT - ymax) / tile_m))
    y1 = int(math.floor((WEB_MERCATOR_LIMIT - ymin) / tile_m))
    return x0, y0, x1, y1


def _tile_count(tiles):
    x0, y0, x1, y1 = tiles
    return (x1 - x0 + 1) * (y1 - y0 + 1)


def _water_codes(grid, check):
    """vt_code of GSI's water areas (WA) in each cell (0 outside them), and
    whether every tile was read; (None, False) without the MVT driver."""
    import urllib.error
    import urllib.request

    if gdal.GetDriverByName("MVT") is None:
        return None, False
    bounds = grid.mercator_bounds()
    zoom = WATER_TILE_MAX_ZOOM
    while (
        zoom > WATER_TILE_MIN_ZOOM
        and _tile_count(_tile_range(bounds, zoom)) > MAX_TILES
    ):
        zoom -= 1
    tiles = _tile_range(bounds, zoom)
    if _tile_count(tiles) > MAX_TILES:
        return None, False
    out = grid.raster(gdal.GDT_UInt16)
    vectors = memory_vector_datasource()
    areas = vectors.CreateLayer("water", grid.srs, ogr.wkbMultiPolygon)
    areas.CreateField(ogr.FieldDefn("vt_code", ogr.OFTInteger))
    to_grid = osr.CoordinateTransformation(grid.mercator, grid.srs)
    complete = True
    x0, y0, x1, y1 = tiles
    for ty in range(y0, y1 + 1):
        for tx in range(x0, x1 + 1):
            if check is not None:
                check()
            url = WATER_TILE_URL.format(z=zoom, x=tx, y=ty)
            try:
                request = urllib.request.Request(url, headers=_HEADERS)
                with urllib.request.urlopen(  # nosec B310
                    request, timeout=15
                ) as response:
                    content = response.read()
            except urllib.error.HTTPError as error:
                if (
                    error.code != 404
                ):  # 404: nothing drawn (no water, far out at sea)
                    complete = False
                continue
            # not read: the water there is not known
            except Exception:  # noqa: BLE001
                complete = False
                continue
            name = "/vsimem/qlae_wa_{}_{}_{}.pbf".format(zoom, tx, ty)
            gdal.FileFromMemBuffer(name, content)
            try:
                tile = gdal.OpenEx(
                    name,
                    gdal.OF_VECTOR,
                    open_options=[
                        "X={}".format(tx),
                        "Y={}".format(ty),
                        "Z={}".format(zoom),
                    ],
                )
                layer = tile.GetLayerByName("WA") if tile is not None else None
                if layer is not None:
                    for feature in layer:
                        geometry = feature.GetGeometryRef()
                        if geometry is None:
                            continue
                        geometry = geometry.Clone()
                        geometry.AssignSpatialReference(grid.mercator)
                        if geometry.Transform(to_grid) != 0:
                            continue
                        item = ogr.Feature(areas.GetLayerDefn())
                        item.SetGeometry(geometry)
                        item.SetField(
                            "vt_code", int(feature.GetField("vt_code") or 1)
                        )
                        areas.CreateFeature(item)
                tile = None
            # not read: the water there is not known
            except Exception:  # noqa: BLE001
                complete = False
            finally:
                gdal.Unlink(name)
    if areas.GetFeatureCount():
        gdal.RasterizeLayer(out, [1], areas, options=["ATTRIBUTE=vt_code"])
    return out.GetRasterBand(1).ReadAsArray(), complete


def _coarse_on_grid(source, need, grid, check):
    """Heights of a coarse GSI DEM stretched to the grid (NaN where it has
    none), and the cells whose tile could not be read; None if too many."""
    from .dem_loader import GSITileDEMLoader

    url, zoom, encoding = source
    rows, cols = np.nonzero(need)
    bounds = grid.mercator_bounds(
        (rows.min(), rows.max() + 1), (cols.min(), cols.max() + 1)
    )
    tiles = _tile_range(bounds, zoom)
    if _tile_count(tiles) > MAX_TILES:
        return None, None
    x0, y0, x1, y1 = tiles
    mosaic = np.full(
        ((y1 - y0 + 1) * TILE_SIZE, (x1 - x0 + 1) * TILE_SIZE),
        np.nan,
        dtype=np.float32,
    )
    failed = np.zeros((y1 - y0 + 1, x1 - x0 + 1), dtype=bool)
    for ty in range(y0, y1 + 1):
        for tx in range(x0, x1 + 1):
            if check is not None:
                check()
            tile, error = GSITileDEMLoader._fetch_tile_array(
                url.format(z=zoom, x=tx, y=ty), encoding
            )
            iy, ix = ty - y0, tx - x0
            if tile is not None:
                mosaic[
                    iy * TILE_SIZE: (iy + 1) * TILE_SIZE,
                    ix * TILE_SIZE: (ix + 1) * TILE_SIZE,
                ] = tile
            elif error and "404" not in error:
                # 404: no heights there (the sea). Otherwise not known.
                failed[iy, ix] = True
    tile_m = 2.0 * WEB_MERCATOR_LIMIT / (2**zoom)
    pixel = tile_m / TILE_SIZE
    source_raster = gdal.GetDriverByName("MEM").Create(
        "", mosaic.shape[1], mosaic.shape[0], 1, gdal.GDT_Float32
    )
    source_raster.SetGeoTransform(
        (
            -WEB_MERCATOR_LIMIT + x0 * tile_m,
            pixel,
            0.0,
            WEB_MERCATOR_LIMIT - y0 * tile_m,
            0.0,
            -pixel,
        )
    )
    source_raster.SetProjection(grid.mercator.ExportToWkt())
    source_band = source_raster.GetRasterBand(1)
    source_band.SetNoDataValue(float("nan"))
    source_band.WriteArray(mosaic)
    target = grid.raster(gdal.GDT_Float32)
    target_band = target.GetRasterBand(1)
    target_band.SetNoDataValue(float("nan"))
    target_band.Fill(float("nan"))
    gdal.Warp(
        target,
        source_raster,
        resampleAlg="bilinear",
        srcNodata="nan",
        dstNodata="nan",
    )
    values = target_band.ReadAsArray().astype(np.float64)
    unread = np.zeros(grid.shape, dtype=bool)
    if failed.any():
        # The cells under the tiles that could not be read.
        mask = gdal.GetDriverByName("MEM").Create(
            "", mosaic.shape[1], mosaic.shape[0], 1, gdal.GDT_Byte
        )
        mask.SetGeoTransform(source_raster.GetGeoTransform())
        mask.SetProjection(grid.mercator.ExportToWkt())
        mask.GetRasterBand(1).WriteArray(
            np.kron(
                failed, np.ones((TILE_SIZE, TILE_SIZE), dtype=np.uint8)
            ).astype(np.uint8)
        )
        on_grid = grid.raster(gdal.GDT_Byte)
        gdal.Warp(on_grid, mask, resampleAlg="near")
        unread = on_grid.GetRasterBand(1).ReadAsArray() > 0
    return values, unread


def _fill(data, grid, check):
    data = data.copy()
    counts = {}
    missing = np.isnan(data)

    codes, _complete = _water_codes(grid, check)
    if codes is None:
        codes = np.zeros(data.shape, dtype=np.uint16)
    water = codes > 0
    sea = missing & (codes == SEA_VT_CODE)
    lake_river = missing & water & ~sea
    unknown = missing & ~water

    # Small holes not touching water: from around them.
    if unknown.any():
        labels, count = _label(unknown)
        sizes = np.bincount(labels.ravel(), minlength=count + 1)
        small = (sizes <= SMALL_HOLE_MAX_PX) & ~_touching(labels, count, water)
        small[0] = False
        small_px = small[labels]
        del labels
        if small_px.any():
            filled = _fill_from_neighbors(data, SMALL_HOLE_SEARCH_PX)
            done = small_px & ~np.isnan(filled)
            data[done] = filled[done]
            counts["interpolated"] = int(done.sum())
            unknown &= ~done

    # Land: DEM5A, then DEM10B; where DEM10B has no height either, the sea.
    any_unread = False
    for index, source in enumerate(COARSE_SOURCES):
        if not unknown.any():
            break
        values, unread = _coarse_on_grid(source, unknown, grid, check)
        if values is None:
            raise FillIncomplete("too_wide")
        any_unread = any_unread or bool(unread.any())
        done = unknown & ~np.isnan(values)
        data[done] = values[done]
        counts["dem5a" if index == 0 else "dem10b"] = int(done.sum())
        unknown &= ~done
        if index == len(COARSE_SOURCES) - 1:
            sea |= unknown & np.isnan(values) & ~unread
            unknown &= ~sea
    if unknown.any():
        # Only cells under tiles that could not be read are left.
        raise FillIncomplete("network" if any_unread else "too_wide")

    data[sea] = 0.0
    counts["sea"] = int(sea.sum())

    # Lakes and rivers: from their banks.
    for key, cells in (("water", lake_river),):
        if cells.any():
            filled = _fill_from_neighbors(data, max(data.shape))
            done = cells & ~np.isnan(filled)
            data[done] = filled[done]
            counts[key] = int(done.sum())
    return data, counts


def _fill_terrarium(data, grid, check):
    """Outside Japan: small holes from around them, the rest from Terrarium
    as it is, anything left from around it."""
    data = data.copy()
    counts = {}
    unknown = np.isnan(data)

    labels, count = _label(unknown)
    sizes = np.bincount(labels.ravel(), minlength=count + 1)
    small = sizes <= SMALL_HOLE_MAX_PX
    small[0] = False
    small_px = small[labels]
    del labels
    if small_px.any():
        filled = _fill_from_neighbors(data, SMALL_HOLE_SEARCH_PX)
        done = small_px & ~np.isnan(filled)
        data[done] = filled[done]
        counts["interpolated"] = int(done.sum())
        unknown &= ~done

    if unknown.any():
        rows, cols = np.nonzero(unknown)
        bounds = grid.mercator_bounds(
            (rows.min(), rows.max() + 1), (cols.min(), cols.max() + 1)
        )
        zoom = TERRARIUM_MAX_ZOOM
        while (
            zoom > TERRARIUM_MIN_ZOOM
            and _tile_count(_tile_range(bounds, zoom)) > MAX_TILES
        ):
            zoom -= 1
        values, unread = _coarse_on_grid(
            (TERRARIUM_URL, zoom, "terrarium"), unknown, grid, check
        )
        if values is None:
            raise FillIncomplete("too_wide")
        done = unknown & ~np.isnan(values)
        data[done] = values[done]
        counts["terrarium"] = int(done.sum())
        unknown &= ~done
        if (unknown & unread).any():
            raise FillIncomplete("network")

    if unknown.any():
        # Terrarium covers the earth: a few cells at most (its own gaps).
        filled = _fill_from_neighbors(data, max(data.shape))
        done = unknown & ~np.isnan(filled)
        data[done] = filled[done]
        counts["interpolated_rest"] = int(done.sum())
    return data, counts


def _dilate8(mask):
    """Grown by one cell to its 8 neighbours."""
    out = mask.copy()
    out[1:, :] |= mask[:-1, :]
    out[:-1, :] |= mask[1:, :]
    rows_done = out.copy()
    out[:, 1:] |= rows_done[:, :-1]
    out[:, :-1] |= rows_done[:, 1:]
    return out


def memory_vector_datasource():
    """An in-memory vector data source (the MEM driver from GDAL 3.11, where
    the Memory driver is deprecated)."""
    if int(gdal.VersionInfo()) >= 3110000:
        return gdal.GetDriverByName("MEM").Create(
            "", 0, 0, 0, gdal.GDT_Unknown
        )
    return ogr.GetDriverByName("Memory").CreateDataSource("")


def _label(mask):
    """Numbers (from 1) for the groups of True cells joined by their 8
    neighbours; (numbers, count). By polygons in GDAL, as scipy is not in
    every QGIS."""
    rows, cols = mask.shape
    gt = (0.0, 1.0, 0.0, float(rows), 0.0, -1.0)
    driver = gdal.GetDriverByName("MEM")
    source = driver.Create("", cols, rows, 1, gdal.GDT_Byte)
    source.SetGeoTransform(gt)
    band = source.GetRasterBand(1)
    band.WriteArray(mask.astype(np.uint8))
    vectors = memory_vector_datasource()
    layer = vectors.CreateLayer("groups", geom_type=ogr.wkbPolygon)
    layer.CreateField(ogr.FieldDefn("v", ogr.OFTInteger))
    gdal.Polygonize(band, band, layer, 0, ["8CONNECTED=8"])
    fids = [feature.GetFID() for feature in layer]
    for number, fid in enumerate(fids, start=1):
        feature = layer.GetFeature(fid)
        feature.SetField("v", number)
        layer.SetFeature(feature)
    target = driver.Create("", cols, rows, 1, gdal.GDT_Int32)
    target.SetGeoTransform(gt)
    gdal.RasterizeLayer(target, [1], layer, options=["ATTRIBUTE=v"])
    labels = target.GetRasterBand(1).ReadAsArray().astype(np.int32)
    labels[~mask] = 0
    return labels, len(fids)


def _touching(labels, count, seeds):
    """For each group, whether it touches a ``seeds`` cell (index 0 unused)."""
    touching = np.zeros(count + 1, dtype=bool)
    touched = labels[_dilate8(seeds) & (labels > 0)]
    touching[np.unique(touched)] = True
    touching[0] = False
    return touching


def _fill_from_neighbors(data, max_search_px):
    """The NaN cells interpolated from around them (GDAL FillNodata); those
    out of reach stay NaN."""
    nodata = np.isnan(data)
    rows, cols = data.shape
    driver = gdal.GetDriverByName("MEM")
    raster = driver.Create("", cols, rows, 1, gdal.GDT_Float32)
    band = raster.GetRasterBand(1)
    work = data.astype(np.float32)
    work[nodata] = -9999.0
    band.SetNoDataValue(-9999.0)
    band.WriteArray(work)
    mask_raster = driver.Create("", cols, rows, 1, gdal.GDT_Byte)
    mask_band = mask_raster.GetRasterBand(1)
    mask_band.WriteArray((~nodata).astype(np.uint8) * 255)
    gdal.FillNodata(band, mask_band, max_search_px, 0)
    filled = band.ReadAsArray().astype(np.float64)
    filled[filled == -9999.0] = np.nan
    return filled
